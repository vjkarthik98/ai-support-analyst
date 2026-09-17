"""Tests for :mod:`app.main`, the HTTP layer.

Two things are under test here, and neither is "does the data layer work" -
that is already covered by the modules' own suites. What matters at this
boundary is:

**The contract.** Status codes, response shapes and headers are what a client
programs against. A caller must be able to distinguish "wait and retry" (429)
from "retry now" (502) from "this will never work until someone configures a
key" (503). Returning 500 for all three would be technically a failure report
and practically useless.

**Graceful degradation.** The deterministic endpoints must serve with no API
key configured. This is asserted directly, because it is a promise made in the
README and demonstrated during the walkthrough - and because nothing in the
code structurally prevents someone re-introducing a hard dependency on the
language model at startup.

Every test runs offline. The language model is replaced with a scripted fake,
so ``/query`` can be exercised - including its failure paths - without network
access, credentials or cost.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.llm import (
    ChatResponse,
    LlmRateLimitedError,
    LlmUnavailableError,
    TicketQueryService,
    ToolCall,
)
from app.main import app
from app.prompts import ANOMALY_TOOL, QUERY_TOOL


class ScriptedClient:
    """A chat client returning predetermined responses.

    Attributes:
        model: Identifier reported in responses.
    """

    model = "fake-model"

    def __init__(self, *responses: ChatResponse) -> None:
        """Initialise the client.

        Args:
            *responses: Responses to return, in order.
        """
        self._responses = list(responses)

    def complete(self, *args: Any, **kwargs: Any) -> ChatResponse:
        """Return the next scripted response.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            The next scripted response.
        """
        return self._responses.pop(0)


class FailingClient:
    """A chat client that always raises, for exercising error handlers."""

    model = "fake-model"

    def __init__(self, error: Exception) -> None:
        """Initialise the client.

        Args:
            error: The exception to raise.
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


def tool_call(name: str, **arguments: Any) -> ChatResponse:
    """Build a response representing a tool call.

    Args:
        name: Tool being chosen.
        **arguments: Arguments supplied.

    Returns:
        A scripted response.
    """
    return ChatResponse(
        tool_calls=[ToolCall(name=name, arguments=arguments)],
        prompt_tokens=900,
        completion_tokens=60,
    )


def prose(text: str) -> ChatResponse:
    """Build a response representing narration.

    Args:
        text: The assistant's message.

    Returns:
        A scripted response.
    """
    return ChatResponse(text=text, prompt_tokens=200, completion_tokens=40)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a client for an application with no language model configured.

    Startup still succeeds - that is the point. Used for every deterministic
    endpoint, and to assert that ``/query`` degrades rather than crashes.

    Yields:
        A configured test client.
    """
    with TestClient(app) as test_client:
        test_client.app.state.query_service = None
        yield test_client


@pytest.fixture
def unguarded_client() -> Iterator[TestClient]:
    """Yield a client that returns 500 responses instead of re-raising.

    ``TestClient`` re-raises unhandled server exceptions by default, which is
    usually helpful - a test failure shows the real traceback. But it makes the
    catch-all handler untestable, because the exception never reaches it.
    Disabling that is the only way to assert on what a real HTTP client would
    actually receive.

    Yields:
        A test client that surfaces server errors as responses.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def llm_client() -> Iterator[TestClient]:
    """Yield a client whose query service uses a scripted fake model.

    Yields:
        A test client. Individual tests replace the scripted responses by
        assigning to ``app.state.query_service``.
    """
    with TestClient(app) as test_client:
        yield test_client


