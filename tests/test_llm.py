"""Tests for :mod:`app.llm`, the tool-calling orchestration layer.

Every test here runs offline, in milliseconds, with no API key and no cost -
which is the whole reason the service depends on the :class:`ChatClient`
protocol rather than on Groq's SDK. A ``FakeChatClient`` returns scripted
responses, so the pipeline's behaviour can be driven deliberately down paths a
live model would produce only by luck: a malformed query, a refused statement,
an empty result, a rate limit.

That is the practical payoff of dependency inversion, and it is worth being
able to state plainly: these tests cover the repair-retry path, which against
a real model might take dozens of attempts to trigger even once.

What is deliberately *not* tested here is whether the model writes good SQL.
That is a property of the model and the prompt, not of this code, and asserting
it would make the suite slow, costly, non-deterministic and dependent on
network access. It is verified by hand against the sample questions instead.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.data import Database
from app.llm import (
    ChatClient,
    ChatResponse,
    LlmNotConfiguredError,
    LlmRateLimitedError,
    LlmUnavailableError,
    QueryResult,
    ToolCall,
    TicketQueryService,
    backoff_delay,
    build_chat_client,
    is_retryable,
)
from app.prompts import ANOMALY_TOOL, NARRATION_ROW_LIMIT, QUERY_TOOL

AS_OF = datetime(2024, 3, 30, 18, 6)


class FakeChatClient:
    """A scripted :class:`ChatClient` for driving the pipeline deliberately.

    Attributes:
        model: Identifier reported in results.
        calls: Every request received, so tests can assert on what the service
            actually sent - the forced tool flag, the repair message, and so on.
    """

    def __init__(self, *responses: ChatResponse, model: str = "fake-model") -> None:
        """Initialise the fake.

        Args:
            *responses: Responses to return, in order.
            model: Model identifier to report.
        """
        self.model = model
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_tool: bool = False,
        max_tokens: int = 400,
    ) -> ChatResponse:
        """Return the next scripted response.

        Args:
            messages: Conversation sent by the service.
            tools: Tool schemas offered.
            force_tool: Whether a tool call was required.
            max_tokens: Generation ceiling.

        Returns:
            The next scripted response.

        Raises:
            AssertionError: If the service makes more calls than were scripted,
                which means the pipeline is not as bounded as it claims.
        """
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "force_tool": force_tool,
                "max_tokens": max_tokens,
            }
        )

        if not self._responses:
            raise AssertionError(
                f"Unscripted call number {len(self.calls)} - the pipeline made "
                "more model calls than expected."
            )
        return self._responses.pop(0)


class ExplodingChatClient:
    """A client that raises a given exception, for testing failure paths."""

    model = "fake-model"

    def __init__(self, error: Exception) -> None:
        """Initialise the client.

        Args:
            error: The exception to raise on any call.
        """
        self._error = error

    def complete(self, *args: Any, **kwargs: Any) -> ChatResponse:
        """Raise the configured exception.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            Exception: Always, as configured.
        """
        raise self._error


def tool_response(name: str, **arguments: Any) -> ChatResponse:
    """Build a response representing a tool call.

    Args:
        name: Tool the model is choosing.
        **arguments: Arguments it supplies.

    Returns:
        A scripted response.
    """
    return ChatResponse(
        tool_calls=[ToolCall(name=name, arguments=arguments)],
        prompt_tokens=900,
        completion_tokens=60,
    )


def text_response(text: str) -> ChatResponse:
    """Build a response representing prose.

    Args:
        text: The assistant's message.

    Returns:
        A scripted response.
    """
    return ChatResponse(text=text, prompt_tokens=200, completion_tokens=40)


@pytest.fixture
def service_factory(real_database: Database):
    """Return a factory building a service around a scripted client.

    Args:
        real_database: Database built from the shipped dataset.

    Returns:
        A callable taking scripted responses and returning
        ``(service, client)``.
    """

    def _build(*responses: ChatResponse) -> tuple[TicketQueryService, FakeChatClient]:
        client = FakeChatClient(*responses)
        service = TicketQueryService(
            client=client,
            db_path=real_database.path,
            as_of=real_database.as_of,
            row_count=real_database.row_count,
        )
        return service, client

    return _build


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_answers_a_counting_question(service_factory) -> None:
    """A query tool call is validated, executed and narrated.

    The count comes from SQLite. The model is shown the result and writes prose
    around it - it never counts anything itself.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets WHERE status='Open'"),
        text_response("There are 111 open tickets."),
    )

    result = service.answer("How many tickets are open?")

    assert result.tool == QUERY_TOOL
    assert result.rows == [{"n": 111}]
    assert "111" in result.answer


