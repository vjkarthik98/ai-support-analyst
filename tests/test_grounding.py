"""Tests for :mod:`app.grounding`, the fabrication check.

Two failure directions matter equally here, and they pull against each other:

    - A **false negative** lets an invented figure reach the user. That is the
      failure the whole architecture exists to prevent.
    - A **false positive** discards a correct, well-phrased answer in favour of
      a blunt one. Less dangerous, but it degrades every good response, so the
      check must tolerate anything the model could legitimately have derived.

The permissive cases below are therefore as important as the strict ones. Most
are drawn from answers the live system actually produced.
"""

from __future__ import annotations

import pytest

from app.grounding import correct_hour_units, extract_numbers, ungrounded_numbers


def check(
    answer: str,
    rows: list[dict[str, object]],
    question: str = "How many tickets?",
    row_count: int = 1,
    reports: list[dict[str, object]] | None = None,
) -> list[str]:
    """Run the grounding check with concise arguments.

    Args:
        answer: The model's answer.
        rows: Result rows it was shown.
        question: The original question.
        row_count: Rows the query matched.
        reports: Anomaly reports it was shown.

    Returns:
        Figures with no source in the evidence.
    """
    return ungrounded_numbers(
        answer, rows=rows, reports=reports, question=question, row_count=row_count
    )


# ---------------------------------------------------------------------------
# Must stay clean - these are all legitimate answers
# ---------------------------------------------------------------------------


def test_exact_figure_from_a_row_is_grounded() -> None:
    """A number taken straight from the result is accepted."""
    assert check("111 tickets are currently open.", [{"n": 111}]) == []


def test_rounded_average_is_grounded() -> None:
    """Rounding a raw aggregate for display is not fabrication.

    The database returns 19.158; an answer saying 19.16 has rounded, not
    invented. Flagging this would fail every average the system produces.
    """
    assert check("The average is 19.16 hours.", [{"avg": 19.158}]) == []


def test_derived_percentage_is_grounded() -> None:
    """A percentage computed from two supplied figures is allowed.

    "What percentage were resolved?" invites exactly this calculation, so
    treating it as invention would punish the behaviour the question asked for.
    """
    assert check(
        "65.4% of tickets were resolved.",
        [{"resolved": 327, "total": 500}],
        question="What percentage of tickets have been resolved?",
    ) == []


def test_row_count_is_grounded() -> None:
    """The total row count may be cited even when absent from the rows shown."""
    assert check(
        "34 tickets matched.", [{"ticket_id": "TKT-060"}], row_count=34
    ) == []


def test_figure_from_the_question_is_grounded() -> None:
    """Repeating a number the user supplied is not invention."""
    assert check(
        "21 Technical tickets were resolved in under 5 hours.",
        [{"n": 21}],
        question="How many Technical tickets were resolved in under 5 hours?",
    ) == []


def test_years_are_treated_as_context() -> None:
    """A year is context, not a computed quantity."""
    assert check(
        "No tickets were created in 2023.",
        [{"n": 0}],
        question="How many tickets were created in 2023?",
    ) == []


def test_small_integers_in_prose_are_ignored() -> None:
    """Small counts appear constantly in ordinary phrasing.

    "Both of the 2 agents" is prose. Flagging numbers this small would produce
    noise without catching a fabrication worth catching.
    """
    assert check(
        "Both agents tied, each with 37 tickets.",
        [{"agent_id": "AGT-09", "n": 37}],
        row_count=2,
    ) == []


def test_figures_inside_an_anomaly_report_are_grounded() -> None:
    """Thresholds and per-ticket values from a report are legitimate."""
    report = {
        "threshold": 48.15,
        "count": 21,
        "considered": 327,
        "method": "Tukey upper fence: Q3 + 1.5 x IQR",
        "anomalies": [
            {"ticket_id": "TKT-108", "value": 119.7, "threshold": 48.15,
             "reason": "Resolved in 119.7h"}
        ],
    }
    assert check(
        "21 of 327 tickets exceeded the 48.15-hour threshold, the worst at 119.7h.",
        [],
        question="Any anomalies?",
        row_count=21,
        reports=[report],
    ) == []


# ---------------------------------------------------------------------------
# Must be flagged - these are fabrications
# ---------------------------------------------------------------------------


def test_invented_count_is_flagged() -> None:
    """A figure appearing nowhere in the evidence is caught.

    The live model produced exactly this shape - "about 400" - when it answered
    from memory instead of from the data.
    """
    assert check("There are about 400 open tickets.", [{"n": 111}]) == ["400"]


def test_invented_average_is_flagged() -> None:
    """A plausible but unsupported average is caught."""
    assert check("The average rating is 4.21.", [{"avg": 3.74}]) == ["4.21"]


