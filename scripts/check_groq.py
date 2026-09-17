"""Stage 0 fail-fast diagnostic for the Groq LLM backend.

Run this BEFORE writing any application code. It answers five questions, in
order of how badly a wrong answer would hurt later:

    1. Is a usable API key present in the environment?
    2. Does the configured model respond to a plain chat completion?
    3. Does it return a well-formed *tool call* when given a tool schema?
    4. Which ``tool_choice`` mode does this model actually accept?
    5. What does a single call actually *cost* in tokens?

Question 4 is the reason this file exists. The query pipeline is designed
around *forcing* the model to select a tool, so that it cannot reply with prose
where an action was required. Groq's documentation does not state whether
``tool_choice="required"`` is supported, so rather than assume, we probe the
live API and fall back down a ladder of progressively more explicit modes.

Question 5 exists because ``gpt-oss-120b`` is a *reasoning* model: it spends
output tokens thinking before it answers, and those tokens count against the
free tier's 8,000 tokens-per-minute ceiling. ``reasoning_effort`` defaults to
``medium``, which is more deliberation than single-table SQL generation needs.
Probe 5 measures both settings so the throughput figure in the README is a
measurement rather than a guess.

This is a hand-run diagnostic, not part of the automated test suite: it makes
live network calls and spends API tokens. It therefore lives in ``scripts/``
rather than ``tests/``, and its name keeps it outside pytest's ``test_*.py``
collection pattern.

Usage (from the repository root)::

    python scripts/check_groq.py

Exit code is 0 if the backend is usable, 1 otherwise, so this can also gate a
setup script or CI job.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from dotenv import load_dotenv
from groq import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    Groq,
    RateLimitError,
)

# Resolve .env from the repository root rather than the current working
# directory, so the script behaves identically whether it is run as
# `python scripts/check_groq.py` or from inside the scripts/ folder.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
ENV_PATH: Final[Path] = REPO_ROOT / ".env"

# A deliberately trivial tool. We are testing the *mechanism* of tool calling,
# not the model's SQL ability, so the schema is kept minimal to isolate the
# thing under test.
PROBE_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "query_tickets",
        "description": (
            "Run a read-only SQL SELECT against the support ticket table and "
            "return the matching rows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single SQLite SELECT statement.",
                }
            },
            "required": ["sql"],
        },
    },
}

# The ladder, ordered most-preferred first. Each rung constrains the model more
# explicitly than the last; the application will use the first rung that works.
TOOL_CHOICE_LADDER: Final[list[tuple[str, Any]]] = [
    ("required", "required"),
    ("auto", "auto"),
    ("named", {"type": "function", "function": {"name": "query_tickets"}}),
]

PROBE_QUESTION: Final[str] = "How many tickets are currently open?"

# Reasoning settings to compare in probe 5. "high" is omitted deliberately:
# it is plainly wrong for this workload and would only spend tokens to prove it.
REASONING_EFFORTS: Final[tuple[str, ...]] = ("low", "medium")

# Free-tier ceiling for the chosen model, used to convert a per-call token cost
# into a throughput figure. See https://console.groq.com/docs/rate-limits
FREE_TIER_TPM: Final[int] = 8_000

# A question costs two LLM calls: one to choose a tool, one to narrate the
# result. Used to extrapolate per-question cost from a single measured call.
CALLS_PER_QUESTION: Final[int] = 2

_PASS = "  [PASS]"
_FAIL = "  [FAIL]"
_WARN = "  [WARN]"
_INFO = "  [INFO]"


@dataclass
class UsageSample:
    """Token accounting for one completion at a given reasoning effort.

    Attributes:
        effort: The ``reasoning_effort`` setting used for the call.
        prompt: Tokens consumed by the input messages and tool schema.
        completion: Tokens generated, inclusive of reasoning tokens.
        reasoning: Tokens spent thinking, when the API reports them separately.
        total: Prompt plus completion, as billed.
    """

    effort: str
    prompt: int
    completion: int
    reasoning: int
    total: int


@dataclass
class Diagnosis:
    """Accumulated result of the probes, used to decide the exit code.

    Attributes:
        key_present: Whether a non-placeholder API key was found.
        chat_ok: Whether a plain chat completion succeeded.
        model: The model id that was exercised.
        working_modes: ``tool_choice`` modes that produced a real tool call,
            ordered best-first.
        usage_samples: Measured token cost per reasoning effort setting.
        errors: Human-readable failure messages collected along the way.
    """

    key_present: bool = False
    chat_ok: bool = False
    model: str = ""
    working_modes: list[str] = field(default_factory=list)
    usage_samples: list[UsageSample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether the backend can support the planned query pipeline."""
        return self.key_present and self.chat_ok and bool(self.working_modes)