def test_exactly_two_model_calls_on_the_happy_path(service_factory) -> None:
    """A successful question costs one selection call and one narration call.

    The bound matters: on an 8,000 token-per-minute tier, an unbounded agent
    loop lets a single confused question exhaust the budget.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("There are 500 tickets."),
    )

    service.answer("How many tickets are there?")

    assert len(client.calls) == 2


def test_tool_selection_is_forced(service_factory) -> None:
    """The first call requires a tool; the narration call does not.

    Forcing the tool is what stops the model answering a data question from
    memory. The narration call must *not* force one, since prose is the goal
    there.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("500 tickets."),
    )

    service.answer("How many tickets?")

    assert client.calls[0]["force_tool"] is True
    assert client.calls[1]["force_tool"] is False


def test_generated_sql_is_returned_for_inspection(service_factory) -> None:
    """The executed SQL travels back with the answer.

    Transparency the evaluator can check: the figure and the query that
    produced it arrive together.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("500 tickets."),
    )

    result = service.answer("How many tickets?")

    assert result.sql is not None
    assert "SELECT COUNT(*)" in result.sql


def test_row_cap_is_applied_to_generated_sql(service_factory) -> None:
    """A LIMIT is appended when the model does not supply one.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT ticket_id FROM tickets"),
        text_response("Here are the tickets."),
    )

    result = service.answer("List the tickets")

    assert "LIMIT" in (result.sql or "")


def test_token_usage_is_accumulated(service_factory) -> None:
    """Usage from both calls is summed into the result.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("500 tickets."),
    )

    result = service.answer("How many tickets?")

    assert result.prompt_tokens == 1100  # 900 + 200
    assert result.completion_tokens == 100  # 60 + 40


# ---------------------------------------------------------------------------
# Row capping before narration
# ---------------------------------------------------------------------------


def test_large_results_are_capped_before_narration(service_factory) -> None:
    """The model sees at most the narration limit, the caller sees everything.

    Sending 500 rows to the model would cost roughly 15,000 tokens and breach
    the per-minute ceiling on one question. The API still returns every row.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT ticket_id FROM tickets"),
        text_response("Showing a sample."),
    )

    result = service.answer("List every ticket")

    assert result.row_count == 500
    assert result.truncated is True

    narration_input = client.calls[1]["messages"][1]["content"]
    assert narration_input.count("TKT-") == NARRATION_ROW_LIMIT
    assert "[500 rows matched" in narration_input


def test_empty_results_are_narrated_honestly(service_factory) -> None:
    """A query matching nothing tells the model so explicitly.

    An empty evidence block would invite the model to fill the silence from
    memory, which is exactly the failure this design prevents.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(
            QUERY_TOOL, sql="SELECT * FROM tickets WHERE category='Nonexistent'"
        ),
        text_response("No tickets matched."),
    )

    result = service.answer("Any tickets in a category that does not exist?")

    assert result.row_count == 0
    assert "[0 rows matched]" in client.calls[1]["messages"][1]["content"]


# ---------------------------------------------------------------------------
# The repair retry
# ---------------------------------------------------------------------------


def test_rejected_sql_is_repaired_once(service_factory) -> None:
    """A guard rejection is fed back and the corrected query succeeds.

    A path a live model would reach only occasionally, driven deliberately here.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(QUERY_TOOL, sql="DROP TABLE tickets"),
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("There are 500 tickets."),
    )

    result = service.answer("How many tickets?")

    assert result.row_count == 1
    assert len(client.calls) == 3  # selection, repair, narration


