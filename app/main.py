"""The FastAPI application: four endpoints over the ticket dataset.

Composition happens here and only here. Every other module is independently
constructible and testable; this one wires them together, decides what a
failure means in HTTP terms, and owns the application's lifecycle.

Startup
-------
The database is rebuilt from the CSV once, when the process starts, rather than
per request. It is a build artifact derived entirely from the CSV, so this
guarantees the running system always reflects the file on disk. State lives on
``app.state`` rather than in module-level globals, which keeps the app
constructible more than once - what makes the test suite able to spin up
isolated instances.

Degrading without an LLM
------------------------
``/health``, ``/anomalies`` and ``/schema`` require no API key and no network.
Only ``/query`` does. A missing key therefore reduces the service to its
deterministic subset instead of preventing startup, and ``/health`` reports
which mode is in effect. This is a deliberate property, verified by test, not
an accident of the code.

Mapping failures to status codes
--------------------------------
Every failure mode gets the status code that describes it, rather than a
blanket 500:

    422  the question was empty or malformed - a client error, caught before
         any token is spent
    429  the provider's rate limit was hit; the response carries Retry-After
    502  the provider was reached but failed
    503  no API key is configured - the service is healthy, credentials are not
    500  anything genuinely unexpected

The distinction between 429, 502 and 503 matters to a caller: one should be
retried after a wait, one may be retried immediately, and one will never
succeed until an operator intervenes.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.anomalies import DETECTORS, detect_anomalies, load_frame
from app.config import settings
from app.data import (
    CATEGORIES,
    PRIORITIES,
    STATUSES,
    TABLE_NAME,
    DataIntegrityError,
    build_database,
)
from app.llm import (
    LlmError,
    LlmNotConfiguredError,
    LlmRateLimitedError,
    LlmUnavailableError,
    TicketQueryService,
    build_chat_client,
)
from app.models import (
    AnomalyResponse,
    ErrorResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    SchemaResponse,
)

# Configured here because this module is the application entry point. The
# modules under app/ deliberately do not call basicConfig - a library that
# configures logging hijacks it from whatever imports it, which is why the
# convention places this responsibility with the application.
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Column documentation for /schema. Kept beside the endpoint that serves it
# rather than in the data layer, because this is API presentation - the
# database itself has no opinion about what a column means to a caller.
_COLUMN_DOCS: dict[str, tuple[str, bool, str]] = {
    "ticket_id": ("TEXT", False, "Unique ticket identifier."),
    "created_at": ("TEXT", False, "When the ticket was raised (YYYY-MM-DD HH:MM:SS)."),
    "category": ("TEXT", False, "Issue category."),
    "priority": ("TEXT", False, "Urgency level."),
    "status": ("TEXT", False, "Current state. Open and Escalated are unresolved."),
    "response_time_hrs": ("REAL", False, "Hours until the first agent response."),
    "resolution_time_hrs": (
        "REAL",
        True,
        "Hours until resolution. NULL while the ticket is unresolved.",
    ),
    "agent_id": ("TEXT", False, "Assigned support agent."),
    "customer_rating": (
        "INTEGER",
        True,
        "Satisfaction rating from 1 to 5. NULL while the ticket is unresolved.",
    ),
    "issue_summary": ("TEXT", False, "Short description of the reported issue."),
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the database and query service once, at startup.

    Doing this here rather than per request means the CSV is parsed once and
    the cost is paid before the first caller arrives. A failure to load the
    data stops the process immediately, which is correct: a service that
    cannot answer anything should not accept traffic.

    Args:
        app: The application being started.

    Yields:
        ``None`` once startup is complete.
    """
    try:
        database = build_database()
    except (DataIntegrityError, FileNotFoundError, RuntimeError) as exc:
        # Refusing to start is correct - a service that can answer nothing
        # should not accept traffic - but the operator needs to read *why*.
        # Raised bare, this message ends up buried in roughly seventy lines of
        # asyncio and uvicorn frames, so it is logged plainly first.
        logger.error("Cannot start: the ticket data could not be loaded.")
        logger.error("  %s", exc)
        logger.error("  Check CSV_PATH in your .env, or the file's contents.")
        raise

    app.state.database = database
    logger.info(
        "Loaded %d tickets, anchored at %s", database.row_count, database.as_of
    )

    # The query service is optional. Without it the deterministic endpoints
    # must still serve, so any failure to construct it is logged as a downgrade
    # rather than raised as a startup failure. Catching the base LlmError
    # rather than one subclass keeps that true for causes not yet imagined -
    # a missing package, a malformed key, a future provider error.
    try:
        app.state.query_service = TicketQueryService(
            client=build_chat_client(),
            db_path=database.path,
            as_of=database.as_of,
            row_count=database.row_count,
        )
        logger.info("Natural-language querying enabled (%s)", settings.groq_model)
    except LlmError as exc:
        app.state.query_service = None
        logger.warning("Natural-language querying disabled: %s", exc)

    yield


