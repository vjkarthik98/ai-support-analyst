"""Tests for :mod:`app.data`, the CSV-to-SQLite ingestion layer.

Organised around the two guarantees the module exists to provide, plus the
correctness gates the assessment itself depends on:

    1. **Missing values stay missing.** An unresolved ticket has no resolution
       time and no rating. If either became ``0.0`` or ``NaN``, every average
       computed over the column would be wrong - and wrong *quietly*, which is
       the dangerous kind. Several tests here exist solely to pin that down.
    2. **The database cannot be written to.** Enforced by SQLite itself through
       a read-only connection, independently of :mod:`app.sql_guard`.

The gate tests assert against the real shipped dataset rather than a fixture,
because "500 rows, 173 unresolved, anchored at 2024-03-30 18:06" are claims
about *that file*. Asserting them against synthetic data would prove nothing.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from app.config import settings
from app.data import (
    Database,
    DataIntegrityError,
    QueryTimeoutError,
    build_database,
    execute_select,
    get_connection,
    read_only_connection,
)

# ---------------------------------------------------------------------------
# Correctness gates - the numbers the rest of the assessment is measured against
# ---------------------------------------------------------------------------


def test_loads_every_row_from_the_shipped_dataset(real_database: Database) -> None:
    """All 500 tickets are ingested.

    Args:
        real_database: Database built from the shipped CSV.
    """
    assert real_database.row_count == 500


def test_as_of_anchors_to_the_latest_ticket(real_database: Database) -> None:
    """With no configured AS_OF, the anchor is the dataset's own last timestamp.

    This is the decision that makes relative-time questions work at all. The
    data ends in March 2024, so resolving "this week" against a real clock
    would match nothing and the system would look broken while behaving
    exactly as written.

    Args:
        real_database: Database built from the shipped CSV.
    """
    assert real_database.as_of == datetime(2024, 3, 30, 18, 6)


def test_database_records_the_file_it_was_built_from(
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """The source file's name is taken from the CSV actually read.

    Recorded at ingestion rather than read back from configuration, so the
    name reported can never disagree with the data that was loaded.

    Args:
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing a valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(valid_row())

    database = build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")

    assert database.source_file == csv_path.name


def test_unresolved_ticket_count(real_database: Database) -> None:
    """Open and Escalated tickets together total 173.

    Worth pinning because the brief's own schema preview contradicts the
    shipped data: it shows an Escalated ticket carrying a resolution time and
    rating, whereas every Escalated row in the real file has neither. The code
    follows the data, and this test records that.

    Args:
        real_database: Database built from the shipped CSV.
    """
    with read_only_connection(real_database.path) as connection:
        unresolved = connection.execute(
            "SELECT COUNT(*) FROM tickets WHERE status IN ('Open', 'Escalated')"
        ).fetchone()[0]

    assert unresolved == 173


def test_average_rating_for_technical_tickets(real_database: Database) -> None:
    """A known aggregate is reproduced exactly.

    Computed independently from the CSV. Any silent corruption of nulls would
    move this number, so it doubles as a canary for the NULL guarantee.

    Args:
        real_database: Database built from the shipped CSV.
    """
    with read_only_connection(real_database.path) as connection:
        average = connection.execute(
            "SELECT AVG(customer_rating) FROM tickets WHERE category = 'Technical'"
        ).fetchone()[0]

    assert round(average, 2) == 3.74


# ---------------------------------------------------------------------------
# The NULL guarantee
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        pytest.param("resolution_time_hrs", id="resolution time"),
        pytest.param("customer_rating", id="customer rating"),
    ],
)
def test_missing_values_are_stored_as_sql_null(
    column: str, real_database: Database
) -> None:
    """Absent measurements become real SQL NULL, not zero.

    Args:
        column: The nullable column under test.
        real_database: Database built from the shipped CSV.
    """
    with read_only_connection(real_database.path) as connection:
        nulls = connection.execute(
            f"SELECT COUNT(*) FROM tickets WHERE {column} IS NULL"
        ).fetchone()[0]

    assert nulls == 173