def with_model(test_client: TestClient, *responses: ChatResponse) -> None:
    """Attach a scripted query service to a running application.

    Args:
        test_client: The client whose app should be reconfigured.
        *responses: Responses the fake model will return, in order.
    """
    database = test_client.app.state.database
    test_client.app.state.query_service = TicketQueryService(
        client=ScriptedClient(*responses),
        db_path=database.path,
        as_of=database.as_of,
        row_count=database.row_count,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_reports_ready(client: TestClient) -> None:
    """The service reports itself ready with the dataset loaded.

    Args:
        client: Test client with no language model configured.
    """
    payload = client.get("/health").json()

    assert payload["status"] == "ok"
    assert payload["dataset_rows"] == 500
    assert payload["version"] == __version__


def test_health_reports_the_time_anchor(client: TestClient) -> None:
    """The anchor is surfaced so an operator can confirm it.

    Relative questions resolve against this value rather than the wall clock.
    If it were wrong, "this week" would silently match nothing, so it is worth
    being visible rather than buried in configuration.

    Args:
        client: Test client with no language model configured.
    """
    assert client.get("/health").json()["as_of"] == "2024-03-30 18:06:00"


def test_health_reports_degraded_mode(client: TestClient) -> None:
    """Health distinguishes "no API key" from "broken".

    Args:
        client: Test client with no language model configured.
    """
    assert client.get("/health").json()["llm_configured"] is False


# ---------------------------------------------------------------------------
# Graceful degradation - the promise this project makes
# ---------------------------------------------------------------------------


def test_anomalies_work_without_an_api_key(client: TestClient) -> None:
    """The statistical endpoint serves with no credentials configured.

    The central claim of the architecture: anomaly detection is deterministic
    and involves no model, so it must not depend on one being available.

    Args:
        client: Test client with no language model configured.
    """
    response = client.get("/anomalies")

    assert response.status_code == 200
    assert response.json()["total_anomalies"] == 101


def test_schema_works_without_an_api_key(client: TestClient) -> None:
    """The schema endpoint serves with no credentials configured.

    Args:
        client: Test client with no language model configured.
    """
    assert client.get("/schema").status_code == 200


def test_query_reports_503_without_a_key(client: TestClient) -> None:
    """A question with no key configured returns 503, not 500.

    Nothing has failed: the service is running correctly and its other
    endpoints are serving. One capability is unavailable until an operator
    supplies a credential, and 503 says exactly that.

    Args:
        client: Test client with no language model configured.
    """
    response = client.post("/query", json={"question": "How many tickets?"})

    assert response.status_code == 503
    assert response.json()["error"] == "not_configured"


def test_missing_key_message_is_actionable(client: TestClient) -> None:
    """The 503 explains how to fix it, not merely that it is broken.

    Args:
        client: Test client with no language model configured.
    """
    detail = client.post("/query", json={"question": "How many?"}).json()["detail"]

    assert "console.groq.com" in detail
    assert "GROQ_API_KEY" in detail


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def test_anomaly_reports_carry_method_and_threshold(client: TestClient) -> None:
    """Each report states what was checked and against which boundary.

    Args:
        client: Test client with no language model configured.
    """
    reports = client.get("/anomalies").json()["reports"]
    outliers = next(r for r in reports if r["kind"] == "resolution_time_outlier")

    assert outliers["threshold"] == 48.15
    assert outliers["count"] == 21
    assert "IQR" in outliers["method"]


def test_anomaly_window_narrows_results(client: TestClient) -> None:
    """A time window restricts which tickets are evaluated.

    Args:
        client: Test client with no language model configured.
    """
    payload = client.get("/anomalies", params={"window_days": 7}).json()
    outliers = next(
        r for r in payload["reports"] if r["kind"] == "resolution_time_outlier"
    )

    assert outliers["count"] == 6


def test_threshold_survives_windowing_over_http(client: TestClient) -> None:
    """The fence is identical whether or not a window is applied.

    Regression cover at the API boundary for a defect fixed in the detector:
    recomputing quartiles inside a quiet week raised the fence from 48.15 to
    80.45 hours and would have excused genuinely slow resolutions.

    Args:
        client: Test client with no language model configured.
    """
    thresholds = set()
    for params in ({}, {"window_days": 7}, {"window_days": 30}):
        payload = client.get("/anomalies", params=params).json()
        thresholds.add(
            next(
                r["threshold"]
                for r in payload["reports"]
                if r["kind"] == "resolution_time_outlier"
            )
        )

    assert thresholds == {48.15}


def test_single_detector_can_be_selected(client: TestClient) -> None:
    """Requesting one detector runs only that one.

    Args:
        client: Test client with no language model configured.
    """
    payload = client.get("/anomalies", params={"kind": "sla_breach"}).json()

    assert [r["kind"] for r in payload["reports"]] == ["sla_breach"]


def test_unknown_detector_returns_422_naming_the_options(client: TestClient) -> None:
    """An invalid detector name is a client error that lists the valid ones.

    Refusing without naming the alternatives leaves the caller guessing.

    Args:
        client: Test client with no language model configured.
    """
    response = client.get("/anomalies", params={"kind": "does_not_exist"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "sla_breach" in detail
    # The message must read cleanly, without KeyError's repr quoting.
    assert not detail.startswith("'")


def test_non_positive_window_is_rejected(client: TestClient) -> None:
    """A window of zero days is refused rather than returning nothing.

    An empty result would render as "no anomalies found", which is
    indistinguishable from a genuine all-clear.

    Args:
        client: Test client with no language model configured.
    """
    assert client.get("/anomalies", params={"window_days": 0}).status_code == 422


def test_every_anomaly_states_its_reason(client: TestClient) -> None:
    """No flagged ticket is returned without a justification.

    Args:
        client: Test client with no language model configured.
    """
    for report in client.get("/anomalies").json()["reports"]:
        for item in report["anomalies"]:
            assert item["reason"].strip()
            assert item["threshold"] is not None


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def test_query_returns_answer_and_evidence(llm_client: TestClient) -> None:
    """A successful question returns prose together with its SQL and rows.

    The evidence travels with the answer so a figure can be verified rather
    than trusted - the whole argument for querying a database instead of
    asking a model to recall numbers.

    Args:
        llm_client: Test client with a running application.
    """
    with_model(
        llm_client,
        tool_call(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets WHERE status='Open'"),
        prose("There are 111 open tickets."),
    )

    payload = llm_client.post("/query", json={"question": "How many are open?"}).json()

    assert payload["answer"] == "There are 111 open tickets."
    assert payload["rows"] == [{"n": 111}]
    assert "SELECT COUNT(*)" in payload["sql"]
    assert payload["tool"] == QUERY_TOOL


def test_query_reports_token_usage_and_timing(llm_client: TestClient) -> None:
    """Cost and latency are returned alongside the answer.

    Args:
        llm_client: Test client with a running application.
    """
    with_model(
        llm_client,
        tool_call(QUERY_TOOL, sql="SELECT COUNT(*) AS n FROM tickets"),
        prose("500 tickets."),
    )

    payload = llm_client.post("/query", json={"question": "How many?"}).json()

    assert payload["prompt_tokens"] == 1100
    assert payload["completion_tokens"] == 100
    assert payload["elapsed_ms"] >= 0


def test_query_can_run_the_anomaly_detectors(llm_client: TestClient) -> None:
    """An anomaly question routes to the detectors, not to SQL.

    Args:
        llm_client: Test client with a running application.
    """
    with_model(
        llm_client,
        tool_call(ANOMALY_TOOL, kind="resolution_time_outlier"),
        prose("21 tickets took unusually long."),
    )

    payload = llm_client.post("/query", json={"question": "Any anomalies?"}).json()

    assert payload["tool"] == ANOMALY_TOOL
    assert payload["sql"] is None
    assert payload["anomaly_reports"][0]["count"] == 21


def test_query_returns_full_rows_while_capping_the_model(
    llm_client: TestClient,
) -> None:
    """The caller receives every row even when the model saw only a sample.

    Capping protects the token budget; it must not silently truncate the API
    response, or a client would believe 20 rows were the whole answer.

    Args:
        llm_client: Test client with a running application.
    """
    with_model(
        llm_client,
        tool_call(QUERY_TOOL, sql="SELECT ticket_id FROM tickets"),
        prose("Showing a sample of the tickets."),
    )

    payload = llm_client.post("/query", json={"question": "List all tickets"}).json()

    assert payload["row_count"] == 500
    assert len(payload["rows"]) == 500
    assert payload["truncated"] is True


def test_query_declines_ungrounded_answers(llm_client: TestClient) -> None:
    """A model that ignores the forced tool call is not passed through.

    Its reply would not be derived from the data, which is precisely what this
    design exists to prevent.

    Args:
        llm_client: Test client with a running application.
    """
    # Two prose replies: the first triggers the ladder retry, the second
    # exhausts it. Only then is the question declined.
    with_model(
        llm_client,
        prose("I think there are around 400."),
        prose("Still around 400."),
    )

    payload = llm_client.post("/query", json={"question": "How many?"}).json()

    assert payload["tool"] is None
    assert payload["rows"] == []


def test_query_reports_unrecoverable_sql_failure(llm_client: TestClient) -> None:
    """Two failed attempts produce an explanation rather than an exception.

    "I could not answer that" is a legitimate outcome and should not surface as
    a 500 - nothing has actually broken.

    Args:
        llm_client: Test client with a running application.
    """
    with_model(
        llm_client,
        tool_call(QUERY_TOOL, sql="DROP TABLE tickets"),
        tool_call(QUERY_TOOL, sql="DELETE FROM tickets"),
    )

    response = llm_client.post("/query", json={"question": "Delete everything"})

    assert response.status_code == 200
    assert response.json()["tool"] is None
    assert "could not" in response.json()["answer"].lower()


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"question": ""}, id="empty string"),
        pytest.param({"question": "   "}, id="whitespace only"),
        pytest.param({}, id="missing field"),
        pytest.param({"question": "x" * 501}, id="over length"),
    ],
)
def test_invalid_questions_are_rejected(
    body: dict[str, Any], llm_client: TestClient
) -> None:
    """Malformed questions are refused before any model call is made.

    Validation at the edge is the cheapest place to fail: no tokens are spent
    discovering that the input was empty.

    Args:
        body: An invalid request body.
        llm_client: Test client with a running application.
    """
    # No responses are scripted, so any model call would raise IndexError -
    # which means reaching the model at all would fail this test.
    with_model(llm_client)

    assert llm_client.post("/query", json=body).status_code == 422


