"""Tests for :mod:`app.prompts`, the model-facing text.

Prompt text is usually left untested, on the grounds that it is "just a
string". That reasoning does not hold here, because several properties of
these strings are load-bearing and would fail silently if broken:

    - The time anchor. Without it the model assumes today's date, emits
      ``date('now')`` against a 2024 dataset, and returns nothing. The system
      would appear broken while behaving exactly as written.
    - The enum values. If the prompt described categories the table does not
      contain, every generated query would filter on values that never match.
    - The token budget. The free tier allows 8,000 tokens per minute. A prompt
      that grows unnoticed silently reduces how many questions can be asked
      before rate limiting begins.

None of those produce an exception. They produce confidently wrong answers,
which is why they are pinned here.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.anomalies import DETECTORS
from app.data import CATEGORIES, PRIORITIES, STATUSES, TABLE_NAME
from app.prompts import (
    ANOMALY_TOOL,
    NARRATION_ROW_LIMIT,
    QUERY_TOOL,
    build_narration_messages,
    build_system_prompt,
    build_tool_schemas,
    render_anomaly_reports,
    render_rows,
    truncate_rows,
)

AS_OF = datetime(2024, 3, 30, 18, 6)

# Roughly four characters per token for English prose and code. Imprecise, but
# adequate for catching a prompt that has grown by hundreds of tokens.
CHARS_PER_TOKEN = 4

# Ceilings, not targets. Measured at ~667 and ~301 tokens; these leave room for
# sensible edits while failing loudly if the prompt doubles.
MAX_SYSTEM_PROMPT_TOKENS = 900
MAX_TOOL_SCHEMA_TOKENS = 450


def estimate_tokens(text: str) -> int:
    """Approximate the token count of a string.

    Args:
        text: The text to measure.

    Returns:
        An estimated token count.
    """
    return len(text) // CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# The time anchor - the single most important instruction in the prompt
# ---------------------------------------------------------------------------


def test_system_prompt_states_the_anchor_timestamp() -> None:
    """The reference date appears verbatim in the prompt."""
    prompt = build_system_prompt(AS_OF, row_count=500)

    assert "2024-03-30 18:06:00" in prompt


def test_system_prompt_forbids_using_the_real_date() -> None:
    """The prompt explicitly rules out resolving dates against today.

    Stating the anchor alone is not enough; a model will otherwise default to
    its own notion of "now". The instruction has to be explicit.
    """
    prompt = build_system_prompt(AS_OF, row_count=500).lower()

    assert "never against today" in prompt


def test_system_prompt_shows_relative_date_examples() -> None:
    """Worked date arithmetic is provided, not merely described.

    Two of the brief's five sample questions use "this week" and "this month",
    so these are the exact expressions the model must get right.
    """
    prompt = build_system_prompt(AS_OF, row_count=500)

    assert "-7 days" in prompt
    assert "start of month" in prompt
    # The examples must anchor on the timestamp, not on SQLite's 'now'.
    assert "'now'" not in prompt


def test_anchor_changes_with_the_supplied_time() -> None:
    """The prompt reflects whichever anchor it is given.

    An operator may pin ``AS_OF`` to reproduce past behaviour; a hard-coded
    date in the prompt would silently ignore that.
    """
    prompt = build_system_prompt(datetime(2023, 1, 15, 9, 0), row_count=42)

    assert "2023-01-15 09:00:00" in prompt
    assert "42 rows" in prompt


# ---------------------------------------------------------------------------
# Schema accuracy
# ---------------------------------------------------------------------------


def test_system_prompt_lists_every_enum_value() -> None:
    """Category, priority and status values match the data layer exactly.

    Read from the same constants the ingestion code validates against, so the
    prompt cannot describe a table that no longer exists.
    """
    prompt = build_system_prompt(AS_OF, row_count=500)

    for value in CATEGORIES | PRIORITIES | STATUSES:
        assert value in prompt


def test_system_prompt_names_the_table() -> None:
    """The table name comes from the data layer rather than being restated."""
    assert TABLE_NAME in build_system_prompt(AS_OF, row_count=500)


def test_system_prompt_explains_null_semantics() -> None:
    """The prompt describes when resolution time and rating are absent.

    Without this the model tends to add a redundant ``IS NOT NULL`` filter, or
    worse, treat missing values as zero.
    """
    prompt = build_system_prompt(AS_OF, row_count=500)

    assert "NULL unless Resolved" in prompt
    assert "Escalated" in prompt


def test_system_prompt_defines_unresolved() -> None:
    """"Unresolved" is defined to include Escalated.

    The brief's schema preview implies Escalated tickets carry a resolution
    time; the shipped data has none. The prompt follows the data.
    """
    prompt = build_system_prompt(AS_OF, row_count=500)

    assert "'Open', 'Escalated'" in prompt


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------


def test_system_prompt_stays_within_budget() -> None:
    """The prompt does not grow past its token ceiling.

    Every question costs this prompt plus the tool schemas, twice over per
    minute of use. Unnoticed growth directly reduces throughput on a
    tokens-per-minute quota.
    """
    tokens = estimate_tokens(build_system_prompt(AS_OF, row_count=500))

    assert tokens < MAX_SYSTEM_PROMPT_TOKENS


def test_tool_schemas_stay_within_budget() -> None:
    """The tool definitions do not grow past their token ceiling."""
    tokens = estimate_tokens(json.dumps(build_tool_schemas()))

    assert tokens < MAX_TOOL_SCHEMA_TOKENS


def test_prompt_omits_the_issue_summary_vocabulary() -> None:
    """The 26 distinct issue summaries are deliberately not enumerated.

    Listing them would add roughly 200 tokens to every call, to spare the model
    an occasional ``LIKE``. A poor trade against a per-minute ceiling, and a
    decision worth protecting from well-meaning future edits.
    """
    prompt = build_system_prompt(AS_OF, row_count=500)

    assert "Incorrect charge on invoice" not in prompt
    assert "Change notification preferences" not in prompt


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


def test_both_tools_are_offered() -> None:
    """The model is given exactly the query and anomaly tools."""
    names = [schema["function"]["name"] for schema in build_tool_schemas()]

    assert names == [QUERY_TOOL, ANOMALY_TOOL]


def test_query_tool_requires_sql() -> None:
    """The query tool declares ``sql`` as mandatory."""
    query_schema = build_tool_schemas()[0]["function"]

    assert query_schema["parameters"]["required"] == ["sql"]


def test_anomaly_tool_arguments_are_optional() -> None:
    """The anomaly tool can be called with no arguments.

    "Are there any anomalies?" is a complete question; requiring a detector
    name would force the model to guess one.
    """
    anomaly_schema = build_tool_schemas()[1]["function"]

    assert anomaly_schema["parameters"]["required"] == []


def test_anomaly_tool_enum_matches_the_registry() -> None:
    """Offered detector names are exactly those registered.

    Generated from the live registry, so a newly registered detector becomes
    available to the model with no second place to update - and the model can
    never be offered a detector that does not exist.
    """
    anomaly_schema = build_tool_schemas()[1]["function"]
    offered = anomaly_schema["parameters"]["properties"]["kind"]["enum"]

    assert offered == sorted(DETECTORS)


# ---------------------------------------------------------------------------
# Row capping and rendering
# ---------------------------------------------------------------------------


def test_small_result_sets_pass_through_untouched() -> None:
    """A result within the limit is not flagged as truncated."""
    rows = [{"n": index} for index in range(5)]
    shown, truncated = truncate_rows(rows)

    assert shown == rows
    assert truncated is False


def test_large_result_sets_are_capped() -> None:
    """A large result is reduced to the narration limit.

    The cap is what keeps a "list every ticket" question inside the token
    budget: 500 rows would cost roughly 15,000 tokens and exceed the
    per-minute ceiling on a single question.
    """
    rows = [{"n": index} for index in range(500)]
    shown, truncated = truncate_rows(rows)

    assert len(shown) == NARRATION_ROW_LIMIT
    assert truncated is True


def test_rendered_rows_report_the_full_total_when_capped() -> None:
    """Truncated output tells the model how many rows exist in total.

    Otherwise it would describe 20 rows as though they were the whole answer.
    """
    rows = [{"ticket_id": f"TKT-{index:03d}"} for index in range(NARRATION_ROW_LIMIT)]
    rendered = render_rows(rows, total=500, truncated=True)

    assert "[500 rows matched" in rendered
    assert f"showing first {NARRATION_ROW_LIMIT}]" in rendered


def test_empty_result_is_stated_plainly() -> None:
    """No rows renders as an explicit statement, not an empty string.

    An empty prompt section invites the model to fill the gap from memory.
    """
    assert "[0 rows matched]" in render_rows([], total=0, truncated=False)


def test_null_values_render_as_null() -> None:
    """Missing values appear as NULL rather than as Python's ``None``.

    ``None`` is a Python spelling; ``NULL`` is the SQL concept the model has
    been told about in the system prompt.
    """
    rendered = render_rows(
        [{"ticket_id": "TKT-001", "resolution_time_hrs": None}],
        total=1,
        truncated=False,
    )

    assert "NULL" in rendered
    assert "None" not in rendered


# ---------------------------------------------------------------------------
# Anomaly rendering
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_report() -> dict[str, object]:
    """Return a serialised report describing a clean run.

    Returns:
        A report mapping with no anomalies.
    """
    return {
        "kind": "sla_breach",
        "description": "Unresolved urgent tickets past their SLA",
        "method": "older than 24h",
        "threshold": 24.0,
        "considered": 12,
        "count": 0,
        "anomalies": [],
    }


def test_clean_report_still_states_method_and_threshold(
    empty_report: dict[str, object],
) -> None:
    """A report with no findings still describes what was checked.

    "No anomalies" is only meaningful alongside the rule applied. Without it
    the model cannot distinguish a clean result from a detector that did
    nothing.

    Args:
        empty_report: A report describing a clean run.
    """
    rendered = render_anomaly_reports([empty_report])

    assert "older than 24h" in rendered
    assert "24.0" in rendered
    assert "12" in rendered


def test_anomaly_rendering_includes_each_reason() -> None:
    """Every listed anomaly carries its justification into the prompt."""
    report = {
        "kind": "resolution_time_outlier",
        "description": "Slow resolutions",
        "method": "Tukey upper fence",
        "threshold": 48.15,
        "considered": 327,
        "count": 1,
        "anomalies": [
            {
                "ticket_id": "TKT-108",
                "priority": "High",
                "category": "General",
                "agent_id": "AGT-03",
                "reason": "Resolved in 119.7h, above the 48.1h threshold",
            }
        ],
    }
    rendered = render_anomaly_reports([report])

    assert "TKT-108" in rendered
    assert "119.7h" in rendered


def test_anomaly_listing_is_capped(empty_report: dict[str, object]) -> None:
    """A long anomaly list is truncated with the total stated.

    Args:
        empty_report: A report used as a template.
    """
    report = dict(empty_report)
    report["count"] = 80
    report["anomalies"] = [
        {
            "ticket_id": f"TKT-{index:03d}",
            "priority": "High",
            "category": "Billing",
            "agent_id": "AGT-01",
            "reason": "overdue",
        }
        for index in range(80)
    ]

    rendered = render_anomaly_reports([report])

    assert rendered.count("TKT-") == NARRATION_ROW_LIMIT
    assert "of 80" in rendered


# ---------------------------------------------------------------------------
# Narration messages
# ---------------------------------------------------------------------------


def test_narration_carries_question_and_evidence() -> None:
    """Both the question and the data reach the second call."""
    messages = build_narration_messages(
        "How many tickets are open?", evidence="open_tickets\n111", as_of=AS_OF
    )
    combined = " ".join(message["content"] for message in messages)

    assert "How many tickets are open?" in combined
    assert "111" in combined


def test_narration_forbids_inventing_figures() -> None:
    """The narration prompt rules out estimation.

    This is the instruction that keeps the model describing SQL results rather
    than supplying plausible numbers of its own.
    """
    system = build_narration_messages("q", evidence="data", as_of=AS_OF)[0]["content"]

    assert "Never estimate" in system
    assert "exactly" in system


def test_narration_prompt_is_small() -> None:
    """The second call does not repeat the schema or tool instructions.

    Once the data is in hand they are irrelevant, and repeating them would
    roughly double the cost of every question.
    """
    system = build_narration_messages("q", evidence="data", as_of=AS_OF)[0]["content"]

    # Raised from 200 in two deliberate steps. Every rule added was driven by
    # an observed failure against the live model, never by speculation:
    #   - a column of NULLs is a real result, not missing data
    #   - a partial list must state its total
    #   - a NULL resolution time *satisfies* "not resolved in time"
    #   - counts stay whole; only long decimals are rounded
    #
    # The number that matters is the proportion: this stays around 40% of the
    # selection prompt, and roughly a fifth of a question's total cost. If a
    # future edit pushes past this, the right response is to ask which rule has
    # stopped earning its place - not to raise the ceiling again by reflex.
    assert estimate_tokens(system) < 340
    # Column definitions and tool instructions must not reappear. The word
    # "tickets" itself is expected - it is the subject matter, not the schema.
    assert "resolution_time_hrs" not in system
    assert "customer_rating" not in system
    assert QUERY_TOOL not in system