def test_nulls_are_typed_null_not_zero_or_nan(real_database: Database) -> None:
    """SQLite reports the stored type as ``null``.

    ``IS NULL`` alone would not catch a stored NaN, which is a REAL and would
    make aggregates behave unpredictably while still looking "missing". Asking
    SQLite for the storage class closes that gap.

    Args:
        real_database: Database built from the shipped CSV.
    """
    with read_only_connection(real_database.path) as connection:
        stored_types = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT typeof(resolution_time_hrs) FROM tickets "
                "WHERE status = 'Open'"
            )
        }

    assert stored_types == {"null"}


def test_null_resolution_times_are_excluded_from_averages(
    real_database: Database,
) -> None:
    """An average over the column ignores unresolved tickets entirely.

    The concrete harm the NULL guarantee prevents: were the 173 missing values
    stored as ``0.0``, they would be counted as instant resolutions and drag
    the mean far below the truth.

    Args:
        real_database: Database built from the shipped CSV.
    """
    with read_only_connection(real_database.path) as connection:
        average, counted = connection.execute(
            "SELECT AVG(resolution_time_hrs), COUNT(resolution_time_hrs) FROM tickets"
        ).fetchone()

    assert counted == 327  # 500 total minus the 173 unresolved
    assert average > 15  # ~19.16; would collapse toward 12.5 if nulls became 0.0


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("DELETE FROM tickets", id="delete"),
        pytest.param("UPDATE tickets SET status = 'Resolved'", id="update"),
        pytest.param("DROP TABLE tickets", id="drop"),
        pytest.param(
            "INSERT INTO tickets (ticket_id) VALUES ('TKT-999')", id="insert"
        ),
    ],
)
def test_write_attempts_fail_on_a_read_only_connection(
    statement: str, real_database: Database
) -> None:
    """SQLite refuses every write, whatever statement reaches it.

    This barrier is deliberately independent of :mod:`app.sql_guard`: the guard
    inspects SQL text and never touches the database, while this check never
    parses SQL. A defect in one is unlikely to defeat the other.

    Args:
        statement: A write that must be refused.
        real_database: Database built from the shipped CSV.
    """
    with read_only_connection(real_database.path) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(statement)


def test_read_only_connection_closes_on_exception(real_database: Database) -> None:
    """The context manager closes the connection even when the body raises.

    A leaked handle is not merely untidy on Windows: it prevents the next
    rebuild from deleting the database file.

    Args:
        real_database: Database built from the shipped CSV.
    """
    leaked: sqlite3.Connection | None = None

    with pytest.raises(RuntimeError):
        with read_only_connection(real_database.path) as connection:
            leaked = connection
            raise RuntimeError("simulated failure inside the with-block")

    assert leaked is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        leaked.execute("SELECT 1")


def test_connection_refuses_a_missing_database(tmp_path: Path) -> None:
    """Opening a database that was never built fails rather than creating one.

    ``mode=ro`` will not create a missing file, unlike an ordinary connection.
    A silently-created empty database would produce zero-row answers that look
    like legitimate results.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    with pytest.raises(sqlite3.OperationalError):
        get_connection(tmp_path / "never_built.db")


def test_database_path_containing_a_space(
    path_with_space: Path, project_csv: Path
) -> None:
    """A path with a space in it opens correctly.

    Regression cover for a defect this repository would genuinely hit: it lives
    under ``AI Assessment``. A hand-built ``file:`` URI breaks on the space;
    only :meth:`pathlib.Path.as_uri` percent-encodes it properly.

    Args:
        path_with_space: A directory whose name contains a space.
        project_csv: Path to the shipped dataset.
    """
    database = build_database(
        csv_path=project_csv, db_path=path_with_space / "tickets.db"
    )

    with read_only_connection(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 500


# ---------------------------------------------------------------------------
# Rebuild behaviour
# ---------------------------------------------------------------------------


def test_rebuild_replaces_an_existing_database(
    project_csv: Path, tmp_path: Path
) -> None:
    """Building twice over the same path succeeds and does not duplicate rows.

    The database is a build artifact derived from the CSV, so every startup
    rebuilds it. Appending instead of replacing would double the row count.

    Args:
        project_csv: Path to the shipped dataset.
        tmp_path: pytest's per-test temporary directory.
    """
    db_path = tmp_path / "tickets.db"
    build_database(csv_path=project_csv, db_path=db_path)
    rebuilt = build_database(csv_path=project_csv, db_path=db_path)

    assert rebuilt.row_count == 500
    with read_only_connection(rebuilt.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 500


def test_configured_as_of_overrides_the_dataset_anchor(
    project_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly configured AS_OF wins over the auto-anchor.

    Lets an operator reproduce the system's behaviour at a chosen point in
    time, which is also how the anomaly detectors are tested deterministically.

    Args:
        project_csv: Path to the shipped dataset.
        tmp_path: pytest's per-test temporary directory.
        monkeypatch: pytest's attribute patcher.
    """
    pinned = datetime(2024, 2, 1, 12, 0)
    monkeypatch.setattr(settings, "as_of", pinned)

    database = build_database(csv_path=project_csv, db_path=tmp_path / "tickets.db")

    assert database.as_of == pinned


