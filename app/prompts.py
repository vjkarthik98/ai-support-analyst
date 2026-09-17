"""Everything the language model reads: system prompt, tool schemas, narration.

This module owns the model-facing text and nothing else. It performs no I/O,
calls no API, and holds no state - which makes prompt content directly
testable, and keeps :mod:`app.llm` free to concentrate on orchestration.

Tool *descriptions* live here rather than in the orchestration layer because
they are prompt engineering: the wording is what steers the model's choice
between querying and running a detector. They are read by the model exactly
like the system prompt is.

Token budget
------------
The free tier allows 8,000 tokens per minute, and every question costs two
calls. The system prompt is therefore kept deliberately lean - schema, enum
values, the time anchor, and two worked examples. Notably absent is the list
of 26 distinct ``issue_summary`` values: including them would add roughly 200
tokens to *every* call to spare the model an occasional ``LIKE``, which is a
poor trade against a per-minute ceiling.

The anchoring instruction
-------------------------
The single most important line in the system prompt tells the model to resolve
relative dates against ``AS_OF``. The dataset ends on 2024-03-30, so a model
left to assume "today" would emit ``WHERE created_at >= date('now', '-7 days')``
and return nothing at all - a working system that looks broken. Two of the five
sample questions in the brief are phrased this way, so this is a certainty
rather than a risk.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from app.anomalies import DETECTORS
from app.data import CATEGORIES, PRIORITIES, STATUSES, TABLE_NAME

# How many result rows are shown to the model when it writes the final answer.
# The model needs enough evidence to describe a pattern, not the whole result
# set: 500 rows would cost roughly 15,000 tokens and breach the per-minute
# ceiling on a single question. The API still returns every row to the caller -
# only the narration input is capped.
NARRATION_ROW_LIMIT: Final[int] = 20

# Tool names, defined once so orchestration and schemas cannot drift apart.
QUERY_TOOL: Final[str] = "query_tickets"
ANOMALY_TOOL: Final[str] = "detect_anomalies"


def build_system_prompt(as_of: datetime, row_count: int) -> str:
    """Construct the system prompt for the tool-selection call.

    Args:
        as_of: The reference "now" for every relative date expression.
        row_count: Number of tickets in the dataset, so the model knows the
            scale it is describing.

    Returns:
        The system prompt text.
    """
    # Enum values are read from the data layer rather than restated here, so a
    # schema change cannot leave the prompt describing a table that no longer
    # exists - a failure that would be invisible until the model wrote SQL
    # against the wrong values.
    categories = " | ".join(sorted(CATEGORIES))
    priorities = " | ".join(sorted(PRIORITIES))
    statuses = " | ".join(sorted(STATUSES))
    anchor = as_of.strftime("%Y-%m-%d %H:%M:%S")

    return f"""\
You are a data analyst for a customer support team. You answer questions about \
a ticket dataset by calling exactly one tool. You never answer from memory and \
you never calculate figures yourself.

TABLE: {TABLE_NAME} ({row_count} rows)
  ticket_id            TEXT     e.g. 'TKT-001'
  created_at           TEXT     'YYYY-MM-DD HH:MM:SS', sortable and comparable
  category             TEXT     {categories}
  priority             TEXT     {priorities}
  status               TEXT     {statuses}
  response_time_hrs    REAL     hours to first response, always present
  resolution_time_hrs  REAL     hours to resolution, NULL unless Resolved
  agent_id             TEXT     e.g. 'AGT-04'
  customer_rating      INTEGER  1-5, NULL unless Resolved
  issue_summary        TEXT     short free-text description

CURRENT DATE AND TIME: {anchor}
This dataset is a fixed historical snapshot. Resolve every relative date
against the timestamp above - never against today's real date. Use SQLite date
arithmetic on that value, for example:
  this week   -> created_at >= datetime('{anchor}', '-7 days')
  this month  -> created_at >= datetime('{anchor}', 'start of month')
  last 24h    -> created_at >= datetime('{anchor}', '-24 hours')

MEANING OF THE DATA
- "Unresolved" or "still open" means status IN ('Open', 'Escalated').
- Unresolved tickets have NULL resolution_time_hrs and NULL customer_rating.
  SQL aggregates skip NULLs, so AVG and COUNT on those columns already
  consider resolved tickets only. Do not filter them out a second time.