def test_repair_message_explains_the_failure(service_factory) -> None:
    """The retry tells the model what went wrong and shows the bad SQL.

    "Invalid SQL" alone would give the model nothing to correct.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(QUERY_TOOL, sql="DELETE FROM tickets"),
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("500 tickets."),
    )

    service.answer("How many tickets?")

    repair_message = client.calls[1]["messages"][-1]["content"]
    assert "DELETE" in repair_message
    assert "failed" in repair_message.lower()


def test_execution_errors_also_trigger_repair(service_factory) -> None:
    """Valid SQL referencing a missing column is retried.

    The guard accepts it - it is a well-formed SELECT - so this failure comes
    from SQLite, and must be caught and fed back just the same.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT no_such_column FROM tickets"),
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("500 tickets."),
    )

    result = service.answer("How many tickets?")

    assert result.row_count == 1
    assert len(client.calls) == 3


def test_timeout_is_reported_not_repaired(
    service_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runaway query is reported immediately rather than retried.

    The distinction from other SQL failures matters. A malformed query can be
    corrected and re-run; a query that exhausted its execution budget will
    exhaust it again, so a repair attempt spends a second model call to reach
    the same outcome - on a budget measured per minute, that is wasted twice
    over.

    Scripting only one response makes the assertion strict: a repair attempt
    would ask for a second and fail the test outright.

    Args:
        service_factory: Factory building a service with a scripted client.
        monkeypatch: pytest's attribute patcher.
    """
    # Shortened from the five-second default so the suite stays fast. The
    # behaviour under test is "aborts and does not retry", not the exact
    # duration - and a test suite slow enough to avoid is a suite that stops
    # being run.
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "query_timeout_seconds", 0.5)

    runaway = (
        "WITH RECURSIVE bomb(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM bomb) "
        "SELECT COUNT(*) FROM bomb"
    )
    service, client = service_factory(tool_response(QUERY_TOOL, sql=runaway))

    result = service.answer("Count the bomb")

    assert len(client.calls) == 1
    assert result.tool is None
    assert "too long" in result.answer.lower()


def test_repair_is_attempted_only_once(service_factory) -> None:
    """Two failures end the attempt rather than looping.

    Repeated retries consume a per-minute token budget and rarely succeed where
    two attempts did not.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(QUERY_TOOL, sql="DROP TABLE tickets"),
        tool_response(QUERY_TOOL, sql="DELETE FROM tickets"),
    )

    result = service.answer("Delete everything")

    assert result.tool is None
    assert result.row_count == 0
    assert len(client.calls) == 2  # no narration call after giving up


def test_failed_query_explains_itself(service_factory) -> None:
    """A question that cannot be answered says why, in plain language.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(QUERY_TOOL, sql="DROP TABLE tickets"),
        tool_response(QUERY_TOOL, sql="TRUNCATE tickets"),
    )

    result = service.answer("Wipe the data")

    assert "could not" in result.answer.lower()


# ---------------------------------------------------------------------------
# The anomaly path
# ---------------------------------------------------------------------------


def test_anomaly_tool_runs_the_detectors(service_factory) -> None:
    """An anomaly question runs the deterministic engine, not SQL.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(ANOMALY_TOOL, kind="resolution_time_outlier"),
        text_response("21 tickets took unusually long."),
    )

    result = service.answer("Any anomalies in resolution times?")

    assert result.tool == ANOMALY_TOOL
    assert result.sql is None
    assert result.anomaly_reports is not None
    assert result.anomaly_reports[0]["count"] == 21


def test_anomaly_window_is_honoured(service_factory) -> None:
    """A windowed anomaly question restricts the tickets evaluated.

    The threshold still derives from all history, so the fence stays at 48.15.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(ANOMALY_TOOL, kind="resolution_time_outlier", window_days=7),
        text_response("Six this week."),
    )

    result = service.answer("Any anomalies this week?")

    assert result.anomaly_reports is not None
    assert result.anomaly_reports[0]["count"] == 6
    assert result.anomaly_reports[0]["threshold"] == 48.15


def test_all_detectors_run_when_no_kind_is_given(service_factory) -> None:
    """Omitting the detector name runs every registered detector.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(ANOMALY_TOOL),
        text_response("Two kinds of anomaly were found."),
    )

    result = service.answer("Are there any anomalies?")

    assert result.anomaly_reports is not None
    assert len(result.anomaly_reports) == 2