def test_tickets_after_a_pinned_as_of_are_excluded(
    project_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned AS_OF removes tickets that did not yet exist at that moment.

    They previously stayed in the table: counted by SQL, judged by the anomaly
    detectors, and given a negative age by the SLA rule.

    Args:
        project_csv: Path to the shipped dataset.
        tmp_path: pytest's per-test temporary directory.
        monkeypatch: pytest's attribute patcher.
    """
    pinned = datetime(2024, 2, 1, 12, 0)
    monkeypatch.setattr(settings, "as_of", pinned)

    database = build_database(csv_path=project_csv, db_path=tmp_path / "tickets.db")

    with read_only_connection(database.path) as connection:
        latest, count = connection.execute(
            "SELECT MAX(created_at), COUNT(*) FROM tickets"
        ).fetchone()
    assert latest <= "2024-02-01 12:00:00"
    assert 0 < database.row_count < 500
    # The reported row count describes the table actually built.
    assert count == database.row_count


def test_as_of_before_every_ticket_is_rejected(
    project_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An AS_OF preceding the whole dataset fails loudly, not as an empty table.

    An empty table would answer every question with a confident zero.

    Args:
        project_csv: Path to the shipped dataset.
        tmp_path: pytest's per-test temporary directory.
        monkeypatch: pytest's attribute patcher.
    """
    monkeypatch.setattr(settings, "as_of", datetime(2020, 1, 1))

    with pytest.raises(DataIntegrityError, match="earlier than every ticket"):
        build_database(csv_path=project_csv, db_path=tmp_path / "tickets.db")


# ---------------------------------------------------------------------------
# Validation of malformed input
# ---------------------------------------------------------------------------


def test_missing_csv_file_is_reported(tmp_path: Path) -> None:
    """A non-existent CSV raises ``FileNotFoundError``.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    with pytest.raises(FileNotFoundError):
        build_database(
            csv_path=tmp_path / "absent.csv", db_path=tmp_path / "tickets.db"
        )


def test_missing_columns_are_named(
    write_csv: Callable[..., Path], tmp_path: Path
) -> None:
    """A truncated header reports exactly which columns are absent.

    Checked once before any row is read, so the message names the file and the
    columns rather than surfacing as a bare ``KeyError`` on the first row.

    Args:
        write_csv: Factory writing a temporary CSV.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv("TKT-001,2024-01-15 09:30", header="ticket_id,created_at")

    with pytest.raises(DataIntegrityError, match="missing required column"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


def test_empty_file_is_rejected(
    write_csv: Callable[..., Path], tmp_path: Path
) -> None:
    """A file with no header at all is rejected.

    Args:
        write_csv: Factory writing a temporary CSV.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(header="")

    with pytest.raises(DataIntegrityError, match="header"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


def test_header_without_data_rows_is_rejected(
    write_csv: Callable[..., Path], tmp_path: Path
) -> None:
    """A header with no tickets beneath it is rejected.

    An empty table would otherwise answer every question with zero, which reads
    as a valid result rather than a loading failure.

    Args:
        write_csv: Factory writing a temporary CSV.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv()

    with pytest.raises(DataIntegrityError, match="no data rows|header"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        pytest.param({"category": "Nonsense"}, "category", id="unknown category"),
        pytest.param({"priority": "Urgent"}, "priority", id="unknown priority"),
        pytest.param({"status": "Pending"}, "status", id="unknown status"),
        pytest.param(
            {"created_at": "15/01/2024"}, "created_at", id="wrong date format"
        ),
        pytest.param(
            {"response_time_hrs": "quick"}, "not numeric", id="non-numeric response time"
        ),
        pytest.param(
            {"resolution_time_hrs": "ages"}, "not numeric", id="non-numeric resolution"
        ),
        pytest.param(
            {"customer_rating": "great"}, "whole number", id="non-numeric rating"
        ),
        pytest.param(
            {"response_time_hrs": ""}, "required but blank", id="blank required field"
        ),
        pytest.param({"agent_id": ""}, "required but blank", id="blank agent id"),
    ],
)
def test_invalid_field_values_are_rejected_with_context(
    overrides: dict[str, str],
    expected_message: str,
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """Each malformed field is rejected, naming the ticket and the problem.

    Failing at ingestion is deliberate. A category the anomaly detectors have
    never seen should stop the process at startup, not silently skew a result
    discovered during a live walkthrough.

    Args:
        overrides: The single field to corrupt.
        expected_message: Text the error must contain.
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing an otherwise-valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(valid_row(**overrides))

    with pytest.raises(DataIntegrityError, match=expected_message):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


def test_error_messages_identify_the_offending_ticket(
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """A validation failure names the specific ticket that caused it.

    With 500 rows, "invalid category" without an identifier would mean reading
    the file by hand to find the offender.

    Args:
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing an otherwise-valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(
        valid_row(ticket_id="TKT-001"),
        valid_row(ticket_id="TKT-042", priority="Urgent"),
    )

    with pytest.raises(DataIntegrityError, match="TKT-042"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


def test_short_row_is_rejected(
    write_csv: Callable[..., Path], tmp_path: Path
) -> None:
    """A row with too few fields fails with a readable message.

    ``csv.DictReader`` yields ``None`` for columns a short row never reaches,
    so reading them directly would raise ``AttributeError`` naming neither the
    row nor the column.

    Args:
        write_csv: Factory writing a temporary CSV.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv("TKT-001,2024-01-15 09:30,Billing,High,Open,1.5")

    with pytest.raises(DataIntegrityError, match="required but blank"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


def test_optional_fields_may_be_blank(
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """Blank resolution time and rating are accepted and stored as NULL.

    The positive counterpart to the validation tests above: an unresolved
    ticket is legitimate data, not a malformed row.

    Args:
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing an otherwise-valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(
        valid_row(status="Open", resolution_time_hrs="", customer_rating="")
    )
    database = build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")

    with read_only_connection(database.path) as connection:
        row = connection.execute(
            "SELECT resolution_time_hrs, customer_rating FROM tickets"
        ).fetchone()

    assert row["resolution_time_hrs"] is None
    assert row["customer_rating"] is None


# ---------------------------------------------------------------------------
# Runaway queries
# ---------------------------------------------------------------------------


def test_runaway_query_is_aborted(real_database: Database) -> None:
    """A query that would never finish is stopped rather than hanging.

    A recursive CTE is a legitimate read-only SELECT, so :mod:`app.sql_guard`
    allows it - correctly, since nothing about the text is unsafe. Only an
    execution limit can defend against it, and without one this statement
    blocks its thread permanently: verified still running after twelve seconds
    before the timeout existed.

    The symptom of a regression here is a frozen service rather than a failing
    assertion, which is exactly why it is pinned.

    Args:
        real_database: Database built from the shipped dataset.
    """
    runaway = (
        "WITH RECURSIVE bomb(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM bomb) "
        "SELECT COUNT(*) FROM bomb"
    )
    started = time.monotonic()

    with read_only_connection(real_database.path, timeout_seconds=0.5) as connection:
        with pytest.raises(QueryTimeoutError):
            execute_select(connection, runaway, max_rows=500)

    # Generous upper bound: the assertion is "it stopped", not "it stopped at
    # precisely one second", which would be flaky on a loaded machine.
    assert time.monotonic() - started < 10


def test_connection_still_works_after_a_timeout(real_database: Database) -> None:
    """An aborted query does not poison the connection.

    The service must keep answering afterwards; a timeout that left the
    database unusable would convert one bad question into an outage.

    Args:
        real_database: Database built from the shipped dataset.
    """
    runaway = (
        "WITH RECURSIVE bomb(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM bomb) "
        "SELECT COUNT(*) FROM bomb"
    )

    with read_only_connection(real_database.path, timeout_seconds=0.5) as connection:
        with pytest.raises(QueryTimeoutError):
            execute_select(connection, runaway, max_rows=500)

        rows = execute_select(
            connection, "SELECT COUNT(*) AS n FROM tickets", max_rows=500
        )

    assert rows == [{"n": 500}]


def test_ordinary_queries_are_unaffected(real_database: Database) -> None:
    """A normal query completes well inside the budget.

    Guards against a timeout set so aggressively that legitimate work fails.

    Args:
        real_database: Database built from the shipped dataset.
    """
    with read_only_connection(real_database.path) as connection:
        rows = execute_select(
            connection,
            "SELECT agent_id, COUNT(*) AS n FROM tickets GROUP BY agent_id",
            max_rows=500,
        )

    assert len(rows) == 12


def test_fetch_cap_applies_without_a_limit_clause(real_database: Database) -> None:
    """Row capping holds even when the statement carries no LIMIT.

    Args:
        real_database: Database built from the shipped dataset.
    """
    with read_only_connection(real_database.path) as connection:
        rows = execute_select(connection, "SELECT ticket_id FROM tickets", max_rows=10)

    assert len(rows) == 10


# ---------------------------------------------------------------------------
# Malformed input an evaluator could plausibly produce
# ---------------------------------------------------------------------------


def test_byte_order_mark_is_handled(
    valid_row: Callable[..., str], csv_header: str, tmp_path: Path
) -> None:
    """A CSV saved with a byte-order mark loads normally.

    The likeliest of all the malformed-input cases, because saving this file
    from Excel produces one. The mark would otherwise attach to the first
    header name, and the failure would report "missing required column:
    ticket_id" - pointing at entirely the wrong problem.

    Args:
        valid_row: Factory producing a valid row.
        csv_header: The standard header row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = tmp_path / "bom.csv"
    csv_path.write_text(f"{csv_header}\n{valid_row()}", encoding="utf-8-sig")

    database = build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")

    assert database.row_count == 1


def test_duplicate_ticket_ids_are_named(
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """A repeated ticket id is reported with the id and the line number.

    Caught during parsing rather than left to the primary-key constraint, whose
    message names neither.

    Args:
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing a valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(
        valid_row(ticket_id="TKT-001"),
        valid_row(ticket_id="TKT-002"),
        valid_row(ticket_id="TKT-001"),
    )

    with pytest.raises(DataIntegrityError, match="TKT-001.*more than once"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


@pytest.mark.parametrize(
    "rating",
    [pytest.param("0", id="below range"), pytest.param("9", id="above range")],
)
def test_ratings_outside_one_to_five_are_rejected(
    rating: str,
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """A satisfaction rating outside 1-5 is refused.

    The brief defines this column as an integer from 1 to 5. An out-of-range
    value would pass through every aggregate unnoticed and quietly shift
    averages - worse than a parse failure, because nothing would look wrong.

    Args:
        rating: An invalid rating value.
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing a valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(valid_row(customer_rating=rating))

    with pytest.raises(DataIntegrityError, match="outside the valid range"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


@pytest.mark.parametrize(
    "rating",
    [pytest.param("1", id="lower bound"), pytest.param("5", id="upper bound")],
)
def test_ratings_at_the_boundaries_are_accepted(
    rating: str,
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """The extremes of the valid range are not rejected.

    The companion to the test above: an off-by-one in the bounds check would
    silently discard every one-star and five-star rating, skewing exactly the
    figures an analyst cares about most.

    Args:
        rating: A valid boundary rating.
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing a valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(valid_row(customer_rating=rating))

    database = build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")

    assert database.row_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"response_time_hrs": "-3.0"}, id="negative response time"),
        pytest.param({"resolution_time_hrs": "-10.0"}, id="negative resolution time"),
    ],
)
def test_negative_durations_are_rejected(
    overrides: dict[str, str],
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """A negative elapsed time is refused.

    Not merely unusual but impossible, and accepting one would drag the
    interquartile fence downwards so that genuinely slow tickets stopped being
    flagged as outliers.

    Args:
        overrides: The field to corrupt.
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing a valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(valid_row(**overrides))

    with pytest.raises(DataIntegrityError, match="cannot be negative"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"response_time_hrs": "nan"}, id="NaN response time"),
        pytest.param({"resolution_time_hrs": "NaN"}, id="NaN resolution time"),
        pytest.param({"response_time_hrs": "inf"}, id="infinite response time"),
        pytest.param({"resolution_time_hrs": "-inf"}, id="negative infinity"),
    ],
)
def test_non_finite_durations_are_rejected(
    overrides: dict[str, str],
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """The text "nan" and "inf" is refused, although ``float()`` accepts it.

    Stored, a NaN becomes NULL - breaking the NOT NULL constraint with an
    error that names neither ticket nor value - and an infinity turns every
    average over its column into ``inf``. Both are caught here instead, with
    the ticket named.

    Args:
        overrides: The field to corrupt.
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing a valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(valid_row(**overrides))

    with pytest.raises(DataIntegrityError, match="must be a finite number"):
        build_database(csv_path=csv_path, db_path=tmp_path / "tickets.db")


def test_zero_duration_is_accepted(
    write_csv: Callable[..., Path],
    valid_row: Callable[..., str],
    tmp_path: Path,
) -> None:
    """A zero elapsed time is valid, unlike a negative one.

    Instant is possible; travelling backwards is not.

    Args:
        write_csv: Factory writing a temporary CSV.
        valid_row: Factory producing a valid row.
        tmp_path: pytest's per-test temporary directory.
    """
    csv_path = write_csv(valid_row(response_time_hrs="0.0"))

    assert build_database(
        csv_path=csv_path, db_path=tmp_path / "tickets.db"
    ).row_count == 1


def test_rows_are_accessible_by_column_name(real_database: Database) -> None:
    """Query results support name-based access.

    ``sqlite3.Row`` is configured so results can be serialised to JSON by
    column name, rather than depending on positional order that a changed
    ``SELECT`` would silently break.

    Args:
        real_database: Database built from the shipped CSV.
    """
    with read_only_connection(real_database.path) as connection:
        row = connection.execute(
            "SELECT ticket_id, status FROM tickets LIMIT 1"
        ).fetchone()

    assert row["ticket_id"].startswith("TKT-")
    assert row["status"] in {"Open", "Resolved", "Escalated"}


# ---------------------------------------------------------------------------
# The CORR aggregate
# ---------------------------------------------------------------------------


def test_corr_matches_the_known_correlation(real_database: Database) -> None:
    """CORR reproduces pandas' Pearson value for response time and rating.

    Args:
        real_database: Database built from the shipped dataset.
    """
    with read_only_connection(real_database.path) as connection:
        (value,) = connection.execute(
            "SELECT ROUND(CORR(response_time_hrs, customer_rating), 3) FROM tickets"
        ).fetchone()

    assert value == -0.078


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([(1, 2), (2, 4), (3, 6)], 1.0),
        ([(1, 6), (2, 4), (3, 2)], -1.0),
        # NULLs are skipped, as by every built-in aggregate.
        ([(1, 2), (None, 9), (2, 4), (3, None), (3, 6)], 1.0),
        # Undefined, not zero: one pair, or a column with no spread.
        ([(1, 2)], None),
        ([(1, 5), (2, 5), (3, 5)], None),
    ],
)
def test_corr_edge_cases(
    real_database: Database, pairs: list[tuple[float | None, float | None]], expected: float | None
) -> None:
    """CORR handles perfect, NULL-bearing and undefined inputs.

    Args:
        real_database: Any database; only the connection's function is used.
        pairs: ``(x, y)`` values to correlate.
        expected: The correlation, or ``None`` where it is undefined.
    """
    values = " UNION ALL ".join(
        f"SELECT {'NULL' if x is None else x} AS x, {'NULL' if y is None else y} AS y"
        for x, y in pairs
    )
    with read_only_connection(real_database.path) as connection:
        (value,) = connection.execute(f"SELECT CORR(x, y) FROM ({values})").fetchone()

    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)