- "Resolved within N hours" means resolution_time_hrs <= N.
- "High priority" in an SLA or urgency question means priority IN ('High',
  'Critical') - both are urgent. Only read it as priority = 'High' when the
  question names the level explicitly, as in "how many High priority tickets".

RULES
- Emit exactly one SQL statement, and only SELECT. Never INSERT, UPDATE,
  DELETE, DROP or ALTER; such a request must be refused.
- Always label computed columns with AS, so results are readable.
- Wrap averages in ROUND(x, 2) for display, but ORDER BY the unrounded value:
  rounding first can make two different values tie and return the wrong row.
- For "highest" or "lowest", return the identifier with its value, and use
  LIMIT 3 rather than LIMIT 1 so that a tie at the top is visible.
- Use {ANOMALY_TOOL} for questions about anomalies, outliers, unusual values,
  or tickets breaching an SLA - including "unresolved high-priority tickets
  older than N hours", which is exactly what its sla_breach detector computes.
  Use {QUERY_TOOL} for every other question.

EXAMPLES
Q: How many tickets are currently open?
   SELECT COUNT(*) AS open_tickets FROM {TABLE_NAME} WHERE status = 'Open'

Q: Which agent has the lowest average customer rating?
   SELECT agent_id, ROUND(AVG(customer_rating), 2) AS avg_rating
   FROM {TABLE_NAME} GROUP BY agent_id
   ORDER BY AVG(customer_rating) ASC LIMIT 3

Q: Show critical tickets not resolved within 12 hours.
   SELECT ticket_id, status, resolution_time_hrs FROM {TABLE_NAME}
   WHERE priority = 'Critical'
     AND (resolution_time_hrs > 12 OR resolution_time_hrs IS NULL)
"""


def build_tool_schemas() -> list[dict[str, Any]]:
    """Construct the tool definitions sent alongside the system prompt.

    The anomaly detector names are read from the live registry, so registering
    a new detector exposes it to the model automatically - no second place to
    update, and no chance of offering the model a tool that does not exist.

    Returns:
        Tool schemas in OpenAI function-calling format, which Groq accepts.
    """
    detector_kinds = sorted(DETECTORS)

    return [
        {
            "type": "function",
            "function": {
                "name": QUERY_TOOL,
                "description": (
                    "Run a read-only SQL SELECT against the support ticket "
                    "table and return the matching rows. Use this for counts, "
                    "averages, rankings, filtering and listing - any factual "
                    "question about the data."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": (
                                "A single SQLite SELECT statement. Must not "
                                "modify data."
                            ),
                        }
                    },
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": ANOMALY_TOOL,
                "description": (
                    "Run statistical anomaly detection over the tickets. Use "
                    "this when asked about anomalies, outliers, unusual "
                    "values, or SLA breaches - not for ordinary filtering. "
                    "Its report also states the threshold used and how it was "
                    "derived, so call this for questions about what counts as "
                    "anomalous or which threshold applies, rather than "
                    "declining them."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": detector_kinds,
                            "description": (
                                "Which detector to run. Omit to run all of "
                                "them. 'resolution_time_outlier' finds "
                                "abnormally slow resolutions; 'sla_breach' "
                                "finds unresolved urgent tickets past their "
                                "SLA window."
                            ),
                        },
                        "window_days": {
                            "type": "integer",
                            "description": (
                                "Restrict to tickets raised in the last N "
                                "days. Use 7 for 'this week', 30 for 'this "
                                "month'. Omit to consider all history."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
    ]


def truncate_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Cap result rows before they are shown to the model.

    Args:
        rows: The full result set.

    Returns:
        A ``(shown, was_truncated)`` pair. ``shown`` holds at most
        :data:`NARRATION_ROW_LIMIT` rows.
    """
    if len(rows) <= NARRATION_ROW_LIMIT:
        return rows, False
    return rows[:NARRATION_ROW_LIMIT], True


