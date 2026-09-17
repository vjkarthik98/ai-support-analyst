"""Validation of model-generated SQL before it reaches the database.

The natural-language pipeline lets a language model write SQL. That text is
untrusted input - not because the model is adversarial, but because a model
can be wrong, and a user can ask it to be wrong ("delete all the tickets").
This module decides whether a given statement is a single, read-only
``SELECT`` and rejects everything else.

Where this sits in the defence
------------------------------
This is the *first* of two independent barriers, not the only one:

    1. This module inspects the SQL **text** before execution.
    2. :func:`app.data.get_connection` opens SQLite with ``mode=ro``, so the
       connection **physically cannot write**, whatever text reaches it.

They are deliberately independent: barrier 2 does not parse SQL and barrier 1
does not touch the database, so a defect in either is unlikely to defeat the
other. Barrier 1 exists to fail fast with a clear, explainable message and to
catch obviously-wrong model output; barrier 2 is what makes damage impossible.

An honest limitation
--------------------
Validating SQL by inspecting text is inherently approximate - a full SQL
parser is the only rigorous approach, and even then permissiveness is a
judgement call. This module is written to be conservative (reject anything not
clearly a read-only SELECT) rather than clever, and it is explicitly *not*
relied upon as the sole protection. That is why the read-only connection
exists. Claiming text validation alone makes arbitrary SQL safe would be
overstating it.

Analysis is performed on a *sanitised* copy of the statement, with comments
removed and string/identifier literals emptied. Both steps matter:

    - Stripping comments means ``SELECT 1 --; DROP TABLE tickets`` is analysed
      as ``SELECT 1``, which is exactly how SQLite will execute it. The comment
      cannot hide a second statement, because statement counting happens after
      stripping.
    - Emptying string literals prevents false positives: a perfectly legitimate
      ``WHERE issue_summary LIKE '%delete%'`` must not be mistaken for a
      ``DELETE``.

The original text - never the sanitised copy - is what gets executed. The
sanitised form exists only to be analysed.
"""

from __future__ import annotations

import re
from typing import Final

# Statements that may begin a valid read-only query. A CTE (``WITH ... AS``)
# is permitted because it is a normal way to express a readable analytical
# query, and its terminal statement is still constrained by the keyword scan
# below (``WITH x AS (...) DELETE ...`` is rejected on the DELETE).
_ALLOWED_LEADING_KEYWORDS: Final[frozenset[str]] = frozenset({"SELECT", "WITH"})

# Anything capable of mutating data, altering schema, touching other database
# files, or changing engine behaviour. Matched as whole words only, so an
# identifier such as ``delete_candidates`` is unaffected (``_`` is a word
# character, so the boundary never falls inside it).
_FORBIDDEN_KEYWORDS: Final[tuple[str, ...]] = (
    # Data modification
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "UPSERT",
    "MERGE",
    # Schema modification
    "CREATE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "RENAME",
    # Database and engine control
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
    # Transaction control - a rollback or open transaction has no legitimate
    # place in a single analytical read.
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
)

# load_extension() is a *function*, not a statement keyword, and would let
# SQLite load arbitrary native code. Matched separately for that reason.
_FORBIDDEN_FUNCTIONS: Final[tuple[str, ...]] = ("LOAD_EXTENSION",)

_FORBIDDEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS + _FORBIDDEN_FUNCTIONS) + r")\b",
    re.IGNORECASE,
)

_LEADING_WORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s*([A-Za-z_]+)")

_LIMIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bLIMIT\b", re.IGNORECASE)

# Expressions that resolve to the real clock. Harmless in most systems, but
# ruinous here: the dataset ends on 2024-03-30, so any query anchored to the
# present matches nothing at all.
#
# This is the most dangerous class of mistake the model can make, because it
# does not fail. "No tickets this week" is a perfectly plausible answer, and
# indistinguishable from a true one unless you already know the data is
# historical. Every other guard here prevents damage; this one prevents a
# confident, silent lie.
# Matched against the *original* statement, not the sanitised copy. The
# sanitiser empties string literals, so by the time it has run, 'now' has
# become '' and is invisible - one guard's protection blinding another.
#
# Matching 'now' only where SQLite would interpret it, as the argument to a
# date function, keeps legitimate text searches such as LIKE '%now%' working.
_WALL_CLOCK_PATTERN: Final[re.Pattern[str]] = re.compile(
    # [^)]* rather than \s* because 'now' is not always the first argument:
    # strftime('%Y', 'now') puts it second.
    r"\b(?:date|time|datetime|julianday|strftime|unixepoch)\s*\([^)]*'now'"
    r"|\bCURRENT_DATE\b|\bCURRENT_TIME\b|\bCURRENT_TIMESTAMP\b",
    re.IGNORECASE,
)


class SqlGuardError(ValueError):
    """Raised when generated SQL is not a safe, single read-only statement.

    Carries a message written to be shown to the model on a repair retry, and
    to the user if that retry also fails - so it must explain *what* was wrong
    specifically enough to be actionable, never just "invalid SQL".
    """


