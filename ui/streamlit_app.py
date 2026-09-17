"""Streamlit interface: a thin HTTP client over the REST API.

This module contains no business logic, and that is the design. It does not
open the database, build prompts, call the model or compute an anomaly. Every
answer on screen comes from an HTTP call to the API, which means the two
interfaces the brief requires cannot disagree with one another - there is only
one implementation, and the UI is one of its callers.

The alternative - importing :mod:`app.llm` directly and skipping HTTP - would
be marginally faster and would create a second, subtly different code path that
only the UI exercises. That is how a UI and an API drift apart.

What the UI adds on top of the API is presentation: sample questions to make
the system approachable, the generated SQL shown beside every answer, and
failure states written for a person rather than a program. A connection error
says "start the API with python run.py", not "ConnectionRefusedError".
"""

from __future__ import annotations

import os
from typing import Any

import altair as alt
import httpx
import pandas as pd
import streamlit as st

# Read from the environment first so the UI can point at a different host,
# falling back to the same configuration the API itself uses - rather than
# hard-coding a port in two places that could drift.
try:
    from app.config import settings

    _DEFAULT_API = f"http://{settings.api_host}:{settings.api_port}"
except Exception:  # pragma: no cover - the UI must run even if config fails
    _DEFAULT_API = "http://127.0.0.1:8000"

API_BASE_URL = os.getenv("API_BASE_URL", _DEFAULT_API).rstrip("/")

# Generous relative to a typical request: a question costs two sequential model
# calls, and a cold first call can be slow.
REQUEST_TIMEOUT = 60.0

# Drawn from the brief's own sample questions, so a reviewer can exercise the
# system without inventing queries. Ordered shortest to longest: laid out two
# per row, that keeps each pair a similar width and every label on a single
# line, rather than one wrapped question knocking the grid out of alignment.
#
# The last two are the interesting ones - both use relative time, which only
# returns anything because of the AS_OF anchor.
SAMPLE_QUESTIONS = [
    "How many tickets are currently open?",
    "Which agent resolved the most tickets this month?",
    "Which agent has the lowest average customer rating?",
    "Are there any anomalies in resolution times this week?",
    "Show me all Critical tickets not resolved within 12 hours.",
    "What is the average customer rating for Technical category tickets?",
]

# Two columns rather than three. In a wide layout each column is broad enough
# for any of the questions above to render on one line.
SAMPLE_COLUMNS = 2

st.set_page_config(
    page_title="AI Support Ticket Analyst",
    layout="wide",
)


