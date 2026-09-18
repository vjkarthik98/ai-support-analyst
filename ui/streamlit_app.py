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

# A sibling module: Streamlit puts this script's own folder on sys.path, so it
# imports the same way however the app is launched.
from formatting import (
    answer_metadata,
    blank_missing,
    detector_label,
    escape_markdown,
    format_reference_date,
    keep_identifiers_together,
)

# A question makes at most four model calls (see app.llm), each bounded by the
# provider timeout and never retried after timing out. The interface must wait
# longer than that worst case. It once gave up at 60 seconds while the server
# could legitimately take minutes - telling the user the request had failed
# while the server carried on, still spending tokens on an answer no one saw.
_MAX_MODEL_CALLS = 4
# Headroom for the database query (itself capped at a few seconds), transport
# retries, and HTTP overhead.
_TIMEOUT_HEADROOM_SECONDS = 30.0

try:
    from app.config import settings

    _DEFAULT_API = f"http://{settings.api_host}:{settings.api_port}"
    _LLM_TIMEOUT = settings.llm_timeout_seconds
except Exception:  # pragma: no cover - the UI must run even if config fails
    _DEFAULT_API = "http://127.0.0.1:8000"
    _LLM_TIMEOUT = 30.0

# Read from the environment first so the UI can point at a different host,
# falling back to the same configuration the API itself uses - rather than
# hard-coding a port in two places that could drift.
API_BASE_URL = os.getenv("API_BASE_URL", _DEFAULT_API).rstrip("/")

REQUEST_TIMEOUT = _MAX_MODEL_CALLS * _LLM_TIMEOUT + _TIMEOUT_HEADROOM_SECONDS

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

# Colours for the severity chart, matching the theme in .streamlit/config.toml:
# the indigo accent for measured values, a rose rule for the threshold.
BAR_COLOUR = "#4F46E5"
THRESHOLD_COLOUR = "#E11D48"

