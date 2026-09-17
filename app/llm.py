"""Tool-calling orchestration: natural-language questions to grounded answers.

The pipeline is bounded: **two model calls on the happy path, four at worst.**
Those four are one tool selection, one retry if the model replies in prose
instead of calling a tool, one repair if the generated SQL is rejected, and one
narration. Every recovery path is capped at a single additional attempt, so the
ceiling holds regardless of how badly a question goes.

Per question:

    1. **Choose an action.** The model is given two tools and forced to call
       one. It cannot reply with prose where an action was required.
    2. **Execute locally.** Generated SQL passes :mod:`app.sql_guard` and runs
       on a read-only connection; an anomaly request runs the deterministic
       detectors. Either way, *this* layer produces the numbers.
    3. **Narrate.** The model writes an answer from the real result.

The central rule: **the model never performs arithmetic.** It translates
language into SQL or tool arguments, and later phrases results it is handed.
Every figure a user sees was computed by SQLite or by pandas. This removes the
most common failure in systems like this - fluent, confident, invented numbers
- and it is why the pipeline is worth its extra round trip.

Why bounded rather than agentic
-------------------------------
An open-ended agent loop would let the model retry indefinitely, which on an
8,000 token-per-minute free tier means a single confused question can exhaust
the budget for everyone. Capping every recovery path at one extra attempt gives
a predictable ceiling: roughly 1,500 tokens for a normal question, and about
3,000 in the worst case.

Retry policy
------------
Transport failures are retried; rate limits are not. That distinction is
deliberate and is the reason the SDK's own retry loop is disabled - it retries
429 unconditionally, which is right for a per-request quota and wrong for a
per-minute token budget, where recovery takes about a minute and the backoff
lasts seconds. See :func:`is_retryable`.

Dependency inversion
--------------------
Nothing here imports the Groq SDK except :class:`GroqChatClient`. The service
depends on the :class:`ChatClient` protocol, so the test suite injects a fake
returning scripted responses and runs offline, instantly, with no API key and
no cost. That is the entire justification for the abstraction - it is not
ceremony, it is what makes the pipeline testable at all.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from app.anomalies import detect_anomalies, load_frame
from app.config import settings
from app.data import QueryTimeoutError, execute_select, read_only_connection
from app.grounding import ungrounded_numbers
from app.prompts import (
    ANOMALY_TOOL,
    QUERY_TOOL,
    REFUSAL_MESSAGE,
    build_narration_messages,
    build_system_prompt,
    build_tool_schemas,
    render_anomaly_reports,
    render_rows,
    truncate_rows,
)
from app.sql_guard import SqlGuardError, validate_select

logger = logging.getLogger(__name__)

# Deterministic output. A data question has one correct answer, so there is no
# value in sampling variety - and repeatability makes failures reproducible.
TEMPERATURE: Final[float] = 0.0

# Generous enough for a long SELECT plus the model's reasoning tokens, which
# count toward this limit. Too small a value returns empty content rather than
# an error, which reads as a broken model but is a budgeting mistake.
TOOL_CALL_MAX_TOKENS: Final[int] = 700
NARRATION_MAX_TOKENS: Final[int] = 400

# Measured at 12 reasoning tokens against 35 for the default "medium", with
# identical SQL on this schema. Single-table aggregation is not a hard
# reasoning problem.
REASONING_EFFORT: Final[str] = "low"

# Model families that accept the reasoning_effort parameter. Sending it to a
# model that does not understand it is rejected by the API, so a configured
# fallback model must not inherit it blindly.
_REASONING_MODEL_PREFIXES: Final[tuple[str, ...]] = ("openai/gpt-oss",)

# HTTP statuses worth retrying. All describe a fault on the provider's side
# that a moment's delay may clear: a request timeout, a lock conflict, or a
# server error. A 4xx means our request was wrong, and repeating an identical
# wrong request cannot produce a different answer.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 409, 500, 502, 503, 504})

# Base delay for exponential backoff, and the ceiling it may reach.
_BACKOFF_BASE_SECONDS: Final[float] = 0.5
_BACKOFF_MAX_SECONDS: Final[float] = 4.0


class LlmError(RuntimeError):
    """Base class for every failure originating in the language-model layer."""


class LlmNotConfiguredError(LlmError):
    """Raised when a question is asked but no API key is configured.

    Distinct from a transport failure because the remedy is different, and the
    API maps it to a different status code: nothing is wrong with the service,
    the operator simply has not supplied credentials.
    """


class LlmUnavailableError(LlmError):
    """Raised when the model provider cannot be reached or fails."""


class LlmRateLimitedError(LlmError):
    """Raised when the provider's rate limit is hit.

    Attributes:
        retry_after: Seconds the provider asked the caller to wait, when it
            said. Surfaced so the API can return a ``Retry-After`` header
            rather than leaving a client to guess.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        """Initialise the error.

        Args:
            message: Human-readable explanation.
            retry_after: Seconds to wait before retrying, if known.
        """
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model.

    Provider-neutral by design: the orchestration layer never touches a Groq
    SDK object, so swapping providers means writing one adapter rather than
    editing the pipeline.

    Attributes:
        name: The tool the model chose.
        arguments: Parsed arguments.
    """

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    """A single completion, reduced to what this application uses.

    Attributes:
        text: Assistant message content, when the model wrote prose.
        tool_calls: Tool invocations requested, if any.
        prompt_tokens: Tokens consumed by the input.
        completion_tokens: Tokens generated, including reasoning tokens.
        declined: Whether the model deliberately refused to call a tool. A
            considered judgement, not a failure - and therefore final.
    """

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    declined: bool = False


@runtime_checkable
class ChatClient(Protocol):
    """The contract the orchestration layer depends on.

    Attributes:
        model: Identifier of the model being called, recorded in responses.
    """

    model: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_tool: bool = False,
        max_tokens: int = NARRATION_MAX_TOKENS,
    ) -> ChatResponse:
        """Request a completion.

        Args:
            messages: Conversation in OpenAI chat format.
            tools: Tool schemas the model may call.
            force_tool: Require the model to call a tool rather than reply
                with prose.
            max_tokens: Ceiling on generated tokens.

        Returns:
            The model's response.

        Raises:
            LlmUnavailableError: If the provider cannot be reached.
            LlmRateLimitedError: If the provider's rate limit is hit.
        """
        ...


class GroqChatClient:
    """A :class:`ChatClient` backed by the Groq API.

    The only class in the application that imports the Groq SDK. Its job is
    translation: Groq's response objects in, this module's provider-neutral
    dataclasses out, with the SDK's exceptions mapped onto ours.
    """

    def __init__(self, api_key: str, model: str) -> None:
        """Initialise the client.

        Args:
            api_key: A Groq API key.
            model: Model identifier to call.
        """
        # Imported here rather than at module scope so that the rest of this
        # module - and the tests that exercise it - never need the SDK present
        # or an API key configured.
        try:
            from groq import Groq
        except ImportError as exc:
            # A partially completed install would otherwise raise ImportError,
            # which the startup path does not treat as an LLM failure - so the
            # whole service would fail to start, including the endpoints that
            # never touch this SDK. Operationally a missing package and a
            # missing key are the same condition: the capability is
            # unavailable, and the system should degrade to its deterministic
            # half rather than refuse to run.
            raise LlmNotConfiguredError(
                "The 'groq' package is not installed, so natural-language "
                "questions are unavailable. Anomaly detection and health "
                "checks still work. Run: pip install -r requirements.txt"
            ) from exc

        # max_retries=0 disables the SDK's own retry loop so that the policy
        # lives in one visible place. The SDK retries 429 unconditionally and
        # offers no way to exclude it, which is the wrong behaviour on a
        # per-minute token budget - see is_retryable().
        self._client = Groq(
            api_key=api_key,
            max_retries=0,
            timeout=settings.llm_timeout_seconds,
        )
        self.model = model
        self._max_retries = settings.llm_max_retries

    def _supports_reasoning_effort(self) -> bool:
        """Report whether the configured model accepts ``reasoning_effort``.

        Returns:
            ``True`` when the model belongs to a family that understands the
            parameter. A configured fallback from another family would have
            the request rejected outright if it were sent regardless.
        """
        return self.model.startswith(_REASONING_MODEL_PREFIXES)

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_tool: bool = False,
        max_tokens: int = NARRATION_MAX_TOKENS,
    ) -> ChatResponse:
        """Request a completion from Groq.

        Args:
            messages: Conversation in OpenAI chat format.
            tools: Tool schemas the model may call.
            force_tool: Require the model to call a tool. Verified as supported
                against the live API before this pipeline was built on it.
            max_tokens: Ceiling on generated tokens.

        Returns:
            The model's response, in provider-neutral form.

        Raises:
            LlmRateLimitedError: If the account's rate limit is exhausted.
            LlmUnavailableError: For transport failures, authentication
                problems, and any other API error.
        """
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "required" if force_tool else "auto"
        if self._supports_reasoning_effort():
            request["reasoning_effort"] = REASONING_EFFORT

        last_error: Exception | None = None

        # One initial attempt plus the configured retries. Bounded, so a
        # persistently failing provider cannot stall a request indefinitely.
        for attempt in range(self._max_retries + 1):
            try:
                completion = self._client.chat.completions.create(**request)
                return _to_chat_response(completion)
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                declined = _declined_to_use_a_tool(exc)
                if declined is not None:
                    recovered = _recover_tool_call(declined)
                    if recovered is not None:
                        # The model meant to call a tool and merely formatted
                        # it wrongly. Its intent is clear, so it is honoured
                        # rather than surfaced as a refusal.
                        logger.info("Recovered a malformed %s call", recovered.name)
                        return ChatResponse(tool_calls=[recovered])

                    # Not a failure. Groq rejects the whole request when
                    # tool_choice="required" and the model chooses to answer in
                    # prose instead - the intended reply is returned in the
                    # error body rather than in a message.
                    #
                    # That is the *correct* outcome for "what is the capital of
                    # France?" or "delete all tickets": the model recognised it
                    # should not call a tool. Surfacing it as HTTP 400 would
                    # turn well-judged behaviour into what looks like a broken
                    # service.
                    return ChatResponse(text=declined, declined=True)

                last_error = exc

                if not is_retryable(exc) or attempt == self._max_retries:
                    break

                delay = backoff_delay(attempt)
                logger.warning(
                    "Provider call failed (%s); retry %d of %d in %.1fs",
                    type(exc).__name__,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                time.sleep(delay)

        raise _translate_provider_error(last_error)


def _declined_to_use_a_tool(exc: Exception) -> str | None:
    """Recognise a refusal that the provider reports as a failed request.

    Groq does not return a normal message when ``tool_choice="required"`` is
    set and the model answers in prose. It rejects the request with HTTP 400
    and ``code: tool_use_failed``, placing the model's intended reply in a
    ``failed_generation`` field.

    That distinction matters. A model declining to run a query for "delete all
    tickets" or "what is the capital of France?" is behaving exactly as
    intended; reporting it as a provider error would present good judgement as
    a broken service. Every field is read defensively, since the error body's
    shape is not part of any contract.

    Args:
        exc: The exception raised by the provider SDK.

    Returns:
        The model's intended reply when this was a refusal, otherwise ``None``.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None

    error = body.get("error")
    if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
        return None

    generated = error.get("failed_generation")
    if isinstance(generated, str) and generated.strip():
        return generated.strip()

    # The refusal is real even when the text is absent, so it must still be
    # reported as a decline rather than falling through to an error.
    return REFUSAL_MESSAGE