app = FastAPI(
    title="AI Support Ticket Analyst",
    version=__version__,
    lifespan=lifespan,
    summary=(
        "Query customer support tickets in natural language, and detect "
        "operational anomalies."
    ),
    description=(
        "Questions are answered by translating them into SQL, executing that "
        "SQL against a read-only database, and having a language model phrase "
        "the result. **Every figure is computed by the database, never by the "
        "model** - which is why each answer returns the SQL that produced it.\n\n"
        "Anomaly detection is purely statistical and involves no model at all, "
        "so `/anomalies` works with no API key configured."
    ),
)

# The Streamlit UI is a browser client on a different port, so it is a
# cross-origin caller. Origins are restricted to the configured UI rather than
# opened to all: this is a local tool, and a wildcard would be a habit worth
# not forming.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://localhost:{settings.ui_port}",
        f"http://127.0.0.1:{settings.ui_port}",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _error(
    code: str, detail: str, status_code: int, *, retry_after: float | None = None
) -> JSONResponse:
    """Build a uniform error response.

    Args:
        code: Stable machine-readable error code.
        detail: Human-readable explanation of what to do about it.
        status_code: HTTP status to return.
        retry_after: Seconds to wait, for rate-limit errors.

    Returns:
        A JSON response matching :class:`app.models.ErrorResponse`.
    """
    headers: dict[str, str] = {}
    if retry_after is not None:
        # Retry-After is an integer number of seconds per RFC 9110, and is
        # rounded up so a client never retries fractionally early.
        headers["Retry-After"] = str(int(retry_after) + 1)

    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error=code, detail=detail, retry_after=retry_after
        ).model_dump(),
        headers=headers,
    )


@app.exception_handler(LlmRateLimitedError)
async def handle_rate_limit(
    request: Request, exc: LlmRateLimitedError
) -> JSONResponse:
    """Return 429 with the provider's own retry guidance.

    Args:
        request: The failed request.
        exc: The rate-limit error.

    Returns:
        A 429 response carrying a ``Retry-After`` header.
    """
    logger.warning("Rate limited: %s", exc)
    return _error(
        "rate_limited", str(exc), status.HTTP_429_TOO_MANY_REQUESTS,
        retry_after=exc.retry_after,
    )


@app.exception_handler(LlmNotConfiguredError)
async def handle_not_configured(
    request: Request, exc: LlmNotConfiguredError
) -> JSONResponse:
    """Return 503 when no API key is configured.

    Not a 500: nothing has failed. The service is running correctly and its
    deterministic endpoints are serving; this one capability is unavailable
    until an operator supplies a credential.

    Args:
        request: The failed request.
        exc: The configuration error.

    Returns:
        A 503 response.
    """
    return _error("not_configured", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)