def _sanitise(sql: str) -> str:
    """Return a copy of ``sql`` with comments removed and literals emptied.

    Walks the statement character by character, because the alternative -
    regex substitution - cannot tell a comment marker inside a string literal
    from a real one. ``WHERE note = '-- not a comment'`` must survive intact.

    Comments become a single space (preserving token separation), string
    literals become ``''`` and quoted identifiers become ``"x"``. The result
    is never executed; it exists purely so the checks below reason about
    structure rather than content.

    Args:
        sql: The raw statement.

    Returns:
        A structurally equivalent statement with comment and literal content
        removed.
    """
    out: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < length else ""

        # -- line comment, runs to end of line
        if char == "-" and next_char == "-":
            while index < length and sql[index] != "\n":
                index += 1
            out.append(" ")
            continue

        # /* block comment */ - unterminated comments run to end of input,
        # matching SQLite's own behaviour.
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < length and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index = min(index + 2, length)
            out.append(" ")
            continue

        # 'string literal', where '' is an escaped quote
        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append("''")
            continue

        # "quoted identifier", where "" is an escaped quote
        if char == '"':
            index += 1
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append('"x"')
            continue

        # [bracketed] and `backticked` identifiers, both accepted by SQLite
        if char in "[`":
            closing = "]" if char == "[" else "`"
            index += 1
            while index < length and sql[index] != closing:
                index += 1
            index += 1
            out.append('"x"')
            continue

        out.append(char)
        index += 1

    return "".join(out)


def _split_statements(sanitised: str) -> list[str]:
    """Split sanitised SQL into non-empty statements.

    Safe to split naively on ``;`` precisely because :func:`_sanitise` has
    already removed every semicolon that lived inside a comment or a string
    literal - the only places a semicolon can appear without terminating a
    statement.

    Args:
        sanitised: Output of :func:`_sanitise`.

    Returns:
        Each non-empty statement, stripped. A trailing semicolon therefore
        yields one statement, not two.
    """
    return [part.strip() for part in sanitised.split(";") if part.strip()]


def validate_select(sql: str, *, max_rows: int | None = None) -> str:
    """Validate that ``sql`` is a single read-only query, and return it.

    Args:
        sql: The statement to validate, typically produced by a language model.
        max_rows: When given, a ``LIMIT`` clause is appended if the statement
            does not already contain one. This bounds both the response size
            and the number of rows later fed to the narration call, which
            matters on a token-per-minute budget. It is a convenience, not a
            guarantee - the executor caps rows when fetching as well.

    Returns:
        The original statement, stripped of surrounding whitespace and any
        trailing semicolon, with a ``LIMIT`` appended when requested. The
        *original* text is returned deliberately: the sanitised copy is for
        analysis only and would corrupt string literals if executed.

    Raises:
        SqlGuardError: If the statement is empty, contains more than one
            statement, does not begin with ``SELECT`` or ``WITH``, or uses a
            forbidden keyword or function.
    """
    if not sql or not sql.strip():
        raise SqlGuardError("No SQL statement was provided.")

    sanitised = _sanitise(sql)
    statements = _split_statements(sanitised)

    if not statements:
        raise SqlGuardError(
            "The statement contains no executable SQL - only comments or blanks."
        )

    if len(statements) > 1:
        # The classic injection shape: a benign query followed by a second,
        # destructive one. Rejected before execution, though sqlite3's own
        # driver would also refuse multiple statements in a single execute().
        raise SqlGuardError(
            f"Only one statement is allowed, but {len(statements)} were found. "
            "Combine the logic into a single SELECT."
        )

    statement = statements[0]

    leading_match = _LEADING_WORD_PATTERN.match(statement)
    if not leading_match:
        raise SqlGuardError("The statement does not begin with a SQL keyword.")

    leading_keyword = leading_match.group(1).upper()
    if leading_keyword not in _ALLOWED_LEADING_KEYWORDS:
        raise SqlGuardError(
            f"Only read-only queries are allowed, so the statement must begin "
            f"with SELECT or WITH, but it begins with {leading_keyword}."
        )

    forbidden_match = _FORBIDDEN_PATTERN.search(statement)
    if forbidden_match:
        raise SqlGuardError(
            f"The statement uses {forbidden_match.group(1).upper()}, which is not "
            "permitted. Only read-only SELECT queries can be executed."
        )

    # Deliberately checked against the raw SQL rather than `statement`: see
    # the pattern's own note on why the sanitised copy cannot see this.
    wall_clock_match = _WALL_CLOCK_PATTERN.search(sql)
    if wall_clock_match:
        # Rejected rather than silently allowed, and phrased so the repair
        # retry can act on it: the message names the offending expression and
        # states the substitution to make.
        raise SqlGuardError(
            f"The statement uses {wall_clock_match.group(0)}, which resolves to "
            "today's date. This dataset is a fixed historical snapshot, so a "
            "query anchored to the present matches nothing. Use the reference "
            "timestamp given in the instructions instead."
        )

    # Return the ORIGINAL text, not the sanitised copy - the latter has had its
    # string literals emptied and would silently change query meaning.
    safe_sql = sql.strip().rstrip(";").strip()

    if max_rows is not None and not _LIMIT_PATTERN.search(sanitised):
        safe_sql = f"{safe_sql} LIMIT {max_rows}"

    return safe_sql