def _recover_tool_call(generated: str) -> ToolCall | None:
    """Rebuild a tool call the model emitted as text rather than as a call.

    The provider rejects the request when the model writes a tool call into the
    message body instead of the tool-call field, and returns that text in
    ``failed_generation``. The model's intent is unambiguous - it named a tool
    and supplied arguments - so honouring it recovers an answer that would
    otherwise be lost.

    Without this, the raw JSON was shown to the user as though it were a
    refusal, which leaked internals and answered nothing.

    Args:
        generated: The text the model produced instead of a tool call.

    Returns:
        The reconstructed call, or ``None`` when the text is ordinary prose.
    """
    text = generated.strip()
    if not text.startswith("{"):
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    name = payload.get("name")
    if name not in {QUERY_TOOL, ANOMALY_TOOL}:
        return None

    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        # Some responses nest the arguments as a JSON string.
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        arguments = {}

    # Nulls arrive as None and mean "argument omitted"; passing them through
    # would fail the detectors' type checks.
    cleaned = {k: v for k, v in arguments.items() if v is not None}
    return ToolCall(name=str(name), arguments=cleaned)


def _translate_provider_error(exc: Exception | None) -> LlmError:
    """Convert a provider exception into this module's own error type.

    Kept separate from the retry loop so that classification, backoff and
    translation each remain independently readable - and so the loop can raise
    whatever it ended on without restating the mapping.

    Args:
        exc: The final exception from the provider, or ``None`` if somehow
            absent.

    Returns:
        The equivalent :class:`LlmError` subclass, ready to raise.
    """
    from groq import APIConnectionError, APIStatusError, RateLimitError

    if isinstance(exc, RateLimitError):
        return LlmRateLimitedError(
            "The model provider's rate limit has been reached. The free tier "
            "allows 30 requests and 8,000 tokens per minute.",
            retry_after=_retry_after_seconds(exc),
        )

    if isinstance(exc, APIConnectionError):
        return LlmUnavailableError(
            "Could not reach the model provider. Check network connectivity."
        )

    if isinstance(exc, APIStatusError):
        return LlmUnavailableError(
            f"The model provider returned an error (HTTP {exc.status_code})."
        )

    return LlmUnavailableError(
        f"The model provider failed unexpectedly: {exc}"
    )