@app.exception_handler(LlmUnavailableError)
async def handle_unavailable(
    request: Request, exc: LlmUnavailableError
) -> JSONResponse:
    """Return 502 when the model provider fails.

    Args:
        request: The failed request.
        exc: The upstream error.

    Returns:
        A 502 response.
    """
    logger.error("Provider unavailable: %s", exc)
    return _error("llm_unavailable", str(exc), status.HTTP_502_BAD_GATEWAY)


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Return 500 in the standard error shape for anything unforeseen.

    Without this, an unhandled exception falls through to the framework's
    default handler and returns the bare string "Internal Server Error". Every
    other failure in this API returns a structured body, so a client would have
    to parse one shape normally and a different one on the least predictable
    path - exactly when clear diagnostics matter most.

    The exception is logged in full, with its traceback, while the response
    carries only a generic message: internal details such as file paths and
    SQL fragments should not be returned to a caller.

    Args:
        request: The failed request.
        exc: The unhandled exception.

    Returns:
        A 500 response matching :class:`app.models.ErrorResponse`.
    """
    logger.exception("Unhandled error serving %s %s", request.method, request.url.path)
    return _error(
        "internal_error",
        "The service encountered an unexpected error. Check the server logs "
        "for details.",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health and effective configuration",
    tags=["status"],
)
async def health(request: Request) -> HealthResponse:
    """Report readiness and the configuration actually in effect.

    Deliberately richer than a bare status flag: the row count and time anchor
    confirm the data loaded as expected, and ``llm_configured`` distinguishes
    "running in deterministic mode" from "broken".

    Args:
        request: The incoming request, used to reach application state.

    Returns:
        The service's current status.
    """
    database = request.app.state.database

    return HealthResponse(
        status="ok",
        version=__version__,
        dataset_rows=database.row_count,
        as_of=database.as_of.isoformat(sep=" "),
        llm_configured=request.app.state.query_service is not None,
        model=settings.groq_model,
    )


@app.get(
    "/schema",
    response_model=SchemaResponse,
    summary="Shape of the queryable data",
    tags=["status"],
)
async def schema(request: Request) -> SchemaResponse:
    """Describe the table, its permitted values and the available detectors.

    Makes the API self-describing: a caller can discover what may be asked
    about without reading the source or guessing at enum values.

    Args:
        request: The incoming request, used to reach application state.

    Returns:
        The dataset's schema.
    """
    database = request.app.state.database

    return SchemaResponse(
        table=TABLE_NAME,
        row_count=database.row_count,
        columns=[
            {
                "name": name,
                "type": sql_type,
                "nullable": nullable,
                "description": description,
            }
            for name, (sql_type, nullable, description) in _COLUMN_DOCS.items()
        ],
        categories=sorted(CATEGORIES),
        priorities=sorted(PRIORITIES),
        statuses=sorted(STATUSES),
        detectors=sorted(DETECTORS),
    )


@app.get(
    "/anomalies",
    response_model=AnomalyResponse,
    summary="Detect anomalous tickets (no language model involved)",
    tags=["analysis"],
)
async def anomalies(
    request: Request,
    kind: str | None = Query(
        default=None,
        description=(
            "Detector to run. Omit to run all of them. See /schema for the "
            "available names."
        ),
    ),
    window_days: int | None = Query(
        default=None,
        gt=0,
        description=(
            "Restrict to tickets raised in the last N days. Thresholds are "
            "still derived from the full history, so a quiet week cannot raise "
            "the bar and hide genuine outliers."
        ),
    ),
) -> AnomalyResponse | JSONResponse:
    """Run the statistical detectors over the dataset.

    Purely deterministic, with no model involvement, so this endpoint serves
    correctly with no API key configured.

    Args:
        request: The incoming request, used to reach application state.
        kind: Optional detector name.
        window_days: Optional time window in days.

    Returns:
        One report per detector, or a 422 if the detector name is unknown.
    """
    database = request.app.state.database

    try:
        reports = detect_anomalies(
            load_frame(database.path),
            as_of=database.as_of,
            kinds=[kind] if kind else None,
            window_days=window_days,
        )
    except KeyError as exc:
        # The detector name came from the caller, so an unknown one is a client
        # error. The message names the valid options rather than only refusing.
        #
        # exc.args[0] rather than str(exc): KeyError's str() wraps the message
        # in repr quotes, which would surface to the caller as "'Unknown
        # detector...'" complete with stray apostrophes.
        return _error(
            "unknown_detector",
            str(exc.args[0]) if exc.args else "Unknown anomaly detector.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    except sqlite3.Error as exc:
        # A database that has gone missing or become locked is an availability
        # problem, not a defect. 503 tells the caller to retry; 500 would tell
        # them to report a bug that does not exist.
        logger.error("Anomaly detection could not read the database: %s", exc)
        return _error(
            "data_unavailable",
            "The ticket data is temporarily unavailable. Please retry shortly.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    payloads = [report.to_dict() for report in reports]

    return AnomalyResponse(
        as_of=database.as_of.isoformat(sep=" "),
        window_days=window_days,
        total_anomalies=sum(report["count"] for report in payloads),
        reports=payloads,
    )


@app.post(
    "/query",
    response_model=QueryResponse,
    summary="Ask a question in natural language",
    tags=["analysis"],
    responses={
        429: {"model": ErrorResponse, "description": "Provider rate limit reached"},
        502: {"model": ErrorResponse, "description": "Provider unreachable"},
        503: {"model": ErrorResponse, "description": "No API key configured"},
    },
)
async def query(request: Request, body: QueryRequest) -> QueryResponse:
    """Answer a natural-language question about the tickets.

    The question is translated into a tool call, the result is computed here,
    and a model phrases the answer. The SQL and the full result set are
    returned alongside the prose so the answer can be verified rather than
    trusted.

    Args:
        request: The incoming request, used to reach application state.
        body: The validated question.

    Returns:
        The answer and the evidence behind it.

    Raises:
        LlmNotConfiguredError: If no API key is configured, mapped to 503.
        LlmRateLimitedError: If the provider's rate limit is hit, mapped to 429.
        LlmUnavailableError: If the provider fails, mapped to 502.
    """
    service: TicketQueryService | None = request.app.state.query_service

    if service is None:
        # Raised rather than returned so the registered handler formats it,
        # keeping every error response identical in shape.
        raise LlmNotConfiguredError(
            "No GROQ_API_KEY is configured, so natural-language questions are "
            "unavailable. /anomalies and /schema still work. Set a free key "
            "from https://console.groq.com in your .env file."
        )

    result = service.answer(body.question)
    return QueryResponse(**result.to_dict())


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    """Point a browser at the interactive documentation.

    Returns:
        A short index of what the service offers.
    """
    return {
        "service": "AI Support Ticket Analyst",
        "version": __version__,
        "docs": "/docs",
        "endpoints": ["/health", "/schema", "/anomalies", "/query"],
    }
