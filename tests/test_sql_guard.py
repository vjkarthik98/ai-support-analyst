"""Tests for :mod:`app.sql_guard`, the validator for model-generated SQL.

This is the most heavily tested module in the project, and deliberately so.
It is the component that decides whether untrusted, model-authored SQL reaches
the database, its inputs are pure strings with no I/O, and its failure modes
run in both directions - each of which deserves explicit coverage:

    - A **false negative** lets a destructive statement through. The read-only
      connection would still block the write, but the guard would have failed
      at its job.
    - A **false positive** rejects a legitimate analytical query. This is the
      more likely failure in practice: a naive ``"DELETE" in sql`` check breaks
      the entirely reasonable ``WHERE issue_summary LIKE '%delete%'``.

Tests are parametrised rather than written one function per case, so adding a
newly-imagined attack is a one-line change and every case is reported by name
when it fails.
"""

from __future__ import annotations

import pytest

from app.sql_guard import SqlGuardError, validate_select

# --------------------------------------------------------------------------
# Statements that MUST be accepted.
#
# Several of these exist specifically to catch over-eager rejection. The
# string-literal cases are the ones a simple substring check would fail.
# --------------------------------------------------------------------------
ALLOWED_STATEMENTS: list[tuple[str, str]] = [
    ("plain count", "SELECT COUNT(*) FROM tickets WHERE status = 'Open'"),
    ("select star", "SELECT * FROM tickets"),
    ("lowercase keywords", "select avg(customer_rating) from tickets"),
    ("mixed case keywords", "SeLeCt COUNT(*) FrOm tickets"),
    ("trailing semicolon", "SELECT * FROM tickets;"),
    ("leading whitespace", "   \n  SELECT 1  "),
    (
        "common table expression",
        "WITH resolved AS (SELECT * FROM tickets WHERE status = 'Resolved') "
        "SELECT COUNT(*) FROM resolved",
    ),
    (
        "aggregate with group by",
        "SELECT agent_id, AVG(customer_rating) FROM tickets GROUP BY agent_id",
    ),
    (
        "subquery",
        "SELECT * FROM tickets WHERE resolution_time_hrs > "
        "(SELECT AVG(resolution_time_hrs) FROM tickets)",
    ),
    # The false-positive guards. A forbidden word appears, but only ever as
    # data or as part of a longer identifier - never as an executable keyword.
    (
        "forbidden word inside a string literal",
        "SELECT * FROM tickets WHERE issue_summary LIKE '%delete%'",
    ),
    (
        "forbidden word inside an identifier",
        "SELECT * FROM tickets AS delete_candidates",
    ),
    (
        "semicolon inside a string literal",
        "SELECT * FROM tickets WHERE issue_summary = 'first;second'",
    ),
    (
        "comment marker inside a string literal",
        "SELECT * FROM tickets WHERE issue_summary = '-- not a comment'",
    ),
    # A trailing comment cannot execute, so stripping it and accepting the
    # query is correct. Rejecting this would be a false positive.
    ("inert trailing line comment", "SELECT 1 --; DROP TABLE tickets"),
    ("semicolon then comment", "SELECT 1; -- DROP TABLE tickets"),
    ("inert block comment", "SELECT /* a note */ COUNT(*) FROM tickets"),
]

# --------------------------------------------------------------------------
# Statements that MUST be rejected.
# --------------------------------------------------------------------------
REJECTED_STATEMENTS: list[tuple[str, str]] = [
    # Stacked statements - the classic injection shape.
    ("stacked statements", "SELECT 1; DROP TABLE tickets"),
    ("stacked hidden by block comment", "SELECT 1 /* x */; DROP TABLE tickets"),
    ("stacked with trailing semicolon", "SELECT 1; DELETE FROM tickets;"),
    # Direct data modification.
    ("delete", "DELETE FROM tickets"),
    ("insert", "INSERT INTO tickets (ticket_id) VALUES ('TKT-999')"),
    ("update", "UPDATE tickets SET status = 'Resolved'"),
    ("replace", "REPLACE INTO tickets (ticket_id) VALUES ('x')"),
    # Schema modification.
    ("drop table", "DROP TABLE tickets"),
    ("create table", "CREATE TABLE evil (id INTEGER)"),
    ("alter table", "ALTER TABLE tickets ADD COLUMN evil TEXT"),
    # Engine and database control.
    ("pragma", "PRAGMA table_info(tickets)"),
    ("attach another database", "ATTACH DATABASE 'evil.db' AS evil"),
    ("vacuum", "VACUUM"),
    # A valid-looking CTE whose terminal statement is destructive. This is why
    # the keyword scan covers the whole statement, not just its first word.
    ("cte leading to delete", "WITH t AS (SELECT 1) DELETE FROM tickets"),
    # load_extension is a function, not a statement keyword, so a keyword-only
    # scan would miss it - and it can load arbitrary native code.
    ("load_extension", "SELECT load_extension('evil.so')"),
    # Nothing executable.
    ("empty string", ""),
    ("whitespace only", "   \n\t  "),
    ("comment only", "-- just a comment"),
    ("block comment only", "/* nothing here */"),
]


@pytest.mark.parametrize(
    "sql",
    [pytest.param(sql, id=name) for name, sql in ALLOWED_STATEMENTS],
)
def test_accepts_read_only_queries(sql: str) -> None:
    """Legitimate read-only queries pass validation unchanged.

    Args:
        sql: A statement that must be accepted.
    """
    assert validate_select(sql)