def is_retryable(exc: Exception) -> bool:
    """Decide whether a provider failure is worth attempting again.

    The whole substance of a retry policy is this one question: *is repeating
    this request likely to produce a different result?*

    Rate limits are the interesting case, and the reason this function exists
    rather than the SDK's own retry being left enabled. The SDK retries 429 by
    default, which is sensible for a per-request quota but wrong for a
    per-*minute* token budget: recovery takes about a minute, while the backoff
    lasts seconds. Retrying cannot succeed, spends two more requests against a
    30-per-minute ceiling, and delays an honest error the caller could have
    acted on immediately.

    Args:
        exc: The exception raised by the provider SDK.

    Returns:
        ``True`` when the failure is transient and a retry is justified.
    """
    from groq import APIConnectionError, APIStatusError, RateLimitError

    # Checked before APIStatusError, which RateLimitError subclasses - the
    # order here is what makes the 429 exclusion actually hold.
    if isinstance(exc, RateLimitError):
        return False

    # A dropped or refused connection never reached the provider, so nothing
    # about the request itself is in question.
    if isinstance(exc, APIConnectionError):
        return True

    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES

    return False


def backoff_delay(attempt: int) -> float:
    """Return the delay before a given retry attempt.

    Exponential with full jitter. The jitter is not decoration: without it
    every client that failed together retries together, reproducing the load
    that caused the failure. Randomising spreads them out.

    Args:
        attempt: Zero-based retry number.

    Returns:
        Seconds to wait.
    """
    ceiling = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
    return random.uniform(0, ceiling)


