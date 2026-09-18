"""Tests for :mod:`ui.formatting`, the interface's text handling.

The Streamlit page itself runs when imported, so it cannot be unit-tested.
What can be tested lives in ``ui/formatting.py``, which is imported here the
same way Streamlit imports it: as a sibling of the page script.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# Streamlit puts the page script's folder on sys.path and the page imports
# ``formatting`` from there. The test does the same, so it exercises the import
# the running interface actually performs.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))

from formatting import (  # noqa: E402 - needs the path above
    answer_metadata,
    blank_missing,
    detector_label,
    escape_markdown,
    format_duration,
    format_reference_date,
    format_tokens,
    keep_identifiers_together,
)


@pytest.mark.parametrize(
    ("answer", "escaped"),
    [
        pytest.param("Refunds of $20 and $35 were issued.", r"Refunds of \$20 and \$35 were issued.", id="dollar pair"),
        pytest.param("AGT_01 and AGT_02 tied.", r"AGT\_01 and AGT\_02 tied.", id="underscores"),
        pytest.param("Rated *3* on average.", r"Rated \*3\* on average.", id="asterisks"),
        pytest.param("See [TKT-001](x).", r"See \[TKT-001\]\(x\).", id="link syntax"),
    ],
)
def test_markdown_characters_display_literally(answer: str, escaped: str) -> None:
    """Characters Markdown would interpret are escaped.

    Two dollar signs made Streamlit render the text between them as a LaTeX
    formula, and underscores or asterisks turned into emphasis - altering an
    answer that must show the data exactly.

    Args:
        answer: Text as the model wrote it.
        escaped: The text as it must be passed to Markdown.
    """
    assert escape_markdown(answer) == escaped


def test_line_breaks_do_not_end_the_heading_early() -> None:
    """A multi-line answer stays one heading.

    The answer is shown as a Markdown heading, which ends at the first line
    break - everything after it lost the heading style.
    """
    assert escape_markdown("111 tickets are open.\n\nMost are Billing.") == (
        "111 tickets are open. Most are Billing."
    )


def test_plain_answers_are_unchanged() -> None:
    """An answer with nothing to escape passes through untouched."""
    answer = "111 tickets are currently open."

    assert escape_markdown(answer) == answer


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("2024-03-30 18:06:00", ("30 Mar 2024", "18:06"), id="dataset anchor"),
        pytest.param("2024-03-05 09:07:00", ("5 Mar 2024", "09:07"), id="single-digit day"),
        pytest.param("not a date", ("not a date", ""), id="unexpected format"),
    ],
)
def test_reference_date_is_split_for_display(value: str, expected: tuple[str, str]) -> None:
    """The sidebar's reference date reads unambiguously and fits its card.

    The month is named so the date reads the same in every convention, and
    the time is returned separately: in the large metric font the full
    timestamp would be cut off. An unexpected value is shown as it came
    rather than hidden.

    Args:
        value: A timestamp as the API returns it.
        expected: The ``(date, time)`` pair to display.
    """
    assert format_reference_date(value) == expected


def test_identifiers_never_break_across_lines() -> None:
    """A word joiner after the hyphen holds each id on one line.

    Long answers wrapped as "TKT-" at the end of one line and "111" at the
    start of the next. The joiner is invisible, so the text reads the same.
    """
    shown = keep_identifiers_together("See TKT-111 and AGT-05, not e-mail.")

    assert shown == "See TKT-\u2060111 and AGT-\u206005, not e-mail."
    assert shown.replace("\u2060", "") == "See TKT-111 and AGT-05, not e-mail."


@pytest.mark.parametrize(
    ("total", "estimated", "expected"),
    [
        pytest.param(2126, False, "2,126 tokens", id="reported"),
        pytest.param(2126, True, "~2,126 tokens", id="estimated"),
    ],
)
def test_token_counts_mark_estimates(total: int, estimated: bool, expected: str) -> None:
    """An estimated count is visibly marked as one.

    Args:
        total: Tokens used.
        estimated: Whether the count was partly estimated.
        expected: The display text.
    """
    assert format_tokens(total, estimated=estimated) == expected


def test_duration_is_shown_in_seconds() -> None:
    """Milliseconds are shown as seconds to one decimal place."""
    assert format_duration(2874) == "2.9 s"


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        pytest.param("sla_breach", "SLA breaches", id="known detector"),
        pytest.param("resolution_time_outlier", "Resolution-time outliers", id="known detector 2"),
        pytest.param("agent_workload_spike", "Agent workload spike", id="future detector"),
    ],
)
def test_detectors_have_readable_names(kind: str, label: str) -> None:
    """Detector ids are shown as names, including ones added later.

    Args:
        kind: The detector's identifier.
        label: The name to show.
    """
    assert detector_label(kind) == label


def test_answer_metadata_summarises_how_it_was_produced() -> None:
    """Rows, tool, time and tokens appear as one quiet line of badges."""
    line = answer_metadata(
        {
            "row_count": 34,
            "tool": "query_tickets",
            "elapsed_ms": 2874,
            "prompt_tokens": 1900,
            "completion_tokens": 226,
            "tokens_estimated": False,
        }
    )

    for part in ("34 rows", "query_tickets", "2.9 s", "2,126 tokens"):
        assert part in line
    assert line.count(":gray-badge[") == 4


def test_missing_values_show_as_a_dash_and_the_rest_exactly() -> None:
    """Missing cells read "—"; present values are unrounded and unpadded.

    The table showed unresolved tickets' resolution time as "None". The fix
    must not alter real values: 2118.567 stays 2118.567, and 12.0 reads 12.
    Checked on the values themselves: an earlier version styled the table
    instead, which passed a test of the style's HTML while Streamlit went on
    showing "None".
    """
    frame = pd.DataFrame(
        {"ticket_id": ["TKT-1", "TKT-2", "TKT-3"], "resolution_time_hrs": [2118.567, None, 12.0]}
    )

    shown = blank_missing(frame)

    assert shown["resolution_time_hrs"].tolist() == ["2118.567", "—", "12"]
    # Columns with nothing missing are untouched, and so is the original.
    assert shown["ticket_id"].tolist() == ["TKT-1", "TKT-2", "TKT-3"]
    assert frame["resolution_time_hrs"].isna().sum() == 1


def test_a_table_with_nothing_missing_is_left_alone() -> None:
    """No styling is applied when every value is present."""
    frame = pd.DataFrame({"n": [1, 2]})

    assert blank_missing(frame) is frame