@pytest.mark.parametrize(
    "sql",
    [pytest.param(sql, id=name) for name, sql in REJECTED_STATEMENTS],
)
def test_rejects_unsafe_statements(sql: str) -> None:
    """Anything that is not a single read-only query is refused.

    Args:
        sql: A statement that must be rejected.
    """
    with pytest.raises(SqlGuardError):
        validate_select(sql)


def test_preserves_original_text_not_sanitised_copy() -> None:
    """The returned SQL keeps its string literals intact.

    Validation runs against a sanitised copy whose literals are emptied. If
    that copy were returned for execution, ``LIKE '%refund%'`` would silently
    become ``LIKE ''`` and quietly return the wrong answer - a far worse
    outcome than an error, because nothing would look broken.
    """
    sql = "SELECT * FROM tickets WHERE issue_summary LIKE '%refund%'"

    assert "'%refund%'" in validate_select(sql)


def test_strips_trailing_semicolon() -> None:
    """A trailing semicolon is removed so the result composes safely."""
    assert validate_select("SELECT 1;") == "SELECT 1"


def test_appends_limit_when_absent() -> None:
    """A row cap is appended when the query does not set its own.

    Bounds both the API response size and the number of rows later sent to the
    narration call, which is what keeps a query inside the free tier's
    tokens-per-minute budget.
    """
    assert validate_select("SELECT * FROM tickets", max_rows=500) == (
        "SELECT * FROM tickets LIMIT 500"
    )


def test_respects_an_existing_limit() -> None:
    """An explicit LIMIT is left alone rather than duplicated.

    Appending a second LIMIT would be a syntax error, so the guard must detect
    the existing one.
    """
    sql = "SELECT * FROM tickets LIMIT 5"

    assert validate_select(sql, max_rows=500) == sql


def test_appends_limit_after_stripping_semicolon() -> None:
    """LIMIT is appended after the semicolon is removed, not before.

    The opposite order would produce ``SELECT * FROM tickets; LIMIT 500``,
    which is invalid SQL.
    """
    assert validate_select("SELECT * FROM tickets;", max_rows=500) == (
        "SELECT * FROM tickets LIMIT 500"
    )


def test_no_limit_appended_when_max_rows_is_none() -> None:
    """Omitting ``max_rows`` leaves the statement untouched."""
    assert validate_select("SELECT * FROM tickets") == "SELECT * FROM tickets"


def test_error_message_names_the_offending_keyword() -> None:
    """Rejection messages identify what was wrong, not merely that it was.

    The message is fed back to the model on a repair retry, and shown to the
    user if that retry also fails. "Invalid SQL" would be useless to both.
    """
    with pytest.raises(SqlGuardError, match="DROP"):
        validate_select("DROP TABLE tickets")


def test_error_message_reports_statement_count() -> None:
    """A stacked statement is reported as such, not as a generic failure."""
    with pytest.raises(SqlGuardError, match="[Oo]nly one statement"):
        validate_select("SELECT 1; DROP TABLE tickets")


# ---------------------------------------------------------------------------
# Wall-clock detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT COUNT(*) FROM tickets WHERE created_at >= date('now', '-7 days')",
            id="date now",
        ),
        pytest.param(
            "SELECT * FROM tickets WHERE created_at >= datetime('now')",
            id="datetime now",
        ),
        pytest.param("SELECT strftime('%Y', 'now')", id="now as second argument"),
        pytest.param(
            "SELECT julianday('now') - julianday(created_at) FROM tickets",
            id="julianday now",
        ),
        pytest.param(
            "SELECT * FROM tickets WHERE created_at >= CURRENT_DATE", id="CURRENT_DATE"
        ),
        pytest.param(
            "SELECT * FROM tickets WHERE created_at > CURRENT_TIMESTAMP",
            id="CURRENT_TIMESTAMP",
        ),
    ],
)
def test_wall_clock_queries_are_rejected(sql: str) -> None:
    """A query anchored to the real clock is refused.

    This is the most dangerous mistake the model can make here, because it does
    not fail. The data ends on 2024-03-30, so a query against the present
    matches nothing - and "no tickets this week" is a perfectly plausible
    answer, indistinguishable from a true one unless you already know the data
    is historical. Every other rule in this module prevents damage; this one
    prevents a confident, silent falsehood.

    Args:
        sql: A statement resolving against the real clock.
    """
    with pytest.raises(SqlGuardError, match="today's date"):
        validate_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT COUNT(*) FROM tickets "
            "WHERE created_at >= datetime('2024-03-30 18:06:00', '-7 days')",
            id="anchored datetime",
        ),
        pytest.param(
            "SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) "
            "FROM tickets GROUP BY month",
            id="strftime on a column",
        ),
        pytest.param(
            "SELECT * FROM tickets WHERE issue_summary LIKE '%now%'",
            id="the word now inside a string literal",
        ),
        pytest.param(
            "SELECT julianday('2024-03-30 18:06:00') - julianday(created_at) "
            "FROM tickets",
            id="julianday on the anchor",
        ),
    ],
)
def test_anchored_and_literal_queries_are_allowed(sql: str) -> None:
    """Legitimate date arithmetic and text searches are unaffected.

    The counterpart to the rejections above. A guard that also blocked
    ``LIKE '%now%'`` or date functions applied to a column would break ordinary
    questions, so the pattern matches ``'now'`` only where SQLite would read it
    as the current time.

    Args:
        sql: A statement that must be accepted.
    """
    assert validate_select(sql)