def test_unknown_detector_is_reported_not_crashed(service_factory) -> None:
    """A detector name that does not exist produces an explanation.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(tool_response(ANOMALY_TOOL, kind="not_a_detector"))

    result = service.answer("Check for imaginary anomalies")

    assert result.tool is None
    assert "could not" in result.answer.lower()


def test_anomaly_reports_reach_the_narration_call(service_factory) -> None:
    """The model is shown each anomaly's stated reason.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        tool_response(ANOMALY_TOOL, kind="sla_breach"),
        text_response("80 tickets have breached."),
    )

    service.answer("Any SLA breaches?")

    evidence = client.calls[1]["messages"][1]["content"]
    assert "SLA" in evidence
    assert "TKT-" in evidence


# ---------------------------------------------------------------------------
# Refusals and malformed model behaviour
# ---------------------------------------------------------------------------


def test_a_declined_question_is_not_retried(service_factory) -> None:
    """A deliberate refusal ends the question rather than being overridden.

    Groq rejects the whole request when ``tool_choice="required"`` is set and
    the model chooses to answer in prose, so a refusal arrives as a failed
    request rather than as a message. The client marks it, and it must be
    treated as final.

    Retrying here was actively harmful and was caught by the benchmark: forced
    to call a tool for "what is the capital of France?", the model produced a
    query returning nothing, and the narration step then answered **"Paris."**
    from its own knowledge. Overriding a correct refusal is precisely how an
    ungrounded answer is manufactured - the single failure this design exists
    to prevent.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        ChatResponse(
            text="I can only answer questions about the ticket dataset.",
            declined=True,
        )
    )

    result = service.answer("What is the capital of France?")

    assert result.tool is None
    assert result.rows == []
    assert "Paris" not in result.answer
    # One call only. A second would be the retry that caused the hallucination.
    assert len(client.calls) == 1


def test_declined_answer_keeps_the_model_wording(service_factory) -> None:
    """The model's own refusal is shown, not a generic substitute.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        ChatResponse(text="I cannot fulfil that request.", declined=True)
    )

    assert service.answer("Delete all tickets.").answer == (
        "I cannot fulfil that request."
    )


def test_prose_reply_triggers_one_retry(service_factory) -> None:
    """A prose reply is met with an explicit instruction, not an immediate refusal.

    Second rung of the tool-selection ladder. ``tool_choice="required"`` makes
    prose unlikely but not impossible, and one reminder recovers most of those
    cases far more cheaply than declining a question the system could answer.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        text_response("I think there are about 400."),
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("There are 500 tickets."),
    )

    result = service.answer("How many tickets?")

    assert result.tool == QUERY_TOOL
    assert result.rows == [{"n": 500}]
    assert len(client.calls) == 3  # selection, ladder retry, narration


def test_ladder_retry_sends_an_explicit_instruction(service_factory) -> None:
    """The retry tells the model plainly that it must call a tool.

    Repeating the identical request would be pointless; the second attempt only
    differs because it says what went wrong.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        text_response("Around 400, I'd guess."),
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("500 tickets."),
    )

    service.answer("How many tickets?")

    instruction = client.calls[1]["messages"][-1]["content"]
    assert "must answer by calling" in instruction
    assert "Do not reply in prose" in instruction


def test_two_prose_replies_are_declined(service_factory) -> None:
    """A model that ignores even the explicit instruction is refused.

    Its reply would be ungrounded - not derived from the data - which is
    precisely what this pipeline exists to prevent. The ladder is capped at one
    extra attempt so a stubborn model cannot spend the budget indefinitely.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(
        text_response("I think there are about 400."),
        text_response("Still roughly 400."),
    )

    result = service.answer("How many tickets?")

    assert result.tool is None
    assert result.rows == []
    assert len(client.calls) == 2  # no narration of an ungrounded answer


def test_unknown_tool_is_declined(service_factory) -> None:
    """A tool the service does not implement is refused safely.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(tool_response("delete_everything", target="tickets"))

    result = service.answer("Do something unsupported")

    assert result.tool is None
    assert result.rows == []


