"""Pydantic schemas defining the HTTP boundary.

Every request body is validated on the way in and every response is declared on
the way out. That buys three things at once, which is why the layer exists at
all rather than passing dictionaries around:

**Rejection before cost.** A blank question is refused by FastAPI before a
single token is spent on it. Validation at the edge is the cheapest place to
fail.

**A contract, not a promise.** These classes generate the OpenAPI schema, so
``/docs`` documents the real shape of every response rather than a prose
description that drifts from the code. During a walkthrough that page *is* the
API documentation.

**A stable surface.** The Streamlit UI consumes these models. Internal types
such as :class:`app.llm.QueryResult` can change shape without breaking it, as
long as the mapping here is updated - which the type checker enforces.

Field descriptions are written for whoever reads ``/docs``, not for us. They
are the only explanation an evaluator gets before trying an endpoint.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class QueryRequest(BaseModel):
    """A natural-language question about the ticket dataset."""

    model_config = ConfigDict(
        # Whitespace is stripped before validation, so a question of only
        # spaces fails min_length rather than reaching the pipeline and
        # costing an API call to discover it was empty.
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [{"question": "How many tickets are currently open?"}]
        },
    )

    question: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="A question about the support tickets, in plain English.",
    )


class QueryResponse(BaseModel):
    """An answer, together with the evidence that produced it.

    The generated SQL and full result set travel with the prose deliberately.
    An evaluator can then verify a figure rather than trust it, which is the
    whole argument for querying a database instead of asking a model to recall
    numbers.
    """

    question: str = Field(description="The question as asked.")
    answer: str = Field(description="The natural-language answer.")
    tool: str | None = Field(
        description=(
            "Which tool produced the answer: 'query_tickets' for SQL, "
            "'detect_anomalies' for the statistical engine, or null if the "
            "question could not be answered from this dataset."
        )
    )
    sql: str | None = Field(
        description="The SQL executed, when the query tool was used."
    )
    rows: list[dict[str, Any]] = Field(
        description="The complete result set, uncapped."
    )
    row_count: int = Field(description="Number of rows returned.")
    truncated: bool = Field(
        description=(
            "Whether the model was shown fewer rows than were returned. The "
            "full set is always present in 'rows'."
        )
    )
    anomaly_reports: list[dict[str, Any]] | None = Field(
        description="Detector reports, when the anomaly tool was used."
    )
    as_of: str = Field(
        description=(
            "The reference time used to resolve relative dates such as 'this "
            "week'. Anchored to the dataset's latest ticket, not the wall "
            "clock, because the data is a fixed historical snapshot."
        )
    )
    elapsed_ms: int = Field(description="Total round-trip time in milliseconds.")
    model: str = Field(description="The model that answered.")
    prompt_tokens: int = Field(description="Input tokens across every model call.")
    completion_tokens: int = Field(
        description="Generated tokens across every model call."
    )
    tokens_estimated: bool = Field(
        default=False,
        description=(
            "True when part of the token count is an estimate. The provider "
            "reports no usage for a request it rejects - such as a model "
            "declining to call a tool - although tokens were still spent."
        ),
    )


class AnomalyItem(BaseModel):
    """A single flagged ticket, carrying its own justification."""

    ticket_id: str = Field(description="Identifier of the flagged ticket.")
    kind: str = Field(description="Which detector flagged it.")
    reason: str = Field(
        description=(
            "Why it was flagged, stating the measured value and the threshold "
            "it crossed. Safe to show a user verbatim."
        )
    )
    value: float = Field(description="The measured quantity that triggered the flag.")
    threshold: float = Field(description="The boundary the value crossed.")
    created_at: str = Field(description="When the ticket was raised.")
    category: str = Field(description="Ticket category.")
    priority: str = Field(description="Ticket priority.")
    status: str = Field(description="Ticket status.")
    agent_id: str = Field(description="Assigned agent.")


class AnomalyReportModel(BaseModel):
    """The outcome of running one detector.

    Reports its method and threshold even when nothing was flagged: "no
    anomalies" is only trustworthy if the reader can see what was looked for.
    Otherwise it is indistinguishable from a detector that silently failed.
    """

    kind: str = Field(description="Identifier of the detector.")
    description: str = Field(description="What this detector looks for.")
    method: str = Field(description="How the threshold was derived.")
    threshold: float | None = Field(
        description=(
            "The boundary applied, or null when too little data existed to "
            "establish one."
        )
    )
    as_of: str = Field(description="Reference time used for age calculations.")
    considered: int = Field(description="How many tickets were eligible.")
    count: int = Field(description="How many were flagged.")
    anomalies: list[AnomalyItem] = Field(description="The flagged tickets.")
    rationale: str | None = Field(
        default=None,
        description=(
            "Why this method suits the data, with figures computed from it. "
            "Null for a fixed business rule."
        ),
    )


class AnomalyResponse(BaseModel):
    """All detector reports for a request.

    Served without any language model involvement, so this endpoint works with
    no API key configured.
    """

    as_of: str = Field(description="Reference time used for age calculations.")
    window_days: int | None = Field(
        description=(
            "Time window applied, in days, or null for all history. Note that "
            "statistical thresholds are always derived from the full history - "
            "a quiet week must not raise the bar and hide real outliers."
        )
    )
    total_anomalies: int = Field(description="Total flagged across all detectors.")
    reports: list[AnomalyReportModel] = Field(description="One report per detector.")


class HealthResponse(BaseModel):
    """Service health and the configuration actually in effect.

    Deliberately more than a bare ``{"status": "ok"}``. Reporting the row
    count, the time anchor and whether the language model is configured lets an
    operator diagnose the two most likely misconfigurations - an empty dataset,
    or a missing API key - from a single request.
    """

    status: str = Field(description="'ok' when the service is ready.")
    version: str = Field(description="Running application version.")
    dataset_rows: int = Field(description="Tickets loaded into the database.")
    dataset_file: str = Field(
        description="Name of the CSV file the tickets were loaded from."
    )
    as_of: str = Field(
        description="Reference time for relative date expressions."
    )
    llm_configured: bool = Field(
        description=(
            "Whether natural-language querying is available. When false, "
            "/anomalies and /schema still work; only /query is unavailable."
        )
    )
    model: str = Field(description="Configured model identifier.")


class ColumnSchema(BaseModel):
    """One column of the ticket table."""

    name: str = Field(description="Column name.")
    type: str = Field(description="SQLite storage type.")
    nullable: bool = Field(description="Whether the column may be NULL.")
    description: str = Field(description="What the column means.")


class SchemaResponse(BaseModel):
    """The shape of the queryable data.

    Exposed so the API is self-describing: a caller can discover what may be
    asked about without reading the source or guessing at enum values.
    """

    table: str = Field(description="Table name.")
    row_count: int = Field(description="Number of rows.")
    columns: list[ColumnSchema] = Field(description="Column definitions.")
    categories: list[str] = Field(description="Permitted category values.")
    priorities: list[str] = Field(description="Permitted priority values.")
    statuses: list[str] = Field(description="Permitted status values.")
    detectors: list[str] = Field(description="Available anomaly detectors.")


class ErrorResponse(BaseModel):
    """A failure, explained in terms the caller can act on.

    Every error path returns this shape, so a client has one thing to parse
    rather than a different structure per failure mode.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": "rate_limited",
                    "detail": (
                        "The model provider's rate limit has been reached. The "
                        "free tier allows 30 requests and 8,000 tokens per "
                        "minute."
                    ),
                    "retry_after": 12.0,
                }
            ]
        }
    )

    error: str = Field(
        description="Stable machine-readable code, safe to branch on."
    )
    detail: str = Field(description="Human-readable explanation of what to do.")
    retry_after: float | None = Field(
        default=None,
        description="Seconds to wait before retrying, on rate-limit errors.",
    )