# ---------------------------------------------------------------------------
# Provider failures mapped to status codes
# ---------------------------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(llm_client: TestClient) -> None:
    """A provider rate limit becomes a 429 carrying a Retry-After header.

    A real path on a free tier allowing 8,000 tokens per minute. The header is
    what lets a client back off correctly instead of guessing.

    Args:
        llm_client: Test client with a running application.
    """
    database = llm_client.app.state.database
    llm_client.app.state.query_service = TicketQueryService(
        client=FailingClient(LlmRateLimitedError("slow down", retry_after=12.0)),
        db_path=database.path,
        as_of=database.as_of,
        row_count=database.row_count,
    )

    response = llm_client.post("/query", json={"question": "How many tickets?"})

    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    # Rounded up, so a client never retries fractionally early.
    assert int(response.headers["retry-after"]) >= 12


def test_provider_outage_returns_502(llm_client: TestClient) -> None:
    """An unreachable provider becomes a 502, distinct from a missing key.

    The two demand different responses: 502 may be retried immediately, while
    503 will never succeed until an operator acts.

    Args:
        llm_client: Test client with a running application.
    """
    database = llm_client.app.state.database
    llm_client.app.state.query_service = TicketQueryService(
        client=FailingClient(LlmUnavailableError("connection refused")),
        db_path=database.path,
        as_of=database.as_of,
        row_count=database.row_count,
    )

    response = llm_client.post("/query", json={"question": "How many tickets?"})

    assert response.status_code == 502
    assert response.json()["error"] == "llm_unavailable"