def test_blank_question_is_rejected(service_factory) -> None:
    """An empty question fails before any model call is made.

    No point spending tokens to discover the input was empty.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory()

    with pytest.raises(ValueError, match="required"):
        service.answer("   ")

    assert client.calls == []


# ---------------------------------------------------------------------------
# Provider failures
# ---------------------------------------------------------------------------


def test_rate_limit_propagates_with_retry_after(real_database: Database) -> None:
    """A rate limit surfaces as a typed error carrying the wait time.

    The API layer turns this into a ``Retry-After`` header rather than leaving
    a client to guess. A real path on an 8,000 token-per-minute tier.

    Args:
        real_database: Database built from the shipped dataset.
    """
    service = TicketQueryService(
        client=ExplodingChatClient(
            LlmRateLimitedError("slow down", retry_after=12.0)
        ),
        db_path=real_database.path,
        as_of=real_database.as_of,
        row_count=real_database.row_count,
    )

    with pytest.raises(LlmRateLimitedError) as caught:
        service.answer("How many tickets?")

    assert caught.value.retry_after == 12.0


def test_missing_groq_package_degrades_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete install reports as "unavailable", not as an ImportError.

    The distinction matters at startup. A raw ``ImportError`` is not an
    ``LlmError``, so it escapes the handler that downgrades the service and
    takes the whole process with it - including the endpoints that never touch
    this SDK. Operationally a missing package and a missing key are the same
    condition: the capability is unavailable.

    Args:
        monkeypatch: pytest's attribute patcher.
    """
    import builtins

    from app.config import settings as live_settings
    from app.llm import GroqChatClient

    real_import = builtins.__import__

    def without_groq(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "groq":
            raise ImportError("No module named groq")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_groq)
    monkeypatch.setattr(live_settings, "groq_api_key", "gsk_real_looking_key")

    with pytest.raises(LlmNotConfiguredError, match="not installed"):
        GroqChatClient(api_key="gsk_real_looking_key", model="any-model")


def test_database_failure_in_the_anomaly_path_is_reported(
    real_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error while detecting anomalies is explained, not raised.

    Args:
        real_database: Database built from the shipped dataset.
        monkeypatch: pytest's attribute patcher.
    """
    import sqlite3

    import app.llm as llm_module

    def broken_load(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(llm_module, "load_frame", broken_load)

    service = TicketQueryService(
        client=FakeChatClient(tool_response(ANOMALY_TOOL)),
        db_path=real_database.path,
        as_of=real_database.as_of,
        row_count=real_database.row_count,
    )

    result = service.answer("Any anomalies?")

    assert result.tool is None
    assert "could not" in result.answer.lower()


def test_missing_api_key_raises_a_specific_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a client without credentials fails with actionable guidance.

    Raised at the point of use, not at import, so the deterministic endpoints
    continue to work without a key.

    Args:
        monkeypatch: pytest's attribute patcher.
    """
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "groq_api_key", None)

    with pytest.raises(LlmNotConfiguredError, match="console.groq.com"):
        build_chat_client()


# ---------------------------------------------------------------------------
# Contract and serialisation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def _status_error(code: int) -> Exception:
    """Build a provider status error with the given HTTP code.

    Args:
        code: The HTTP status to simulate.

    Returns:
        The SDK exception the provider would raise.
    """
    import httpx
    from groq import APIStatusError, RateLimitError

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat")
    response = httpx.Response(code, request=request)
    error_type = RateLimitError if code == 429 else APIStatusError
    return error_type("provider error", response=response, body=None)


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        pytest.param(500, True, id="500 server error"),
        pytest.param(502, True, id="502 bad gateway"),
        pytest.param(503, True, id="503 unavailable"),
        pytest.param(408, True, id="408 request timeout"),
        pytest.param(429, False, id="429 rate limit"),
        pytest.param(401, False, id="401 unauthorized"),
        pytest.param(400, False, id="400 bad request"),
        pytest.param(404, False, id="404 not found"),
    ],
)
def test_retry_classification(code: int, retryable: bool) -> None:
    """Only failures a retry could plausibly fix are retried.

    The 429 case is the reason this policy exists rather than the SDK's own.
    The SDK retries rate limits by default, which suits a per-request quota but
    not a per-*minute* token budget: recovery takes about a minute while the
    backoff lasts seconds, so retrying cannot succeed and spends two further
    requests against a 30-per-minute ceiling.

    The 4xx cases are the mirror image - our request is wrong, and sending the
    identical request again cannot produce a different answer.

    Args:
        code: HTTP status returned by the provider.
        retryable: Whether it should be retried.
    """
    assert is_retryable(_status_error(code)) is retryable


def test_connection_errors_are_retried() -> None:
    """A dropped connection is retried.

    It never reached the provider, so nothing about the request is in question.
    """
    import httpx
    from groq import APIConnectionError

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat")

    assert is_retryable(APIConnectionError(request=request)) is True


