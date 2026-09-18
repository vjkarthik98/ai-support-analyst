"""Text formatting for the Streamlit interface, kept free of Streamlit itself.

Separated from :mod:`streamlit_app` for one reason: that module runs the whole
page when imported, so nothing inside it can be unit-tested. Anything here is a
plain function over strings and can be.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final

import pandas as pd

# Characters Markdown gives meaning to, plus "$", which Streamlit's renderer
# treats as the start of a LaTeX formula. CommonMark lets a backslash escape
# any ASCII punctuation, so escaping these is always safe to display.
_MARKDOWN_SPECIAL: Final[re.Pattern[str]] = re.compile(r"([\\`*_{}\[\]()<>#|$~!])")


def escape_markdown(text: str) -> str:
    """Make model-written text display literally when rendered as Markdown.

    Answers are written by a model and shown in a Markdown heading, where
    characters that are ordinary in prose change the output: two dollar signs
    turn the text between them into a formula, and asterisks or underscores
    into emphasis. A dataset quantity is not the model's formatting choice,
    and must appear exactly as written.

    Line breaks are collapsed to spaces as well, because a heading ends at the
    first one and the rest of the answer would lose its heading style.

    Args:
        text: The text to display.

    Returns:
        The text with Markdown-significant characters escaped and whitespace
        runs collapsed to single spaces.
    """
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))


# The format the API uses for timestamps, e.g. "2024-03-30 18:06:00".
_API_TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def format_reference_date(value: str) -> tuple[str, str]:
    """Split the API's reference timestamp into a readable date and time.

    "2024-03-30 18:06:00" becomes ``("30 Mar 2024", "18:06")``. Naming the
    month makes the date read the same in every convention - "03/04" is March
    in one country and April in another. Seconds are dropped because no answer
    depends on them. Returned as two parts because the sidebar shows the date
    as a large figure, where the full timestamp would be cut off.

    Args:
        value: A timestamp as the API returns it.

    Returns:
        A ``(date, time)`` pair, or ``(value, "")`` if the value is not in the
        expected format - showing the raw value beats showing nothing.
    """
    try:
        moment = datetime.strptime(value, _API_TIMESTAMP_FORMAT)
    except (TypeError, ValueError):
        return value, ""
    return f"{moment.day} {moment:%b %Y}", f"{moment:%H:%M}"


# Identifiers such as TKT-111 and AGT-05.
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"\b([A-Za-z]{2,}-)(\d+)\b")

# U+2060 WORD JOINER: invisible, and forbids a line break where it sits.
_WORD_JOINER: Final[str] = "\u2060"


def keep_identifiers_together(text: str) -> str:
    """Stop ticket and agent ids from breaking across lines.

    Browsers may break a line after a hyphen, so a long answer listing tickets
    wrapped as "TKT-" at the end of one line and "111" at the start of the
    next - an id the reader can no longer scan for. A word joiner after the
    hyphen forbids that break without changing what is displayed.

    Args:
        text: Text that may contain identifiers.

    Returns:
        The text with each identifier held on one line.
    """
    return _IDENTIFIER.sub(rf"\1{_WORD_JOINER}\2", text)


def format_duration(milliseconds: int) -> str:
    """Render an elapsed time for display.

    Args:
        milliseconds: Elapsed time in milliseconds.

    Returns:
        Seconds to one decimal place, e.g. "2.9 s".
    """
    return f"{milliseconds / 1000:.1f} s"


def format_tokens(total: int, *, estimated: bool) -> str:
    """Render a token count, marking one that is partly estimated.

    Args:
        total: Tokens across every model call for the answer.
        estimated: Whether part of the count was estimated because the
            provider did not report it.

    Returns:
        E.g. "2,126 tokens", or "~2,126 tokens" when estimated.
    """
    return f"{'~' if estimated else ''}{total:,} tokens"


# Readable names for the detectors the API reports by identifier. An unknown
# identifier - a detector added later - still reads sensibly via the fallback.
_DETECTOR_LABELS: Final[dict[str, str]] = {
    "resolution_time_outlier": "Resolution-time outliers",
    "sla_breach": "SLA breaches",
}


def detector_label(kind: str) -> str:
    """Name a detector for a person rather than a program.

    Args:
        kind: The detector's identifier, e.g. "sla_breach".

    Returns:
        A readable name, e.g. "SLA breaches".
    """
    return _DETECTOR_LABELS.get(kind, kind.replace("_", " ").capitalize())


def answer_metadata(payload: dict[str, Any]) -> str:
    """Build the one-line summary of how an answer was produced.

    Rows, tool, time and tokens are supporting facts. Shown as four large
    metrics they outweighed the answer itself - "query_tickets" in the biggest
    type on the page - so they are rendered as a quiet line of badges instead.

    Args:
        payload: A ``/query`` response body.

    Returns:
        Markdown for a row of badges.
    """
    rows = payload["row_count"]
    tool = payload["tool"] or "no tool"
    tokens = payload["prompt_tokens"] + payload["completion_tokens"]
    parts = [
        f":gray-badge[:material/table_rows: {rows:,} row{'s' if rows != 1 else ''}]",
        f":gray-badge[:material/build: {tool}]",
        f":gray-badge[:material/timer: {format_duration(payload['elapsed_ms'])}]",
        f":gray-badge[:material/data_usage: "
        f"{format_tokens(tokens, estimated=bool(payload.get('tokens_estimated')))}]",
    ]
    return " ".join(parts)


def blank_missing(frame: pd.DataFrame) -> pd.DataFrame:
    """Show missing values in a results table as a dash.

    Unresolved tickets have no resolution time or rating, and the table
    rendered those cells as the word "None" - Python's name for nothing, not
    a reader's. Only columns that contain a missing value are changed, and
    every other value in them is shown exactly as it came: 119.7 stays 119.7,
    never 119.700000.

    The values are converted rather than styled. Streamlit's table ignores a
    pandas Styler's missing-value setting, so styling left "None" on screen
    while a test of the Styler's own HTML passed.

    Args:
        frame: Rows to display.

    Returns:
        The frame itself when nothing is missing; otherwise a copy in which
        each affected column holds text, with "—" for every missing cell.
    """
    missing = [column for column in frame.columns if frame[column].isna().any()]
    if not missing:
        return frame
    shown = frame.copy()
    for column in missing:
        shown[column] = shown[column].map(lambda value: "—" if pd.isna(value) else _exact(value))
    return shown


def _exact(value: Any) -> str:
    """Render a present value exactly, without a trailing ".0".

    ``f"{value:g}"`` would be shorter and is wrong: it keeps six significant
    digits, silently turning 2118.567 into 2118.57.

    Args:
        value: A cell value that is not missing.

    Returns:
        The value as text, with whole floats shown as integers.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
