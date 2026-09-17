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
    QueryResult,
    ToolCall,
    TicketQueryService,
    build_chat_client,
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


def test_prose_reply_is_not_passed_off_as_an_answer(service_factory) -> None:
    """A model that ignores the forced tool call is declined, not trusted.

    Its reply would be ungrounded - not derived from the data - which is
    precisely what this pipeline exists to prevent.

    Args:
        service_factory: Factory building a service with a scripted client.
    """
    service, client = service_factory(text_response("I think there are about 400."))

    result = service.answer("How many tickets?")

    assert result.tool is None
    assert result.rows == []
    assert len(client.calls) == 1  # no narration of an ungrounded answer


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


def test_fake_client_satisfies_the_protocol() -> None:
    """The test double conforms to the same contract as the real client.

    If it did not, these tests would be exercising a shape the production code
    never sees, and would prove nothing about it.
    """
    assert isinstance(FakeChatClient(), ChatClient)


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