def describe_exception(exc: Exception) -> str:
    """Translate an SDK exception into an actionable message.

    The SDK's default reprs are noisy and bury the part that tells you what to
    fix, which is exactly what is needed while setting up.

    Args:
        exc: The exception raised by the Groq SDK.

    Returns:
        A short explanation aimed at the person running the diagnostic.
    """
    if isinstance(exc, AuthenticationError):
        return "Key rejected. Check GROQ_API_KEY in .env is complete and current."
    if isinstance(exc, RateLimitError):
        return (
            "Rate limited. The free tier allows 30 requests/min and 8K "
            "tokens/min - wait a minute and retry."
        )
    if isinstance(exc, APIConnectionError):
        return "Could not reach Groq. Check your internet connection."
    if isinstance(exc, APIStatusError):
        # A 404 here almost always means the model id is wrong or the model has
        # moved off the free tier - a trap worth naming explicitly.
        detail = f"HTTP {exc.status_code}"
        if exc.status_code == 404:
            detail += " - model id not found, or not available on your tier"
        return f"API error: {detail}"
    return f"{type(exc).__name__}: {exc}"


def load_key_and_model() -> tuple[str, str]:
    """Read the API key and model id from the repository's .env file.

    Returns:
        An ``(api_key, model)`` pair. ``api_key`` is an empty string when the
        value is absent or still the placeholder from ``.env.example``.
    """
    load_dotenv(ENV_PATH)
    key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()

    # Treat the untouched template value as "missing". Otherwise the failure
    # surfaces later as a confusing authentication error.
    if key.startswith("gsk_replace") or key.startswith("gsk_paste"):
        key = ""
    return key, model


def probe_chat(client: Groq, model: str, result: Diagnosis) -> None:
    """Probe 2: confirm the model answers an ordinary chat completion.

    Args:
        client: Configured Groq client.
        model: Model id to exercise.
        result: Diagnosis object, mutated in place with the outcome.
    """
    print("\n[2/5] Plain chat completion")
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            # A reasoning model spends output tokens thinking before it writes.
            # Too small a cap here is consumed entirely by reasoning and yields
            # empty content - which looks like a broken model but is really a
            # budgeting mistake. Low effort plus real headroom avoids that.
            reasoning_effort="low",
            max_tokens=64,
            temperature=0,
        )
        reply = (completion.choices[0].message.content or "").strip()

        if not reply:
            # Empty content is a failure, not a pass. Saying otherwise would
            # hide exactly the problem this probe exists to surface.
            print(f"{_FAIL} model returned empty content")
            print(f"{_INFO} reasoning likely consumed the max_tokens budget")
            result.errors.append(
                "Model returned empty content - raise max_tokens or lower "
                "reasoning_effort."
            )
            return

        print(f"{_PASS} model replied: {reply!r}")
        result.chat_ok = True
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never crashes
        message = describe_exception(exc)
        print(f"{_FAIL} {message}")
        result.errors.append(message)


def probe_tool_choice(client: Groq, model: str, result: Diagnosis) -> None:
    """Probes 3 and 4: test each ``tool_choice`` mode on the ladder.

    Records every mode that the API accepts *and* that produces a real tool
    call, so the application can be configured to use the strictest one
    available.

    Args:
        client: Configured Groq client.
        model: Model id to exercise.
        result: Diagnosis object, mutated in place with the outcome.
    """
    print("\n[3/5] Tool calling, and [4/5] tool_choice ladder")

    for label, mode in TOOL_CHOICE_LADDER:
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You query a support ticket database. The table "
                            "`tickets` has columns: status, priority, category."
                        ),
                    },
                    {"role": "user", "content": PROBE_QUESTION},
                ],
                tools=[PROBE_TOOL],
                tool_choice=mode,
                max_tokens=200,
                temperature=0,
            )
            calls = completion.choices[0].message.tool_calls

            if not calls:
                # The API accepted the parameter but the model answered in
                # prose. That is precisely the failure mode this design guards
                # against, so the rung does not count as working.
                print(
                    f"{_WARN} tool_choice={label!r}: accepted, "
                    "but no tool call returned"
                )
                continue

            arguments = calls[0].function.arguments
            print(
                f"{_PASS} tool_choice={label!r}: called "
                f"{calls[0].function.name}({arguments[:70]}...)"
            )
            result.working_modes.append(label)

        except Exception as exc:  # noqa: BLE001 - while probing, failure is data
            print(f"{_FAIL} tool_choice={label!r}: {describe_exception(exc)}")


def extract_reasoning_tokens(usage: Any) -> int:
    """Pull the reasoning-token count out of a usage object, if present.

    Groq mirrors OpenAI's schema, where reasoning tokens are nested under
    ``completion_tokens_details``. That field is not guaranteed to exist on
    every model or SDK version, so every hop is accessed defensively rather
    than assumed - a missing field should read as "not reported", never crash
    a diagnostic.

    Args:
        usage: The ``usage`` object from a chat completion response.

    Returns:
        The reasoning token count, or 0 when the API does not report one.
    """
    details = getattr(usage, "completion_tokens_details", None)
    if details is None:
        return 0
    return int(getattr(details, "reasoning_tokens", 0) or 0)