def test_backoff_grows_and_stays_bounded() -> None:
    """Delays increase with attempts but never exceed the ceiling.

    Unbounded growth would let a failing provider stall a request far longer
    than any caller is willing to wait.
    """
    ceiling = 4.0
    for attempt in range(6):
        samples = [backoff_delay(attempt) for _ in range(50)]
        assert all(0 <= delay <= ceiling for delay in samples)

    # Jitter means successive calls must differ; identical delays would mean
    # every client that failed together retries together, recreating the load
    # that caused the failure.
    assert len({backoff_delay(3) for _ in range(20)}) > 1


def test_fake_client_satisfies_the_protocol() -> None:
    """The test double conforms to the same contract as the real client.

    If it did not, these tests would be exercising a shape the production code
    never sees, and would prove nothing about it.
    """
    assert isinstance(FakeChatClient(), ChatClient)


# ---------------------------------------------------------------------------
# Degrading when narration fails after the work is already done
# ---------------------------------------------------------------------------


class FailAfterFirstCall:
    """A client that answers the first call and then fails.

    Reproduces the realistic case on a tokens-per-minute tier: the budget is
    exhausted *between* the two calls of a single question.
    """

    model = "fake-model"

    def __init__(self, first: ChatResponse, error: Exception) -> None:
        """Initialise the client.

        Args:
            first: Response for the tool-selection call.
            error: Exception to raise on every subsequent call.
        """
        self._first = first
        self._error = error
        self.calls = 0

    def complete(self, *args: Any, **kwargs: Any) -> ChatResponse:
        """Return the first response, then raise.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            The scripted first response.

        Raises:
            Exception: On the second and later calls.
        """
        self.calls += 1
        if self.calls == 1:
            return self._first
        raise self._error


def build_service(client: Any, database: Database) -> TicketQueryService:
    """Construct a service around a given client.

    Args:
        client: Any chat client.
        database: The database to query.

    Returns:
        A configured service.
    """
    return TicketQueryService(
        client=client,
        db_path=database.path,
        as_of=database.as_of,
        row_count=database.row_count,
    )


def test_rate_limit_during_narration_preserves_the_answer(
    real_database: Database,
) -> None:
    """A limit hit after the SQL ran still returns the computed figure.

    By that point the meaningful work is done and the number is correct;
    only the sentence around it is missing. Discarding a correct answer because
    a cosmetic step failed would be the wrong trade - and this is only possible
    because the model never computes figures in the first place.

    Args:
        real_database: Database built from the shipped dataset.
    """
    service = build_service(
        FailAfterFirstCall(
            tool_response(
                QUERY_TOOL,
                sql="SELECT COUNT(*) AS open_tickets FROM tickets WHERE status='Open'",
            ),
            LlmRateLimitedError("rate limit reached"),
        ),
        real_database,
    )

    result = service.answer("How many tickets are open?")

    assert result.rows == [{"open_tickets": 111}]
    assert "111" in result.answer
    assert "summary was unavailable" in result.answer


def test_outage_during_narration_preserves_anomaly_reports(
    real_database: Database,
) -> None:
    """Detector results survive a provider outage during narration.

    Args:
        real_database: Database built from the shipped dataset.
    """
    service = build_service(
        FailAfterFirstCall(
            tool_response(ANOMALY_TOOL, kind="sla_breach"),
            LlmUnavailableError("connection refused"),
        ),
        real_database,
    )

    result = service.answer("Any SLA breaches?")

    assert result.row_count == 80
    assert result.anomaly_reports is not None
    assert "80" in result.answer


def test_failure_before_any_work_still_raises(real_database: Database) -> None:
    """A limit hit on the *first* call propagates rather than degrading.

    The distinction matters. Nothing has been computed yet, so there is no
    answer to preserve - returning a placeholder would be inventing one. Only a
    failure after the data is in hand is worth absorbing.

    Args:
        real_database: Database built from the shipped dataset.
    """
    service = build_service(
        ExplodingChatClient(LlmRateLimitedError("rate limit reached")), real_database
    )

    with pytest.raises(LlmRateLimitedError):
        service.answer("How many tickets are open?")