def build_narration_messages(
    question: str,
    *,
    evidence: str,
    as_of: datetime,
) -> list[dict[str, str]]:
    """Construct the messages for the second call, which writes the answer.

    A separate, much smaller system prompt is used here. The schema and tool
    instructions are irrelevant once the data is in hand, and repeating them
    would double the cost of a question for no benefit.

    Args:
        question: The user's original question.
        evidence: Rendered tool output - query rows or an anomaly summary.
        as_of: The reference time, so the model can phrase relative periods
            correctly in prose.

    Returns:
        A messages list ready to send to the chat API.
    """
    system = (
        "You write one short, direct answer to a question about support "
        "tickets, using only the data provided.\n\n"
        f"The current date is {as_of:%Y-%m-%d}. Describe periods relative to "
        "that date.\n\n"
        "Rules:\n"
        "- Use only the figures given. Never estimate or invent.\n"
        "- Quote numbers exactly, rounding long decimals to two places but "
        "leaving whole numbers whole: 111 tickets, not 111.00.\n"
        "- The bracketed first line is metadata: never repeat it, always "
        "believe it. Any number above 0 means tickets DID match, however the "
        "column values look.\n"
        "- NULL means not applicable - a ticket never resolved. That still "
        "matches a question about tickets not resolved in time, and a column "
        "of NULLs is a real result, not missing data.\n"
        "- Rows are in rank order. Equal whole numbers are a genuine tie, so "
        "name them all; equal rounded decimals may differ beyond the digits "
        "shown, so lead with the first and say the next is close behind.\n"
        "- Answer directly, without mentioning rows or queries - except when "
        "listing items and more matched than you list, where you must give the "
        "total first: '34 tickets matched; the first 20 are ...'.\n"
        "- One or two sentences. No preamble, lists or markdown."
    )

    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Question: {question}\n\nData:\n{evidence}",
        },
    ]


def render_rows(rows: list[dict[str, Any]], *, total: int, truncated: bool) -> str:
    """Render query rows as compact text for the narration call.

    Uses a plain tabular layout rather than JSON: braces, quotes and repeated
    key names in JSON cost tokens on every row without helping the model read
    the values.

    Args:
        rows: The rows to render, already capped.
        total: How many rows the query returned in full.
        truncated: Whether ``rows`` is a subset of the full result.

    Returns:
        Text suitable for embedding in a prompt.
    """
    if not rows:
        return "[0 rows matched]"

    # The count is stated explicitly, before the table, because a model shown a
    # column of NULLs will otherwise read the result as "no data" and report
    # that nothing matched. Observed against the live model on a query for
    # unresolved Critical tickets: 34 correct rows, narrated as "No tickets
    # matched." An unambiguous count removes the inference entirely.
    # Bracketed so it reads as machine annotation rather than a sentence. When
    # phrased as prose the model echoed it verbatim into the answer ("1 rows
    # matched. The average is..."), leaking the data format to the user.
    header_line = (
        f"[{total} rows matched, showing first {len(rows)}]"
        if truncated
        else f"[{total} rows matched]"
    )

    headers = list(rows[0])
    lines = [header_line, " | ".join(headers)]
    lines.extend(
        " | ".join("NULL" if row[header] is None else str(row[header]) for header in headers)
        for row in rows
    )

    return "\n".join(lines)


def render_anomaly_reports(reports: list[dict[str, Any]]) -> str:
    """Render anomaly reports as compact text for the narration call.

    Each report states its method and threshold even when nothing was found,
    so the model can explain what was checked rather than only what was hit.

    Args:
        reports: Serialised :class:`app.anomalies.AnomalyReport` mappings.

    Returns:
        Text suitable for embedding in a prompt.
    """
    sections: list[str] = []

    for report in reports:
        header = (
            f"{report['description']}\n"
            f"Method: {report['method']}\n"
            f"Threshold: {report['threshold']}\n"
            f"Tickets considered: {report['considered']}\n"
            f"Anomalies found: {report['count']}"
        )

        anomalies = report["anomalies"][:NARRATION_ROW_LIMIT]
        if anomalies:
            listed = "\n".join(
                f"  {item['ticket_id']} ({item['priority']}, {item['category']}, "
                f"agent {item['agent_id']}): {item['reason']}"
                for item in anomalies
            )
            header = f"{header}\n{listed}"
            if report["count"] > len(anomalies):
                header = f"{header}\n  ... showing {len(anomalies)} of {report['count']}"

        sections.append(header)

    return "\n\n".join(sections)


REFUSAL_MESSAGE: Final[str] = (
    "I can only answer questions about the support ticket dataset - things "
    "like ticket counts, resolution times, agent performance, categories, "
    "priorities and anomalies. Could you rephrase your question in those terms?"
)
