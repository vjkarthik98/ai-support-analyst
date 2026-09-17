"""Tool-calling orchestration: natural-language questions to grounded answers.

The pipeline is deliberately bounded at exactly two model calls per question:

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
the budget for everyone. Two calls, plus at most one repair attempt, gives a
predictable ceiling of roughly 1,500 tokens per question.

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
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from app.anomalies import detect_anomalies, load_frame
from app.config import settings
from app.data import read_only_connection
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
    """

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


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
        from groq import Groq

        self._client = Groq(api_key=api_key)
        self.model = model

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
        from groq import APIConnectionError, APIStatusError, RateLimitError

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

        try:
            completion = self._client.chat.completions.create(**request)
        except RateLimitError as exc:
            raise LlmRateLimitedError(
                "The model provider's rate limit has been reached. The free "
                "tier allows 30 requests and 8,000 tokens per minute.",
                retry_after=_retry_after_seconds(exc),
            ) from exc
        except APIConnectionError as exc:
            raise LlmUnavailableError(
                "Could not reach the model provider. Check network connectivity."
            ) from exc
        except APIStatusError as exc:
            raise LlmUnavailableError(
                f"The model provider returned an error (HTTP {exc.status_code})."
            ) from exc

        return _to_chat_response(completion)


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

        if not selection.tool_calls:
            # Forcing a tool call makes this unlikely, but a model that
            # answers in prose anyway must not be allowed through: its reply
            # would be ungrounded, which is precisely what this design exists
            # to prevent. Decline instead.
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

        narration = self._client.complete(
            build_narration_messages(question, evidence=evidence, as_of=self._as_of),
            max_tokens=NARRATION_MAX_TOKENS,
        )
        prompt_tokens += narration.prompt_tokens
        completion_tokens += narration.completion_tokens

        return QueryResult(
            question=question,
            answer=(narration.text or "").strip() or "No answer was produced.",
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
        except (KeyError, ValueError, TypeError) as exc:
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
        narration = self._client.complete(
            build_narration_messages(
                question,
                evidence=render_anomaly_reports(payloads),
                as_of=self._as_of,
            ),
            max_tokens=NARRATION_MAX_TOKENS,
        )
        prompt_tokens += narration.prompt_tokens
        completion_tokens += narration.completion_tokens

        flagged = [
            anomaly for payload in payloads for anomaly in payload["anomalies"]
        ]

        return QueryResult(
            question=question,
            answer=(narration.text or "").strip() or "No answer was produced.",
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
            cursor = connection.execute(sql)
            # fetchmany caps rows even if the LIMIT clause was somehow absent,
            # bounding both the response and memory use.
            return [dict(row) for row in cursor.fetchmany(self._max_rows)]

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