st.set_page_config(
    page_title="AI Support Ticket Analyst",
    page_icon=":material/support_agent:",
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


def render_sidebar() -> tuple[dict[str, Any] | None, str | None]:
    """Show service status and configuration in the sidebar.

    Surfaces the two things that most often explain unexpected behaviour: the
    time anchor, and whether the language model is configured at all. Laid out
    as a status line of badges, then one card per fact, so the state of the
    system can be read at a glance rather than parsed from sentences.

    Returns:
        A ``(health, error)`` pair. ``health`` is the payload, or ``None``
        when it could not be fetched - in which case ``error`` says why.
    """
    sidebar = st.sidebar
    sidebar.subheader("System status")

    health, error = call_api("GET", "/health")
    # Explicit rather than `assert health is not None`. Asserts are stripped
    # under `python -O`, after which None would flow onward and fail later with
    # an unrelated AttributeError, far from the request that actually failed.
    if health is None:
        # The specific reason - not reachable, timed out, an error status - is
        # the one thing that tells the reader what to do. It is returned for
        # the main area to show in full; the sidebar shows only the state, so
        # the same message does not appear twice on one screen.
        reason = error or "The API returned no health information."
        sidebar.markdown(":red-badge[:material/error: Offline]")
        return None, reason

    # One line answers "is it working?": service, build, and whether questions
    # can be asked. Degraded mode is amber, not red - nothing has failed, one
    # capability simply has no key.
    # Labels kept short enough that all three badges fit the sidebar on one
    # line; "Model connected" wrapped onto a second.
    model_badge = (
        ":blue-badge[:material/smart_toy: LLM ready]"
        if health["llm_configured"]
        else ":orange-badge[:material/warning: No LLM key]"
    )
    sidebar.markdown(
        f":green-badge[:material/check_circle: Online] "
        f":gray-badge[v{health['version']}] {model_badge}"
    )

    # The file name sits inside the same card as the count it produced, so the
    # two read as one fact: 500 tickets, from this file.
    with sidebar.container(border=True):
        st.metric("Tickets loaded", f"{health['dataset_rows']:,}")
        # .get() so a sidebar pointed at an older API still renders.
        if health.get("dataset_file"):
            st.caption(f":material/description: {escape_markdown(health['dataset_file'])}")
    as_of_date, as_of_time = format_reference_date(health["as_of"])
    sidebar.metric(
        "Data as of",
        as_of_date,
        border=True,
        # The explanation and the exact time live in the tooltip rather than
        # beneath the card, keeping the sidebar to what is read at a glance.
        help=(
            (f"Latest ticket: {as_of_date}, {as_of_time}. " if as_of_time else "")
            + "The dataset is a fixed snapshot, so questions such as \"this "
            "week\" resolve against this date rather than today - against "
            "today's date they would match nothing."
        ),
    )

    # Neither the model's name nor a missing-key note is repeated here. The
    # badge above already states whether questions can be asked; the name is
    # in /health and every /query response; and the question tab explains a
    # missing key where the reader tries to use it.

    sidebar.divider()
    # Points an evaluator straight at the second interface the brief requires.
    sidebar.link_button(
        "API documentation",
        f"{API_BASE_URL}/docs",
        icon=":material/menu_book:",
        width="stretch",
    )

    return health, None


def render_answer(question: str, payload: dict[str, Any]) -> None:
    """Display an answer together with the evidence behind it.

    The answer leads, in a card, with a quiet line of how it was produced;
    the SQL and the rows follow, so an answer a reviewer can verify sits
    directly above the evidence that verifies it.

    Args:
        question: The question as the user asked it.
        payload: A ``/query`` response body.
    """
    with st.container(border=True):
        st.caption(f":material/chat_bubble: {escape_markdown(question)}")
        # Escaped because the answer is written by a model: "$" pairs would
        # render as a formula and "*" or "_" as emphasis, altering what the
        # data says. Identifiers are then held together so none splits
        # across a line.
        st.markdown(f"### {keep_identifiers_together(escape_markdown(payload['answer']))}")
        st.markdown(answer_metadata(payload))

        if payload["sql"]:
            with st.expander("Generated SQL", icon=":material/code:"):
                st.code(payload["sql"], language="sql", wrap_lines=True)

        # An anomaly answer carries its reports, which already show every
        # flagged ticket; listing the same tickets again as raw rows would
        # only repeat them.
        if payload.get("anomaly_reports"):
            for report in payload["anomaly_reports"]:
                render_report(report)
        elif payload["rows"]:
            rows = payload["row_count"]
            with st.expander(
                f"Results · {rows:,} row{'s' if rows != 1 else ''}",
                icon=":material/table_rows:",
                expanded=True,
            ):
                if payload["truncated"]:
                    st.caption(
                        "The answer describes a sample of these rows; every "
                        "row is listed here."
                    )
                st.dataframe(
                    blank_missing(pd.DataFrame(payload["rows"])),
                    width="stretch",
                    hide_index=True,
                )


def render_report(report: dict[str, Any]) -> None:
    """Display one detector's report as a self-contained card.

    The method and threshold are shown even when nothing was flagged: "no
    anomalies" only means something if the reader can see what was checked.

    Args:
        report: A serialised anomaly report.
    """
    with st.container(border=True):
        st.markdown(f"#### {detector_label(report['kind'])}")
        st.caption(f"{report['description']} · {report['method']}")

        flagged, considered, threshold = st.columns(3)
        flagged.metric("Flagged", f"{report['count']:,}", border=True)
        considered.metric("Considered", f"{report['considered']:,}", border=True)
        threshold.metric(
            "Threshold",
            "n/a" if report["threshold"] is None else f"{report['threshold']:g} h",
            border=True,
        )

        if not report["anomalies"]:
            st.info("Nothing crossed the threshold.", icon=":material/check_circle:")
            return

        frame = pd.DataFrame(report["anomalies"])
        render_severity_chart(frame, threshold=report["threshold"])
        with st.expander(
            f"Flagged tickets · {report['count']:,}", icon=":material/list:"
        ):
            st.dataframe(
                blank_missing(
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
                    ]
                ),
                width="stretch",
                hide_index=True,
            )


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
        .mark_bar(color=BAR_COLOUR, cornerRadiusEnd=3)
        .encode(
            # sort="-x" orders the axis by the measured value rather than by
            # ticket id, matching the table below it.
            y=alt.Y("ticket_id:N", sort="-x", title=None),
            x=alt.X("value:Q", title="Hours"),
            tooltip=["ticket_id", "priority", "category", "value", "reason"],
        )
    )

    layers = [bars]
    if threshold is not None:
        layers.append(
            alt.Chart(pd.DataFrame({"threshold": [threshold]}))
            .mark_rule(color=THRESHOLD_COLOUR, strokeDash=[5, 4], size=2)
            .encode(x="threshold:Q")
        )

    chart = alt.layer(*layers).properties(
        # "container" lets Altair fill the column, avoiding Streamlit's
        # deprecated use_container_width parameter entirely.
        width="container",
        height=min(26 * len(top) + 40, 560),
    )
    st.altair_chart(chart)

    notes = []
    if len(frame) > len(top):
        notes.append(f"The {len(top)} most severe of {len(frame)}, worst first.")
    if threshold is not None:
        notes.append(f"The dashed line marks the {threshold:g} h threshold.")
    if notes:
        st.caption(" ".join(notes))