def measure_token_cost(client: Groq, model: str, result: Diagnosis) -> None:
    """Probe 5: measure real token cost at each reasoning effort setting.

    Issues the identical tool-calling request once per setting and records what
    the API reports spending. This converts the throughput claim in the README
    from an estimate into a measurement.

    Args:
        client: Configured Groq client.
        model: Model id to exercise.
        result: Diagnosis object, mutated in place with the samples.
    """
    print("\n[5/5] Token cost per reasoning_effort")

    for effort in REASONING_EFFORTS:
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You query a support ticket database. The table "
                            "`tickets` has columns: status, priority, category."
                        ),
                    },
                    {"role": "user", "content": PROBE_QUESTION},
                ],
                tools=[PROBE_TOOL],
                tool_choice="required",
                reasoning_effort=effort,
                max_tokens=400,  # Headroom so reasoning cannot truncate output.
                temperature=0,
            )
            usage = completion.usage
            sample = UsageSample(
                effort=effort,
                prompt=int(usage.prompt_tokens),
                completion=int(usage.completion_tokens),
                reasoning=extract_reasoning_tokens(usage),
                total=int(usage.total_tokens),
            )
            result.usage_samples.append(sample)

            reasoning_note = (
                f", reasoning {sample.reasoning}" if sample.reasoning else ""
            )
            print(
                f"{_PASS} effort={effort:<7} prompt {sample.prompt}, "
                f"completion {sample.completion}{reasoning_note}, "
                f"total {sample.total}"
            )

        except Exception as exc:  # noqa: BLE001 - measurement failure is data
            print(f"{_FAIL} effort={effort!r}: {describe_exception(exc)}")


def summarise_budget(result: Diagnosis) -> None:
    """Translate measured token cost into a free-tier throughput figure.

    Args:
        result: A diagnosis holding at least one usage sample.
    """
    if not result.usage_samples:
        return

    cheapest = min(result.usage_samples, key=lambda s: s.total)
    per_question = cheapest.total * CALLS_PER_QUESTION
    per_minute = FREE_TIER_TPM / per_question

    print(f"  reasoning_effort: use {cheapest.effort!r} "
          f"({cheapest.total} tokens/call)")
    print(f"  measured budget : ~{per_question} tokens/question "
          f"({CALLS_PER_QUESTION} calls)")
    print(f"                    ~{per_minute:.1f} questions/min "
          f"within the {FREE_TIER_TPM:,} TPM free-tier ceiling")

    # Only worth reporting the delta when both settings were measured.
    if len(result.usage_samples) > 1:
        dearest = max(result.usage_samples, key=lambda s: s.total)
        saved = dearest.total - cheapest.total
        if saved > 0:
            pct = saved / dearest.total * 100
            print(f"  saving          : {saved} tokens/call vs "
                  f"{dearest.effort!r} ({pct:.0f}% cheaper)")


def report(result: Diagnosis) -> None:
    """Print the verdict and the configuration decision it implies.

    Args:
        result: The populated diagnosis to summarise.
    """
    print("\n" + "=" * 62)
    if result.usable:
        best = result.working_modes[0]
        print("VERDICT: backend is usable.")
        print(f"  model           : {result.model}")
        print(
            f"  tool_choice     : use {best!r} "
            f"(modes that worked: {', '.join(result.working_modes)})"
        )
        if best != "required":
            print(
                "  note            : 'required' is unavailable, so the app "
                "must detect\n                    prose replies and retry - "
                "rung 2 of the ladder."
            )
        summarise_budget(result)
    else:
        print("VERDICT: backend NOT usable. Fix before writing app code.")
        for err in result.errors:
            print(f"  - {err}")
        if not result.key_present:
            print("  - Set GROQ_API_KEY in .env (get one at console.groq.com)")
    print("=" * 62)


def main() -> int:
    """Run all probes and return a shell exit code.

    Returns:
        0 when the backend can support the planned pipeline, 1 otherwise.
    """
    print("=" * 62)
    print("Groq backend diagnostic - Stage 0 fail-fast gate")
    print("=" * 62)

    result = Diagnosis()

    print("\n[1/5] Credentials")
    if not ENV_PATH.exists():
        print(f"{_FAIL} no .env found at {ENV_PATH}")
        report(result)
        return 1

    api_key, model = load_key_and_model()
    result.model = model

    if not api_key:
        print(f"{_FAIL} GROQ_API_KEY missing or still the placeholder value")
        report(result)
        return 1

    # Never print the key itself - only enough to confirm the right one loaded.
    print(f"{_PASS} key loaded ({api_key[:7]}...{api_key[-4:]}, {len(api_key)} chars)")
    print(f"{_PASS} model configured: {model}")
    result.key_present = True

    client = Groq(api_key=api_key)
    probe_chat(client, model, result)

    # No point probing tool calling if basic completion already failed - the
    # error would be identical and would only add noise.
    if result.chat_ok:
        probe_tool_choice(client, model, result)

    # Only measure cost once tool calling is known to work, so the samples
    # reflect the shape of call the application will actually make.
    if result.working_modes:
        measure_token_cost(client, model, result)

    report(result)
    return 0 if result.usable else 1


if __name__ == "__main__":
    sys.exit(main())