def call_api(
    method: str, path: str, **kwargs: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """Call the API and translate any failure into a readable message.

    Every network and HTTP failure is funnelled through here so the interface
    never shows a raw traceback. A person looking at this screen needs to know
    what to do next, not which exception was raised.

    Args:
        method: HTTP method.
        path: Path beneath the API base URL.
        **kwargs: Passed through to httpx.

    Returns:
        A ``(payload, error)`` pair. Exactly one is ever populated.
    """
    url = f"{API_BASE_URL}{path}"

    try:
        response = httpx.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    except httpx.ConnectError:
        return None, (
            f"Cannot reach the API at {API_BASE_URL}. "
            "Start it with `python run.py` and reload this page."
        )
    except httpx.TimeoutException:
        return None, (
            "The API did not respond in time. The model may be slow or "
            "rate limited - wait a moment and try again."
        )
    except httpx.HTTPError as exc:
        # Catches the remaining transport failures - a dropped connection, a
        # malformed response, a protocol error. Rare, but an uncaught one would
        # surface in the browser as a Python traceback, which tells a user
        # nothing they can act on.
        return None, f"Could not complete the request: {exc}"

    if response.status_code >= 400:
        try:
            body = response.json()
            detail = body.get("detail") or body.get("error") or response.text
        except ValueError:
            detail = response.text
        return None, str(detail)

    return response.json(), None


def render_sidebar() -> dict[str, Any] | None:
    """Show service status and configuration in the sidebar.

    Surfaces the two things that most often explain unexpected behaviour: the
    time anchor, and whether the language model is configured at all.

    Returns:
        The health payload, or ``None`` when the API is unreachable.
    """
    st.sidebar.title("Service status")

    health, error = call_api("GET", "/health")
    # Explicit rather than `assert health is not None`. Asserts are stripped
    # under `python -O`, after which None would flow onward and fail later with
    # an unrelated AttributeError, far from the request that actually failed.
    if health is None:
        st.sidebar.error("The API returned no health information.")
        return None

    st.sidebar.success(f"API online - v{health['version']}")
    st.sidebar.metric("Tickets loaded", f"{health['dataset_rows']:,}")

    if health["llm_configured"]:
        st.sidebar.caption(f"Model: `{health['model']}`")
    else:
        st.sidebar.warning(
            "No API key configured. Anomaly detection still works; "
            "natural-language questions do not."
        )

    st.sidebar.divider()
    st.sidebar.caption("**Reference date**")
    st.sidebar.code(health["as_of"], language=None)
    st.sidebar.caption(
        "This dataset is a fixed snapshot ending March 2024. Questions about "
        '"this week" resolve against the date above, not today - otherwise '
        "they would match nothing."
    )

    return health


def render_answer(payload: dict[str, Any]) -> None:
    """Display an answer together with the evidence behind it.

    The SQL and the rows are shown beside the prose deliberately: an answer a
    reviewer can verify is worth more than one they must trust.

    Args:
        payload: A ``/query`` response body.
    """
    st.markdown(f"### {payload['answer']}")

    columns = st.columns(4)
    columns[0].metric("Rows returned", payload["row_count"])
    columns[1].metric("Tool used", payload["tool"] or "none")
    columns[2].metric("Time", f"{payload['elapsed_ms'] / 1000:.1f}s")
    columns[3].metric(
        "Tokens", payload["prompt_tokens"] + payload["completion_tokens"]
    )

    if payload["sql"]:
        with st.expander("Generated SQL", expanded=False):
            st.code(payload["sql"], language="sql")

    if payload["rows"]:
        label = f"Results ({payload['row_count']} rows)"
        if payload["truncated"]:
            label += " - the model summarised a sample; all rows are shown here"
        with st.expander(label, expanded=True):
            st.dataframe(
                pd.DataFrame(payload["rows"]),
                width="stretch",
                hide_index=True,
            )

    if payload.get("anomaly_reports"):
        for report in payload["anomaly_reports"]:
            render_report(report)


def render_report(report: dict[str, Any]) -> None:
    """Display one detector's report.

    The method and threshold are shown even when nothing was flagged: "no
    anomalies" only means something if the reader can see what was checked.

    Args:
        report: A serialised anomaly report.
    """
    st.subheader(report["description"])

    columns = st.columns(3)
    columns[0].metric("Flagged", report["count"])
    columns[1].metric("Considered", report["considered"])
    columns[2].metric(
        "Threshold",
        "n/a" if report["threshold"] is None else f"{report['threshold']:g}",
    )
    st.caption(f"Method: {report['method']}")

    if not report["anomalies"]:
        st.info("No anomalies found by this detector.")
        return

    frame = pd.DataFrame(report["anomalies"])
    st.dataframe(
        frame[
            [
                "ticket_id",
                "priority",
                "category",
                "agent_id",
                "value",
                "threshold",
                "reason",
            ]
        ],
        width="stretch",
        hide_index=True,
    )

    render_severity_chart(frame, threshold=report["threshold"])


def render_severity_chart(frame: pd.DataFrame, *, threshold: float | None) -> None:
    """Plot flagged values against the threshold they exceeded.

    Built with Altair rather than ``st.bar_chart`` because the ordering
    matters. Streamlit's native chart sorts a categorical axis
    alphabetically, so the table would lead with the worst offender while the
    chart led with whichever ticket id happened to sort first - the same data
    telling two different stories about severity.

    The threshold is drawn as a reference line, which is what turns a row of
    bars into an answer to "how far past the limit is this?".

    Args:
        frame: Flagged tickets, already ordered worst-first.
        threshold: The boundary crossed, or ``None`` when none was derived.
    """
    top = frame.head(20)

    bars = (
        alt.Chart(top)
        .mark_bar()
        .encode(
            # sort="-x" orders the axis by the measured value rather than by
            # ticket id, matching the table above it.
            y=alt.Y("ticket_id:N", sort="-x", title=None),
            x=alt.X("value:Q", title="Hours"),
            tooltip=["ticket_id", "priority", "category", "value", "reason"],
        )
    )

    layers = [bars]
    if threshold is not None:
        layers.append(
            alt.Chart(pd.DataFrame({"threshold": [threshold]}))
            .mark_rule(color="#d62728", strokeDash=[5, 5], size=2)
            .encode(x="threshold:Q")
        )

    chart = alt.layer(*layers).properties(
        # "container" lets Altair fill the column, avoiding Streamlit's
        # deprecated use_container_width parameter entirely.
        width="container",
        height=min(28 * len(top) + 40, 560),
    )
    st.altair_chart(chart)
    if threshold is not None:
        st.caption(f"The dashed line marks the {threshold:g} threshold.")


def render_ask_tab(health: dict[str, Any]) -> None:
    """Render the natural-language question interface.

    Args:
        health: The health payload, used to decide whether querying is possible.
    """
    if not health["llm_configured"]:
        st.warning(
            "Natural-language questions need a Groq API key. Add `GROQ_API_KEY` "
            "to your `.env` file and restart. The Anomalies tab works without "
            "one."
        )
        return

    st.caption("Try one of the sample questions, or ask your own.")

    # Sample questions are buttons rather than a dropdown so a reviewer can
    # exercise the system in one click, without typing.
    columns = st.columns(SAMPLE_COLUMNS)
    for index, question in enumerate(SAMPLE_QUESTIONS):
        if columns[index % SAMPLE_COLUMNS].button(question, width="stretch"):
            st.session_state.pending_question = question

    asked = st.chat_input("Ask a question about the support tickets")
    if asked:
        st.session_state.pending_question = asked

    question = st.session_state.pop("pending_question", None)
    if not question:
        return

    st.divider()
    st.caption(f"**Question:** {question}")

    with st.spinner("Translating to SQL, querying, and composing an answer..."):
        payload, error = call_api("POST", "/query", json={"question": question})

    if payload is None:
        st.error(error or "The API returned no answer.")
        return

    render_answer(payload)


def render_anomalies_tab() -> None:
    """Render the deterministic anomaly dashboard.

    Reachable with no API key configured, which is worth stating on screen: it
    demonstrates that the statistical half of the system has no model
    dependency.
    """
    st.caption(
        "Purely statistical - no language model involved. This tab works with "
        "no API key configured."
    )

    schema, error = call_api("GET", "/schema")
    detectors = schema["detectors"] if schema else []

    left, right = st.columns(2)
    detector = left.selectbox(
        "Detector",
        options=["All detectors", *detectors],
        help="Which check to run.",
    )
    window_label = right.selectbox(
        "Time window",
        options=["All history", "Last 7 days", "Last 30 days", "Last 90 days"],
        help=(
            "Restricts which tickets are evaluated. Thresholds are always "
            "derived from the full history, so a quiet week cannot raise the "
            "bar and hide genuine outliers."
        ),
    )

    params: dict[str, Any] = {}
    if detector != "All detectors":
        params["kind"] = detector
    if window_label != "All history":
        params["window_days"] = int(window_label.split()[1])

    payload, error = call_api("GET", "/anomalies", params=params)
    if payload is None:
        st.error(error or "The API returned no anomaly data.")
        return

    st.metric("Total anomalies", payload["total_anomalies"])
    st.divider()

    for report in payload["reports"]:
        render_report(report)
        st.divider()


def main() -> None:
    """Compose the page."""
    st.title("AI Support Ticket Analyst")
    st.caption(
        "Ask questions in plain English. Every figure is computed by the "
        "database and shown with the SQL that produced it - the model phrases "
        "the answer, it never calculates it."
    )

    health = render_sidebar()
    if health is None:
        st.error(
            f"The API is not reachable at {API_BASE_URL}. "
            "Start both services with `python run.py`."
        )
        return

    ask_tab, anomalies_tab = st.tabs(["Ask a question", "Anomalies"])

    with ask_tab:
        render_ask_tab(health)

    with anomalies_tab:
        render_anomalies_tab()


main()
