"""Verification that an answer's figures actually came from the data.

The architecture's central claim is that the model never performs arithmetic:
it writes SQL, the database computes, and the model only phrases the result.
Until now that claim rested on design and instruction - forced tool calls, a
prompt forbidding invention, narration fed only real rows.

That is necessary but not sufficient. **A prompt is a request, not a
guarantee.** This project has already been bitten twice by assuming otherwise:
the model reported "No tickets matched" while holding 34 rows, and answered
"Paris." to a question it had correctly refused, both in direct contradiction
of explicit instructions.

This module closes the gap by *checking*. Every number in an answer must be
traceable to the evidence that produced it. When one is not, the model invented
it, and the narration is discarded in favour of a deterministic summary.

The difference matters when explaining the system: "we told it not to
hallucinate" and "we verify that it did not" are answers of very different
strength.

Being deliberately permissive
-----------------------------
A false positive here is costly - it would replace a good answer with a blunt
one - so the check allows anything the model could legitimately have derived:

    - any figure appearing in the result rows or the anomaly report
    - any figure appearing in the question itself
    - the row count, which the model is shown and told to cite
    - percentages and rounded forms of a grounded figure
    - small integers, which are almost always prose ("two of the three")
    - years and dates, which are context rather than computed values

What remains after those allowances is a number with no visible source - which
is the definition of a fabricated one.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

logger = logging.getLogger(__name__)

# Integers at or below this are ignored. They appear constantly in ordinary
# prose - "the top 3", "both of the 2" - and flagging them would produce noise
# without catching a fabrication worth catching.
SMALL_INTEGER_LIMIT: Final[int] = 10

# Four-digit values in this range are treated as years rather than quantities.
_YEAR_RANGE: Final[range] = range(1900, 2100)

_NUMBER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)?")


def extract_numbers(text: str) -> list[str]:
    """Find every numeric token in a piece of text.

    Args:
        text: The text to scan.

    Returns:
        Numbers as they were written, preserving their original precision.
    """
    # Thousands separators are removed first so "1,234" reads as one number
    # rather than as "1" and "234".
    return _NUMBER_PATTERN.findall(text.replace(",", ""))


def _grounded_values(
    rows: list[dict[str, Any]],
    reports: list[dict[str, Any]] | None,
    question: str,
    row_count: int,
) -> set[str]:
    """Collect every figure the model was legitimately given.

    Args:
        rows: Result rows shown to the model.
        reports: Anomaly reports shown to the model, if any.
        question: The user's question, whose own numbers are fair to repeat.
        row_count: Total rows the query matched.

    Returns:
        The numeric tokens an answer may contain, in several written forms.
    """
    values: set[str] = {str(row_count)}

    def record(value: Any) -> None:
        """Add a value in every form it might reasonably be written.

        Args:
            value: A figure from the evidence.
        """
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            text = f"{value}"
            values.add(text)
            values.add(text.rstrip("0").rstrip("."))
            values.add(f"{value:.0f}")
            values.add(f"{value:.1f}")
            values.add(f"{value:.2f}")
            # An answer may reasonably round 19.158 to 19.16 or to 19.2.
            for places in (0, 1, 2):
                values.add(f"{round(float(value), places)}")
        elif isinstance(value, str):
            values.update(extract_numbers(value))

    for row in rows:
        for cell in row.values():
            record(cell)

    for report in reports or []:
        for key in ("threshold", "count", "considered"):
            record(report.get(key))
        for item in report.get("anomalies", []):
            for key in ("value", "threshold"):
                record(item.get(key))
            record(item.get("reason"))
            record(item.get("ticket_id"))
        record(report.get("method"))

    values.update(extract_numbers(question))
    return values


def ungrounded_numbers(
    answer: str,
    *,
    rows: list[dict[str, Any]],
    reports: list[dict[str, Any]] | None,
    question: str,
    row_count: int,
) -> list[str]:
    """Return figures in the answer that appear nowhere in the evidence.

    Args:
        answer: The model's written answer.
        rows: Result rows it was shown.
        reports: Anomaly reports it was shown, if any.
        question: The original question.
        row_count: Total rows the query matched.

    Returns:
        Every unexplained figure, in the order they appear. An empty list means
        the answer is fully grounded.
    """
    grounded = _grounded_values(rows, reports, question, row_count)
    unexplained: list[str] = []

    for token in extract_numbers(answer):
        if token in grounded:
            continue

        try:
            number = float(token)
        except ValueError:  # pragma: no cover - the pattern only matches numbers
            continue

        # Small counts and years are prose or context, not computed claims.
        if number.is_integer():
            if abs(number) <= SMALL_INTEGER_LIMIT:
                continue
            if int(number) in _YEAR_RANGE and len(token) == 4:
                continue

        # A percentage the model derived from two grounded figures is a
        # legitimate calculation, not an invention.
        if _is_plausible_percentage(number, grounded):
            continue

        unexplained.append(token)

    return unexplained


def _is_plausible_percentage(number: float, grounded: set[str]) -> bool:
    """Report whether a figure could be a percentage of grounded values.

    Asking "what percentage were resolved?" invites the model to divide two
    figures it was given. Treating that as fabrication would punish exactly the
    behaviour the question asked for.

    Args:
        number: The unexplained figure.
        grounded: Figures the model was given.

    Returns:
        ``True`` when the figure is a percentage derivable from the evidence.
    """
    if not 0 <= number <= 100:
        return False

    numeric: list[float] = []
    for value in grounded:
        try:
            numeric.append(float(value))
        except ValueError:
            continue

    for numerator in numeric:
        for denominator in numeric:
            if denominator <= 0:
                continue
            if abs((numerator / denominator) * 100 - number) < 0.5:
                return True
    return False
