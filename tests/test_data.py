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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from app.config import settings
from app.data import (
    Database,
    DataIntegrityError,
    build_database,
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