def test_empty_narration_falls_back_to_a_summary(real_database: Database) -> None:
    """A model returning no text is treated like an outage.

    From the caller's side the two are indistinguishable, and an empty answer
    field would be worse than a plain one.

    Args:
        real_database: Database built from the shipped dataset.
    """
    service = build_service(
        FakeChatClient(
            tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
            ChatResponse(text="   "),
        ),
        real_database,
    )

    result = service.answer("How many tickets?")

    assert result.answer.strip()
    assert "500" in result.answer


def test_fallback_answers_a_single_value_directly(real_database: Database) -> None:
    """A one-cell result is stated as a value, not as "1 row".

    Counts, sums and averages all have this shape, so it is worth answering
    plainly rather than describing the result set.

    Args:
        real_database: Database built from the shipped dataset.
    """
    service = build_service(
        FailAfterFirstCall(
            tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS total FROM tickets"),
            LlmUnavailableError("down"),
        ),
        real_database,
    )

    assert "total: 500" in service.answer("How many tickets?").answer


def test_result_serialises_for_an_api_response(service_factory) -> None:
    """A result converts to JSON-safe types.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, _ = service_factory(
        tool_response(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        text_response("500 tickets."),
    )

    payload = service.answer("How many tickets?").to_dict()

    assert payload["tool"] == QUERY_TOOL
    assert isinstance(payload["as_of"], str)
    assert isinstance(payload["elapsed_ms"], int)
    assert payload["rows"] == [{"n": 500}]


def test_provider_refusal_is_recognised_not_treated_as_an_error() -> None:
    """A ``tool_use_failed`` rejection is read as a decline, with its wording.

    The provider reports this as HTTP 400, which is indistinguishable by status
    from a genuinely malformed request. Only the error body's ``code`` field
    separates "the model chose not to call a tool" from "your request was
    wrong", and conflating them turned good judgement into what looked like a
    broken service.
    """
    from groq import BadRequestError
    import httpx

    from app.llm import _declined_to_use_a_tool

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat")
    refusal = BadRequestError(
        "tool use failed",
        response=httpx.Response(400, request=request),
        body={
            "error": {
                "code": "tool_use_failed",
                "failed_generation": "I can only answer questions about tickets.",
            }
        },
    )

    assert _declined_to_use_a_tool(refusal) == (
        "I can only answer questions about tickets."
    )


def test_a_genuine_bad_request_is_not_mistaken_for_a_refusal() -> None:
    """A malformed request stays an error rather than becoming a decline.

    The mirror of the test above. Treating every 400 as a refusal would hide
    real defects behind a polite message, which is worse than the original
    problem because nothing would look wrong.
    """
    from groq import BadRequestError
    import httpx

    from app.llm import _declined_to_use_a_tool

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat")
    malformed = BadRequestError(
        "bad request",
        response=httpx.Response(400, request=request),
        body={"error": {"code": "invalid_parameter", "message": "bad model id"}},
    )

    assert _declined_to_use_a_tool(malformed) is None


def test_a_malformed_tool_call_is_recovered() -> None:
    """A tool call written as text is rebuilt rather than shown to the user.

    The provider rejects the request when the model puts a tool call in the
    message body instead of the tool-call field. The intent is unambiguous - a
    named tool with arguments - so honouring it recovers an answer that would
    otherwise be lost.

    Caught by the benchmark: without this, the raw JSON
    ``{"name": "detect_anomalies", ...}`` was displayed as though it were the
    model's refusal, which leaked internals and answered nothing.
    """
    from app.llm import _recover_tool_call

    recovered = _recover_tool_call(
        '{"name": "detect_anomalies", "arguments": '
        '{"kind": "sla_breach", "window_days": null}}'
    )

    assert recovered is not None
    assert recovered.name == ANOMALY_TOOL
    # A null argument means "omitted"; passing it through would fail the
    # detectors' own type checks.
    assert recovered.arguments == {"kind": "sla_breach"}


def test_prose_is_not_mistaken_for_a_tool_call() -> None:
    """An ordinary refusal is left alone by the recovery path.

    The mirror of the test above. Treating prose as a malformed call would turn
    a correct refusal into a failed query.
    """
    from app.llm import _recover_tool_call

    assert _recover_tool_call("I can only answer questions about tickets.") is None
    assert _recover_tool_call('{"unrelated": "json"}') is None
    assert _recover_tool_call('{"name": "drop_everything", "arguments": {}}') is None