def _queue_suggestion() -> None:
    """Ask the suggested question the user just picked.

    Runs as the chips' change callback, before the page re-renders: it queues
    the question and clears the selection, so the chips behave as buttons - one
    click asks - rather than as a setting that stays switched on.
    """
    st.session_state.pending_question = st.session_state.suggestion
    st.session_state.suggestion = None


def render_ask_tab(health: dict[str, Any]) -> None:
    """Render the natural-language question interface.

    Args:
        health: The health payload, used to decide whether querying is possible.
    """
    if not health["llm_configured"]:
        st.warning(
            "Natural-language questions need a Groq API key. Add `GROQ_API_KEY` "
            "to your `.env` file and restart. The Anomalies tab works without "
            "one.",
            icon=":material/key_off:",
        )
        return

    # Chips rather than a grid of large buttons: a reviewer can still try a
    # question in one click, without the suggestions outweighing the answer.
    st.pills(
        "Suggested questions",
        SAMPLE_QUESTIONS,
        selection_mode="single",
        key="suggestion",
        on_change=_queue_suggestion,
    )

    asked = st.chat_input("Ask a question about the support tickets")
    if asked:
        st.session_state.pending_question = asked

    question = st.session_state.pop("pending_question", None)
    if not question:
        return

    with st.spinner("Translating to SQL, querying, and composing an answer..."):
        payload, error = call_api("POST", "/query", json={"question": question})

    if payload is None:
        st.error(error or "The API returned no answer.", icon=":material/error:")
        return

    render_answer(question, payload)


def render_anomalies_tab() -> None:
    """Render the deterministic anomaly dashboard.

    Reachable with no API key configured, which is worth stating on screen: it
    demonstrates that the statistical half of the system has no model
    dependency.
    """
    st.caption(
        ":material/functions: Purely statistical - no language model involved. "
        "This tab works with no API key configured."
    )

    schema, error = call_api("GET", "/schema")
    detectors = schema["detectors"] if schema else []

    left, right = st.columns(2)
    detector = left.selectbox(
        "Detector",
        options=["all", *detectors],
        format_func=lambda kind: "All detectors" if kind == "all" else detector_label(kind),
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
    if detector != "all":
        params["kind"] = detector
    if window_label != "All history":
        params["window_days"] = int(window_label.split()[1])

    payload, error = call_api("GET", "/anomalies", params=params)
    if payload is None:
        st.error(error or "The API returned no anomaly data.", icon=":material/error:")
        return

    # A summary row first: the total, then each detector's count, so the
    # shape of the result reads before any detail.
    reports = payload["reports"]
    summary = st.columns(len(reports) + 1)
    summary[0].metric("Total anomalies", f"{payload['total_anomalies']:,}", border=True)
    for column, report in zip(summary[1:], reports, strict=True):
        column.metric(detector_label(report["kind"]), f"{report['count']:,}", border=True)

    for report in reports:
        render_report(report)


def main() -> None:
    """Compose the page."""
    st.title("AI Support Ticket Analyst", anchor=False)
    st.caption(
        "Ask about support tickets in plain English. Every figure is computed "
        "by the database and shown with the SQL that produced it - the model "
        "phrases the answer, it never calculates it."
    )
    # The three properties that set this apart, stated where a first-time
    # reader looks - not left to the README.
    st.markdown(
        ":blue-badge[:material/database: Read-only SQL] "
        ":blue-badge[:material/verified: Every figure verified] "
        ":blue-badge[:material/insights: Deterministic anomaly detection]"
    )

    health, error = render_sidebar()
    if health is None:
        # The actual reason, not an assumed one. "Not reachable" was shown for
        # every failure, including an API that answered with an error or was
        # merely slow - pointing the reader at the wrong fix. call_api already
        # adds the "start it with python run.py" advice where that is the fix.
        st.error(error, icon=":material/cloud_off:")
        return

    ask_tab, anomalies_tab = st.tabs(
        [":material/forum: Ask a question", ":material/monitoring: Anomalies"]
    )

    with ask_tab:
        render_ask_tab(health)

    with anomalies_tab:
        render_anomalies_tab()


main()