def test_unexpected_errors_keep_the_standard_shape(
    unguarded_client: TestClient,
) -> None:
    """An unforeseen exception returns structured JSON, not a bare string.

    Without a catch-all handler this path falls through to the framework's
    default and returns the plain text "Internal Server Error", so a client
    would parse one shape normally and a different one on the least predictable
    path - precisely when clear diagnostics matter most.

    Args:
        unguarded_client: Test client with a running application.
    """

    class Unpredictable:
        model = "fake-model"

        def complete(self, *args: Any, **kwargs: Any) -> ChatResponse:
            raise RuntimeError("an error nobody anticipated")

    database = unguarded_client.app.state.database
    unguarded_client.app.state.query_service = TicketQueryService(
        client=Unpredictable(),
        db_path=database.path,
        as_of=database.as_of,
        row_count=database.row_count,
    )

    response = unguarded_client.post("/query", json={"question": "How many tickets?"})

    assert response.status_code == 500
    assert set(response.json()) == {"error", "detail", "retry_after"}
    assert response.json()["error"] == "internal_error"


def test_internal_errors_do_not_leak_details(unguarded_client: TestClient) -> None:
    """A 500 response reveals nothing about the internals that failed.

    The exception is logged in full for the operator; the caller gets a generic
    message. Internal paths, SQL fragments and library internals have no place
    in an HTTP response.

    Args:
        unguarded_client: Test client with a running application.
    """

    class Leaky:
        model = "fake-model"

        def complete(self, *args: Any, **kwargs: Any) -> ChatResponse:
            raise RuntimeError("secret internal detail at /srv/private/path")

    database = unguarded_client.app.state.database
    unguarded_client.app.state.query_service = TicketQueryService(
        client=Leaky(),
        db_path=database.path,
        as_of=database.as_of,
        row_count=database.row_count,
    )

    response = unguarded_client.post("/query", json={"question": "How many tickets?"})

    assert "secret internal detail" not in response.text
    assert "/srv/private/path" not in response.text