def _retry_after_seconds(exc: Any) -> float | None:
    """Extract a ``Retry-After`` value from a provider exception.

    Every hop is accessed defensively: the header is not guaranteed to be
    present, and a missing one must read as "unknown" rather than crash the
    error path - failing while reporting a failure is a poor outcome.

    Args:
        exc: The provider's rate-limit exception.

    Returns:
        Seconds to wait, or ``None`` when the provider did not say.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    raw = headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _to_chat_response(completion: Any) -> ChatResponse:
    """Convert a Groq completion into a provider-neutral response.

    Args:
        completion: The SDK's response object.

    Returns:
        The equivalent :class:`ChatResponse`.
    """
    message = completion.choices[0].message
    usage = getattr(completion, "usage", None)

    tool_calls: list[ToolCall] = []
    for raw_call in message.tool_calls or []:
        try:
            arguments = json.loads(raw_call.function.arguments or "{}")
        except json.JSONDecodeError:
            # Malformed arguments are treated as an empty call rather than
            # propagated: the orchestration layer already handles a tool call
            # it cannot act on, and that path produces a better message than a
            # raw JSON error would.
            logger.warning(
                "Model emitted unparseable arguments for %s", raw_call.function.name
            )
            arguments = {}
        tool_calls.append(
            ToolCall(name=raw_call.function.name, arguments=arguments)
        )

    return ChatResponse(
        text=message.content,
        tool_calls=tool_calls,
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


@dataclass(frozen=True)
class QueryResult:
    """A complete answer, with the evidence that produced it.

    The generated SQL, row count and timing are returned alongside the prose
    deliberately. A user - and an evaluator - can then see exactly how a figure
    was derived instead of being asked to trust it.

    Attributes:
        question: The question as asked.
        answer: The model's natural-language answer.
        tool: Which tool was used, or ``None`` if the question was declined.
        sql: The SQL executed, when the query tool was used.
        rows: Full result set, uncapped.
        row_count: Number of rows returned.
        truncated: Whether the model saw fewer rows than were returned.
        anomaly_reports: Serialised detector reports, when that tool was used.
        as_of: Reference time used to resolve relative dates.
        elapsed_ms: Wall-clock duration of the whole pipeline.
        model: Model identifier that answered.
        prompt_tokens: Total input tokens across both calls.
        completion_tokens: Total generated tokens across both calls.
    """

    question: str
    answer: str
    tool: str | None
    sql: str | None
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    anomaly_reports: list[dict[str, Any]] | None
    as_of: datetime
    elapsed_ms: int
    model: str
    prompt_tokens: int
    completion_tokens: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this result.

        Returns:
            A mapping suitable for an API response body.
        """
        return {
            "question": self.question,
            "answer": self.answer,
            "tool": self.tool,
            "sql": self.sql,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "anomaly_reports": self.anomaly_reports,
            "as_of": self.as_of.isoformat(sep=" "),
            "elapsed_ms": self.elapsed_ms,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class TicketQueryService:
    """Answers natural-language questions about the ticket dataset.

    Holds no global state and opens no connection until asked, so it is safe to
    construct once at application start and call concurrently.
    """

    def __init__(
        self,
        *,
        client: ChatClient,
        db_path: Path,
        as_of: datetime,
        row_count: int,
        max_rows: int | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            client: Any object satisfying :class:`ChatClient`. Injected rather
                than constructed here, so tests can supply a scripted fake.
            db_path: Path to the database built at startup.
            as_of: Reference time for relative date expressions.
            row_count: Number of tickets, quoted to the model for context.
            max_rows: Hard cap on rows a single query may return. Defaults to
                the configured value.
        """
        self._client = client
        self._db_path = db_path
        self._as_of = as_of
        self._row_count = row_count
        self._max_rows = max_rows if max_rows is not None else settings.max_result_rows
        self._tools = build_tool_schemas()
        self._system_prompt = build_system_prompt(as_of, row_count)

    def answer(self, question: str) -> QueryResult:
        """Answer a question about the dataset.

        Args:
            question: A natural-language question.

        Returns:
            The answer together with the evidence behind it.

        Raises:
            ValueError: If ``question`` is blank.
            LlmRateLimitedError: If the provider's rate limit is hit.
            LlmUnavailableError: If the provider cannot be reached.
        """
        if not question or not question.strip():
            raise ValueError("A question is required.")

        started = time.monotonic()
        prompt_tokens = 0
        completion_tokens = 0

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": question.strip()},
        ]

        selection = self._client.complete(
            messages,
            tools=self._tools,
            force_tool=True,
            max_tokens=TOOL_CALL_MAX_TOKENS,
        )
        prompt_tokens += selection.prompt_tokens
        completion_tokens += selection.completion_tokens

        if selection.declined:
            # The model judged that no tool applies - to "what is the capital
            # of France?", or "delete all tickets". That judgement is correct
            # and must stand.
            #
            # Retrying here was actively harmful: forcing a tool call onto a
            # refused question produced a query returning nothing, after which
            # the narration step answered "Paris." from the model's own
            # knowledge. Overriding a correct refusal is how an ungrounded
            # answer gets manufactured, which is the one failure this whole
            # design exists to prevent.
            logger.info("Model declined to use a tool for: %s", question)
            return self._declined(
                question,
                answer=selection.text or REFUSAL_MESSAGE,
                started=started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        if not selection.tool_calls:
            # Second rung of the tool-selection ladder. `tool_choice="required"`
            # makes prose unlikely but not impossible, and one explicit reminder
            # recovers most of those cases far more cheaply than declining a
            # question the system could have answered.
            logger.warning("No tool call returned; retrying with an explicit instruction")
            selection = self._client.complete(
                [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "You must answer by calling one of the available "
                            "tools. Do not reply in prose. If the question "
                            "cannot be answered from the ticket data, call "
                            f"{QUERY_TOOL} with a SELECT that returns no rows."
                        ),
                    },
                ],
                tools=self._tools,
                force_tool=True,
                max_tokens=TOOL_CALL_MAX_TOKENS,
            )
            prompt_tokens += selection.prompt_tokens
            completion_tokens += selection.completion_tokens

        if not selection.tool_calls:
            # Two prose replies to an explicit instruction. Passing it through
            # would publish an ungrounded answer, which is precisely what this
            # pipeline exists to prevent, so the question is declined instead.
            logger.warning("Model returned no tool call for: %s", question)
            return self._declined(
                question,
                answer=selection.text or REFUSAL_MESSAGE,
                started=started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        call = selection.tool_calls[0]

        if call.name == ANOMALY_TOOL:
            return self._answer_with_anomalies(
                question,
                call,
                started=started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        if call.name == QUERY_TOOL:
            return self._answer_with_query(
                question,
                call,
                messages=messages,
                started=started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        logger.error("Model requested an unknown tool: %s", call.name)
        return self._declined(
            question,
            answer=REFUSAL_MESSAGE,
            started=started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _answer_with_query(
        self,
        question: str,
        call: ToolCall,
        *,
        messages: list[dict[str, Any]],
        started: float,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> QueryResult:
        """Validate, execute and narrate a generated SQL query.

        Grants exactly one repair attempt. If the first statement is rejected
        by the guard or fails to execute, the error is fed back and the model
        asked to correct it. A second failure is reported honestly rather than
        retried further - repeated attempts burn a per-minute token budget and
        rarely succeed where the first two did not.

        Args:
            question: The original question.
            call: The model's tool call.
            messages: The conversation so far, reused for the repair attempt.
            started: Monotonic start time of the pipeline.
            prompt_tokens: Input tokens consumed so far.
            completion_tokens: Generated tokens so far.

        Returns:
            The narrated answer, or an explanation of why none could be given.
        """
        sql = str(call.arguments.get("sql", "")).strip()
        rows: list[dict[str, Any]] = []
        safe_sql: str | None = None
        failure: str | None = None

        for attempt in (1, 2):
            try:
                safe_sql = validate_select(sql, max_rows=self._max_rows)
                rows = self._execute(safe_sql)
                failure = None
                break
            except QueryTimeoutError as exc:
                # Deliberately not repaired. A malformed query can be corrected;
                # a query that ran too long will run too long again, so a retry
                # spends a second model call to reach the same outcome - on a
                # budget measured per minute.
                logger.warning("Query aborted on timeout: %s", safe_sql)
                return self._declined(
                    question,
                    answer=str(exc),
                    started=started,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    sql=safe_sql,
                )
            except (SqlGuardError, sqlite3.Error) as exc:
                failure = str(exc)
                logger.info("SQL attempt %d rejected: %s", attempt, failure)

                if attempt == 2:
                    break

                repair = self._client.complete(
                    [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                f"That query failed: {failure}\n\n"
                                f"Failed SQL: {sql}\n\n"
                                "Call the tool again with a corrected, "
                                "read-only SELECT statement."
                            ),
                        },
                    ],
                    tools=self._tools,
                    force_tool=True,
                    max_tokens=TOOL_CALL_MAX_TOKENS,
                )
                prompt_tokens += repair.prompt_tokens
                completion_tokens += repair.completion_tokens

                if not repair.tool_calls:
                    break
                sql = str(repair.tool_calls[0].arguments.get("sql", "")).strip()

        if failure is not None:
            return self._declined(
                question,
                answer=(
                    "I could not turn that into a valid query against this "
                    f"dataset. The database reported: {failure}"
                ),
                started=started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                sql=safe_sql,
            )

        shown, truncated = truncate_rows(rows)
        evidence = render_rows(shown, total=len(rows), truncated=truncated)

        answer, narration = self._narrate(
            question,
            evidence=evidence,
            fallback=_describe_rows(rows),
            result_count=len(rows),
            rows=rows,
            truncated=truncated,
        )
        prompt_tokens += narration.prompt_tokens
        completion_tokens += narration.completion_tokens

        return QueryResult(
            question=question,
            answer=answer,
            tool=QUERY_TOOL,
            sql=safe_sql,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            anomaly_reports=None,
            as_of=self._as_of,
            elapsed_ms=_elapsed_ms(started),
            model=self._client.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _answer_with_anomalies(
        self,
        question: str,
        call: ToolCall,
        *,
        started: float,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> QueryResult:
        """Run the anomaly detectors and narrate their reports.

        Args:
            question: The original question.
            call: The model's tool call.
            started: Monotonic start time of the pipeline.
            prompt_tokens: Input tokens consumed so far.
            completion_tokens: Generated tokens so far.

        Returns:
            The narrated answer.
        """
        kind = call.arguments.get("kind")
        window_days = call.arguments.get("window_days")

        try:
            reports = detect_anomalies(
                load_frame(self._db_path),
                as_of=self._as_of,
                kinds=[kind] if kind else None,
                window_days=int(window_days) if window_days else None,
            )
        except (KeyError, ValueError, TypeError, sqlite3.Error) as exc:
            # The model chose arguments the detectors reject. Reported rather
            # than retried: the tool schema already constrains these values, so
            # a second attempt is unlikely to differ.
            logger.info("Anomaly arguments rejected: %s", exc)
            return self._declined(
                question,
                answer=f"I could not run that anomaly check: {exc}",
                started=started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        payloads = [report.to_dict() for report in reports]
        answer, narration = self._narrate(
            question,
            evidence=render_anomaly_reports(payloads),
            fallback=_describe_reports(payloads),
            result_count=sum(p["count"] for p in payloads),
            # No rows are passed: the reports already carry every figure the
            # model was shown - thresholds, counts and each ticket's value.
            reports=payloads,
        )
        prompt_tokens += narration.prompt_tokens
        completion_tokens += narration.completion_tokens

        flagged = [
            anomaly for payload in payloads for anomaly in payload["anomalies"]
        ]

        return QueryResult(
            question=question,
            answer=answer,
            tool=ANOMALY_TOOL,
            sql=None,
            rows=flagged,
            row_count=len(flagged),
            truncated=False,
            anomaly_reports=payloads,
            as_of=self._as_of,
            elapsed_ms=_elapsed_ms(started),
            model=self._client.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _narrate(
        self,
        question: str,
        *,
        evidence: str,
        fallback: str,
        result_count: int = 0,
        rows: list[dict[str, Any]] | None = None,
        reports: list[dict[str, Any]] | None = None,
        truncated: bool = False,
    ) -> tuple[str, ChatResponse]:
        """Ask the model to phrase an answer, degrading to a plain summary.

        By this point the expensive, meaningful work is already done: the SQL
        has run, or the detectors have. The remaining call only turns correct
        figures into a sentence. Letting a rate limit or an outage at that
        moment discard the result would throw away a correct answer because a
        cosmetic step failed - and on a tokens-per-minute free tier, a limit hit
        between two calls of the same question is entirely plausible.

        This is only defensible because of the core design rule: the model
        phrases answers, it never computes them. There is always a real result
        to fall back on.

        Args:
            question: The original question.
            evidence: Rendered tool output to narrate.
            fallback: A factual summary to use if the model is unavailable.

        Returns:
            An ``(answer, response)`` pair. On failure the response carries zero
            token usage, since no tokens were spent.
        """
        try:
            narration = self._client.complete(
                build_narration_messages(
                    question, evidence=evidence, as_of=self._as_of
                ),
                max_tokens=NARRATION_MAX_TOKENS,
            )
        except LlmError as exc:
            logger.warning(
                "Narration unavailable (%s); returning the computed result "
                "with a plain summary.",
                exc,
            )
            # Exception messages do not reliably end in punctuation, so one is
            # added rather than letting two sentences run together.
            reason = str(exc).rstrip(". ")
            return (
                f"{fallback} (A written summary was unavailable: {reason}. "
                "The figures are complete and correct.)",
                ChatResponse(),
            )

        text = (narration.text or "").strip()
        # An empty completion means the model produced nothing usable, which is
        # indistinguishable from an outage as far as the caller is concerned.
        if not text:
            return fallback, narration

        invented = ungrounded_numbers(
            text,
            rows=rows or [],
            reports=reports,
            question=question,
            row_count=result_count,
        )
        if invented:
            # A figure with no source in the evidence was not computed - it was
            # produced. The deterministic summary is known to be correct, so it
            # replaces the narration.
            #
            # This is what turns "the model never does arithmetic" from a claim
            # about the design into a property checked on every response.
            logger.warning(
                "Answer contained ungrounded figures %s; using the "
                "deterministic summary instead.",
                invented,
            )
            return fallback, narration

        if truncated and result_count > 0 and not _states_the_total(text, result_count):
            # The model listed a sample without saying so. Observed twice: 13
            # of 34 matching tickets named as though they were all of them.
            # The reader has no way to know the list is partial, so the total
            # is stated for them rather than left to chance.
            logger.info("Answer omitted the total for a truncated result")
            text = f"{result_count} tickets matched. {text}"

        if result_count > 0 and _claims_no_results(text):
            # The model has contradicted data already in hand. Observed against
            # the live model on a query returning 34 unresolved tickets: the
            # column was mostly NULL and it reported "No tickets matched."
            #
            # The prompt forbids this, but a prompt is a request rather than a
            # guarantee, and temperature=0 does not make compliance certain. The
            # deterministic summary is known to be correct, so it wins. A model
            # may phrase results; it may not overrule them.
            logger.warning(
                "Model claimed no results while holding %d rows; using the "
                "deterministic summary instead.",
                result_count,
            )
            return fallback, narration

        return text, narration

    def _execute(self, sql: str) -> list[dict[str, Any]]:
        """Run validated SQL against the read-only database.

        Args:
            sql: A statement already cleared by :func:`validate_select`.

        Returns:
            Result rows as plain dictionaries, ready to serialise.

        Raises:
            sqlite3.Error: If the statement is syntactically valid but wrong -
                an unknown column, for instance. Caught by the caller and fed
                back to the model as a repair opportunity.
        """
        with read_only_connection(self._db_path) as connection:
            return execute_select(connection, sql, max_rows=self._max_rows)

    def _declined(
        self,
        question: str,
        *,
        answer: str,
        started: float,
        prompt_tokens: int,
        completion_tokens: int,
        sql: str | None = None,
    ) -> QueryResult:
        """Build a result for a question that could not be answered.

        Returns a normal result rather than raising: "I could not answer that"
        is a legitimate outcome, and callers should not have to distinguish it
        from a transport failure, which is genuinely exceptional.

        Args:
            question: The original question.
            answer: Explanation to show the user.
            started: Monotonic start time of the pipeline.
            prompt_tokens: Input tokens consumed.
            completion_tokens: Generated tokens.
            sql: The offending SQL, when there was some.

        Returns:
            A result carrying the explanation and no data.
        """
        return QueryResult(
            question=question,
            answer=answer,
            tool=None,
            sql=sql,
            rows=[],
            row_count=0,
            truncated=False,
            anomaly_reports=None,
            as_of=self._as_of,
            elapsed_ms=_elapsed_ms(started),
            model=self._client.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


# Phrasings that assert an empty result. Matched only when rows were actually
# returned, so a genuinely empty result is never rewritten.
_EMPTY_CLAIM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(no|zero)\s+(tickets?|rows?|results?|records?|matches|data)\b"
    r"|\bnothing\s+(matched|found)\b"
    r"|\bnone\s+(matched|found)\b"
    r"|\bdid\s+not\s+match\b",
    re.IGNORECASE,
)


def _claims_no_results(text: str) -> bool:
    """Report whether an answer asserts that nothing was found.

    Args:
        text: The model's answer.

    Returns:
        ``True`` when the answer claims an empty result.
    """
    return bool(_EMPTY_CLAIM_PATTERN.search(text))


def _states_the_total(text: str, total: int) -> bool:
    """Report whether an answer mentions the full number of matching rows.

    Args:
        text: The model's answer.
        total: How many rows matched in full.

    Returns:
        ``True`` when the total appears in the answer.
    """
    return str(total) in text.replace(",", "")


def _describe_rows(rows: list[dict[str, Any]]) -> str:
    """Summarise query rows without a language model.

    Used when narration is unavailable. Deliberately plain: the goal is an
    accurate statement of what was found, not an imitation of the model's prose.

    Args:
        rows: The result set.

    Returns:
        A short factual description.
    """
    if not rows:
        return "No tickets matched that query."

    # A single cell is the shape of every count, sum and average, so it is
    # worth answering directly rather than reporting "1 row".
    if len(rows) == 1 and len(rows[0]) == 1:
        (column, value), = rows[0].items()
        return f"{column.replace('_', ' ')}: {value}"

    return f"{len(rows)} rows matched. The full result is included below."


def _describe_reports(reports: list[dict[str, Any]]) -> str:
    """Summarise anomaly reports without a language model.

    Args:
        reports: Serialised detector reports.

    Returns:
        A short factual description.
    """
    parts = [
        f"{report['description']}: {report['count']} of {report['considered']} "
        f"flagged (threshold {report['threshold']})"
        for report in reports
    ]
    return ". ".join(parts) + "."


def _elapsed_ms(started: float) -> int:
    """Return milliseconds elapsed since a monotonic start time.

    Args:
        started: A value previously returned by :func:`time.monotonic`.

    Returns:
        Elapsed milliseconds.
    """
    return int((time.monotonic() - started) * 1000)


def build_chat_client() -> ChatClient:
    """Construct the configured chat client.

    Returns:
        A client ready to answer questions.

    Raises:
        LlmNotConfiguredError: If no API key is configured. Raised here, at the
            point of use, rather than at import - so the deterministic parts of
            the application keep working without credentials.
    """
    if not settings.llm_enabled or settings.groq_api_key is None:
        raise LlmNotConfiguredError(
            "No GROQ_API_KEY is configured, so natural-language questions are "
            "unavailable. Anomaly detection and health checks still work. Set "
            "a free key from https://console.groq.com in your .env file."
        )

    return GroqChatClient(api_key=settings.groq_api_key, model=settings.groq_model)
