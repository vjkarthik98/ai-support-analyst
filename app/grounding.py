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
    - any figure in the evidence text the model read, including its framing
      ("[34 rows matched, showing first 20]")
    - any figure appearing in the question itself
    - the row count, which the model is shown and told to cite
    - rounded forms of a grounded figure, and percentages derivable from the
      evidence - but only figures written as percentages
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

# Number words, so a figure spelled out is verified like one written in digits.
# Without this, "twenty-one tickets" would pass the check unseen whatever the
# evidence said: the pattern above only recognises digits. The model is told
# to write digits, but a prompt is a request - this makes the check hold even
# when the request is ignored. Words above ninety-nine ("a hundred") are not
# covered; answers use digits for figures that large in practice.
_UNIT_WORDS: Final[dict[str, int]] = {
    word: value
    for value, word in enumerate(
        (
            "zero one two three four five six seven eight nine ten eleven "
            "twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
        ).split()
    )
}
_TENS_WORDS: Final[dict[str, int]] = {
    word: value
    for value, word in zip(
        range(20, 100, 10),
        "twenty thirty forty fifty sixty seventy eighty ninety".split(),
        strict=True,
    )
}
_WORD_NUMBER_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"\b(?:(?P<tens>{'|'.join(_TENS_WORDS)})"
    rf"(?:[\s-](?P<unit>{'|'.join(list(_UNIT_WORDS)[1:10])}))?"
    rf"|(?P<single>{'|'.join(_UNIT_WORDS)}))\b",
    re.IGNORECASE,
)


def _spell_numbers_as_digits(text: str) -> str:
    """Rewrite number words from zero to ninety-nine as digits.

    Args:
        text: The text to rewrite.

    Returns:
        The text with each number word replaced by its digits, in place, so
        the order of figures is preserved.
    """

    def to_digits(match: re.Match[str]) -> str:
        """Convert one matched number word to its digits.

        Args:
            match: A match of :data:`_WORD_NUMBER_PATTERN`.

        Returns:
            The value as a string of digits.
        """
        if match.group("single"):
            return str(_UNIT_WORDS[match.group("single").lower()])
        value = _TENS_WORDS[match.group("tens").lower()]
        if match.group("unit"):
            value += _UNIT_WORDS[match.group("unit").lower()]
        return str(value)

    return _WORD_NUMBER_PATTERN.sub(to_digits, text)


def extract_numbers(text: str) -> list[str]:
    """Find every numeric figure in a piece of text, in digits or in words.

    Args:
        text: The text to scan.

    Returns:
        Numbers in the order they appear. Digits keep their original
        precision; number words are returned as their digits.
    """
    return [token for token, _ in _numbers_with_units(text)]


# A figure followed by a percent sign or the word, allowing a space between.
_PERCENT_SUFFIX: Final[re.Pattern[str]] = re.compile(r"\s*(?:%|per\s?cent\b)", re.IGNORECASE)


def _numbers_with_units(text: str) -> list[tuple[str, bool]]:
    """Find every numeric figure, noting which are written as percentages.

    Args:
        text: The text to scan.

    Returns:
        ``(token, is_percentage)`` pairs in the order the figures appear.
    """
    # Thousands separators are removed first so "1,234" reads as one number
    # rather than as "1" and "234".
    normalised = _spell_numbers_as_digits(text.replace(",", ""))
    return [
        (match.group(), bool(_PERCENT_SUFFIX.match(normalised, match.end())))
        for match in _NUMBER_PATTERN.finditer(normalised)
    ]


def _grounded_values(
    rows: list[dict[str, Any]],
    reports: list[dict[str, Any]] | None,
    question: str,
    row_count: int,
    evidence: str = "",
) -> set[str]:
    """Collect every figure the model was legitimately given.

    Args:
        rows: Result rows shown to the model.
        reports: Anomaly reports shown to the model, if any.
        question: The user's question, whose own numbers are fair to repeat.
        row_count: Total rows the query matched.
        evidence: The rendered evidence exactly as the model read it.

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

    # The evidence text exactly as the model read it. Beyond the rows it holds
    # framing such as "[34 rows matched, showing first 20]", and the narration
    # prompt asks the model to cite that sample size: "34 tickets matched; the
    # first 20 are ...". Checked only against rows and reports, the "20" was
    # rejected - the check refusing the very answer the prompt requested - and
    # three correct benchmark answers were replaced by the plain summary.
    # Anything the model was shown is, by definition, not invented.
    values.update(extract_numbers(evidence))
    return values


def ungrounded_numbers(
    answer: str,
    *,
    rows: list[dict[str, Any]],
    reports: list[dict[str, Any]] | None,
    question: str,
    row_count: int,
    evidence: str = "",
) -> list[str]:
    """Return figures in the answer that appear nowhere in the evidence.

    Args:
        answer: The model's written answer.
        rows: Result rows it was shown.
        reports: Anomaly reports it was shown, if any.
        question: The original question.
        row_count: Total rows the query matched.
        evidence: The rendered evidence exactly as the model read it.

    Returns:
        Every unexplained figure, in the order they appear. An empty list means
        the answer is fully grounded.
    """
    grounded = _grounded_values(rows, reports, question, row_count, evidence)
    unexplained: list[str] = []

    for token, is_percentage in _numbers_with_units(answer):
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
        # legitimate calculation, not an invention - but only a figure the
        # answer actually presents as a percentage qualifies. Applied to every
        # number, this allowance let invented counts through: with a few dozen
        # grounded values, nearly every number from 0 to 100 is *some* ratio of
        # two of them, so "The busiest agent handled 55 tickets" passed against
        # a result that never contained 55.
        if is_percentage and _is_plausible_percentage(number, grounded):
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