def test_one_invented_figure_among_correct_ones_is_flagged() -> None:
    """A single unsupported number is caught even beside grounded ones.

    The dangerous shape in practice: mostly right, quietly wrong in one place.
    """
    assert check("111 are open and 87 are escalated.", [{"n": 111}]) == ["87"]


def test_invented_count_that_happens_to_be_a_ratio_is_flagged() -> None:
    """The percentage allowance applies only to figures written as percentages.

    22 / 40 = 55%, so a percentage of 55 is derivable from this evidence. A
    *count* of 55 is not - nothing here is 55 of anything. The allowance was
    once applied to every number, and with a few dozen grounded values nearly
    every figure from 0 to 100 is some ratio of two of them: an invented 55
    passed against a real twelve-row, per-agent result.
    """
    rows = [{"n": 40}, {"n": 22}, {"n": 110}]

    assert check("The busiest agent handled 55 tickets.", rows) == ["55"]
    # The same figure presented as the percentage it is remains legitimate.
    assert check("55% of them were escalated.", rows) == []
    assert check("55 percent of them were escalated.", rows) == []


def test_invented_figure_spelled_in_words_is_flagged() -> None:
    """A fabricated figure cannot escape the check by being written as words.

    The live model opened an answer with "Six tickets" rather than "6
    tickets". Six is small enough to be exempt, but the same habit applied
    to a larger figure would have put an unverified number in front of the
    user, because only digits were being checked.
    """
    assert check("Twenty-one tickets breached the SLA.", [{"n": 80}]) == ["21"]


# ---------------------------------------------------------------------------
# Number words - verified like digits, without flagging ordinary prose
# ---------------------------------------------------------------------------


def test_correct_figure_spelled_in_words_is_grounded() -> None:
    """A true figure written as words is accepted, not merely ignored."""
    assert check("Eighty tickets breached the SLA.", [{"n": 80}]) == []


def test_number_words_inside_other_words_are_not_numbers() -> None:
    """Only whole words are read as numbers.

    "someone", "often" and "none" contain "one" and "ten"; reading them as
    figures would flag ordinary prose and discard good answers.
    """
    assert check("Someone often reports none of these; 111 remain.", [{"n": 111}]) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("1,234 tickets", ["1234"], id="thousands separator"),
        pytest.param("3.74 average", ["3.74"], id="decimal"),
        pytest.param("no numbers here", [], id="none"),
        pytest.param("Six tickets", ["6"], id="word opening a sentence"),
        pytest.param("twenty-one and forty two", ["21", "42"], id="compound words"),
        pytest.param("seventy tickets", ["70"], id="tens word"),
        pytest.param("15 and fifteen", ["15", "15"], id="digits and words in order"),
        pytest.param("someone often has none", [], id="words inside words"),
    ],
)
def test_number_extraction(text: str, expected: list[str]) -> None:
    """Numbers are read in the forms answers actually use.

    A thousands separator must not split one figure into two, which would
    make "1,234" look like the unrelated numbers 1 and 234.

    Args:
        text: Text to scan.
        expected: Numbers that should be found.
    """
    assert extract_numbers(text) == expected


# ---------------------------------------------------------------------------
# Negative figures and hour units
# ---------------------------------------------------------------------------


def test_negative_figure_is_grounded_when_rounded() -> None:
    """A -0.078 correlation may be quoted as "-0.08" without being invented."""
    assert (
        ungrounded_numbers(
            "The correlation is -0.08, so there is no meaningful relationship.",
            rows=[{"correlation": -0.078}],
            reports=None,
            question="Is there a relationship?",
            row_count=1,
        )
        == []
    )


def test_hour_value_written_as_days_is_relabelled() -> None:
    """28.47 hours narrated as days is corrected to hours."""
    answer = correct_hour_units(
        "Low takes longest at 28.47 days.",
        rows=[{"priority": "Low", "avg_resolution_time": 28.47}],
    )

    assert answer == "Low takes longest at 28.47 hours."


def test_days_that_are_not_hour_values_are_left_alone() -> None:
    """A date range such as "the last 7 days" is not an hour value."""
    answer = correct_hour_units(
        "Over the last 7 days the average was 13.43 hours.",
        rows=[{"avg_resolution_hrs": 13.43}],
    )

    assert answer == "Over the last 7 days the average was 13.43 hours."


def test_hour_values_from_anomaly_reports_are_relabelled() -> None:
    """Detector values are hours too."""
    answer = correct_hour_units(
        "TKT-108 took 119.7 days.",
        rows=[],
        reports=[{"threshold": 48.15, "anomalies": [{"value": 119.7}]}],
    )

    assert answer == "TKT-108 took 119.7 hours."