def test_database_failure_returns_503_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database that cannot be read is an availability problem.

    503 tells a caller to retry; 500 tells them to report a bug that does not
    exist. The distinction is the difference between a transient condition and
    a defect, and a client can act on only one of them.

    Args:
        client: Test client with no language model configured.
        monkeypatch: pytest's attribute patcher.
    """
    import sqlite3

    import app.main as main_module

    def broken_load(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(main_module, "load_frame", broken_load)

    response = client.get("/anomalies")

    assert response.status_code == 503
    assert response.json()["error"] == "data_unavailable"


def test_errors_share_one_shape(client: TestClient) -> None:
    """Every error response carries the same fields.

    A client parses one structure rather than a different one per failure mode.

    Args:
        client: Test client with no language model configured.
    """
    payload = client.post("/query", json={"question": "How many?"}).json()

    assert set(payload) == {"error", "detail", "retry_after"}


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def test_interactive_docs_are_served(client: TestClient) -> None:
    """``/docs`` renders, and the OpenAPI schema is valid.

    That page is the API documentation during the walkthrough, so it working is
    a deliverable rather than a convenience.

    Args:
        client: Test client with no language model configured.
    """
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_every_endpoint_appears_in_the_schema(client: TestClient) -> None:
    """All four documented endpoints are present in the OpenAPI schema.

    Args:
        client: Test client with no language model configured.
    """
    paths = client.get("/openapi.json").json()["paths"]

    assert {"/health", "/schema", "/anomalies", "/query"} <= set(paths)


def test_root_points_at_the_documentation(client: TestClient) -> None:
    """The root path directs a browser to the interactive docs.

    Args:
        client: Test client with no language model configured.
    """
    assert client.get("/").json()["docs"] == "/docs"
