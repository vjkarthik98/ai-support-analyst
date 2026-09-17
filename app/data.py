"""CSV ingestion into SQLite, the AS_OF time anchor, and read-only access.

This module owns exactly one concern: turning ``support_tickets.csv`` into a
trustworthy, queryable SQLite table. "Trustworthy" specifically means two
things that are easy to get wrong and expensive to get wrong silently:

    1. Missing values (unresolved tickets have no resolution time or rating)
       must become SQL ``NULL``, never ``0.0`` or ``NaN``. A ``0.0`` resolution
       time for an open ticket would corrupt every average computed over it;
       a stored ``NaN`` would make ``IS NULL`` checks fail unpredictably.
    2. ``created_at`` must be stored in a form SQLite can sort and compare as
       a string, so that plain ``WHERE created_at > ...`` clauses behave
       correctly without every generated query needing a date function.

The standard library's ``csv`` module is used for ingestion rather than
pandas, even though pandas is a project dependency (used later in
:mod:`app.anomalies` for its vectorised statistics). ``csv.DictReader`` hands
back every field as a plain string with no implicit type inference, which
means the coercion functions below have complete, auditable control over
exactly how each value becomes a Python type - precisely the guarantee that
requirement (1) above depends on. Pandas' automatic dtype inference is a
common source of the NaN-vs-NULL bug this module is designed to avoid.

Schema *facts* (column names, enum values, row count, the AS_OF anchor) are
exposed here for other modules to consume. Formatting those facts into LLM
prompt text is deliberately left to :mod:`app.prompts` - this module answers
"what is true about the data", not "how do we phrase that for a model".
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from app.config import settings

logger = logging.getLogger(__name__)

TABLE_NAME: Final[str] = "tickets"

# The permitted values for each categorical column, per the brief's own
# schema description (section 3.3). Exported so app.sql_guard, app.anomalies
# and app.prompts share one definition instead of three copies that could
# drift apart.
CATEGORIES: Final[frozenset[str]] = frozenset({"Billing", "Technical", "General"})
PRIORITIES: Final[frozenset[str]] = frozenset({"Low", "Medium", "High", "Critical"})
STATUSES: Final[frozenset[str]] = frozenset({"Open", "Resolved", "Escalated"})

COLUMNS: Final[tuple[str, ...]] = (
    "ticket_id",
    "created_at",
    "category",
    "priority",
    "status",
    "response_time_hrs",
    "resolution_time_hrs",
    "agent_id",
    "customer_rating",
    "issue_summary",
)

# created_at is stored as "YYYY-MM-DD HH:MM:SS" text. SQLite has no native
# datetime type, but ISO 8601 text sorts and compares correctly with plain
# string operators, and works directly with SQLite's date/datetime()
# functions - so generated SQL never needs to know about a special format.
_CREATE_TABLE_SQL: Final[str] = f"""
    CREATE TABLE {TABLE_NAME} (
        ticket_id            TEXT    PRIMARY KEY,
        created_at           TEXT    NOT NULL,
        category             TEXT    NOT NULL,
        priority             TEXT    NOT NULL,
        status               TEXT    NOT NULL,
        response_time_hrs    REAL    NOT NULL,
        resolution_time_hrs  REAL,
        agent_id             TEXT    NOT NULL,
        customer_rating      INTEGER,
        issue_summary        TEXT    NOT NULL
    )
"""
# No indexes are created deliberately: the table holds 500 rows, so a full
# scan costs microseconds. An index would add write-path complexity and a
# maintenance burden for zero measurable read benefit at this data volume.

_INSERT_SQL: Final[str] = f"""
    INSERT INTO {TABLE_NAME} ({", ".join(COLUMNS)})
    VALUES ({", ".join("?" for _ in COLUMNS)})
"""


@dataclass(frozen=True)
class Database:
    """Metadata describing a freshly built SQLite database.

    Immutable and returned by value from :func:`build_database`, rather than
    stored as module-level global state. The caller (the FastAPI startup
    event, in practice) decides how long it lives and how it is shared - this
    module has no opinion on application lifecycle, only on data correctness.

    Attributes:
        path: Filesystem path to the SQLite database file.
        as_of: The reference "now" for relative-time questions - either the
            operator-configured value, or the dataset's own latest
            ``created_at`` when none was configured.
        row_count: Number of ticket rows loaded.
    """

    path: Path
    as_of: datetime
    row_count: int


class DataIntegrityError(ValueError):
    """Raised when a row in the source CSV fails validation.

    A distinct exception type (rather than a bare ``ValueError``) lets callers
    catch data-quality problems specifically, separately from ordinary
    programming errors that also happen to raise ``ValueError``.
    """


def _parse_created_at(raw: str, *, ticket_id: str) -> datetime:
    """Parse the CSV's ``created_at`` field into a ``datetime``.

    Args:
        raw: The raw field value, expected as ``YYYY-MM-DD HH:MM``.
        ticket_id: The owning ticket's id, included in errors so a bad row
            can be located immediately rather than requiring a line-number hunt.

    Returns:
        The parsed timestamp.

    Raises:
        DataIntegrityError: If ``raw`` does not match the expected format.
    """
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise DataIntegrityError(
            f"{ticket_id}: created_at {raw!r} is not in YYYY-MM-DD HH:MM format"
        ) from exc


def _parse_required_float(raw: str, *, field: str, ticket_id: str) -> float:
    """Parse a field that must always be present (e.g. response_time_hrs).

    Args:
        raw: The raw field value.
        field: Column name, used only to make a validation error readable.
        ticket_id: The owning ticket's id, for the same reason.

    Returns:
        The parsed float.

    Raises:
        DataIntegrityError: If ``raw`` is blank or not a valid float.
    """
    text = raw.strip()
    if not text:
        raise DataIntegrityError(f"{ticket_id}: {field} is required but blank")
    try:
        return float(text)
    except ValueError as exc:
        raise DataIntegrityError(
            f"{ticket_id}: {field} {raw!r} is not numeric"
        ) from exc


def _parse_optional_float(raw: str, *, field: str, ticket_id: str) -> float | None:
    """Parse a field that is legitimately absent for unresolved tickets.

    Args:
        raw: The raw field value; blank means "not applicable".
        field: Column name, used only to make a validation error readable.
        ticket_id: The owning ticket's id, for the same reason.

    Returns:
        The parsed float, or ``None`` when the field is blank - never
        ``0.0`` and never NaN. This is the specific guarantee this module
        exists to provide: a missing resolution time must not silently
        become a real value that then corrupts an average.

    Raises:
        DataIntegrityError: If a non-blank value is not a valid float.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise DataIntegrityError(
            f"{ticket_id}: {field} {raw!r} is not numeric"
        ) from exc


def _parse_optional_int(raw: str, *, field: str, ticket_id: str) -> int | None:
    """Parse ``customer_rating``, which is absent for unresolved tickets.

    Args:
        raw: The raw field value; blank means "not rated".
        field: Column name, used only to make a validation error readable.
        ticket_id: The owning ticket's id, for the same reason.

    Returns:
        The parsed integer, or ``None`` when blank.

    Raises:
        DataIntegrityError: If a non-blank value is not a valid integer.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise DataIntegrityError(
            f"{ticket_id}: {field} {raw!r} is not a whole number"
        ) from exc


def _validate_enum(
    value: str, *, allowed: frozenset[str], field: str, ticket_id: str
) -> str:
    """Confirm a categorical field holds one of its documented values.

    Failing loudly here, at ingestion, is deliberate: a category the anomaly
    detectors or the SQL guard have never seen should be caught at startup,
    not discovered as a silently-wrong query result during the walkthrough.

    Args:
        value: The raw field value.
        allowed: The full set of valid values for this column.
        field: Column name, for the error message.
        ticket_id: The owning ticket's id, for the error message.

    Returns:
        ``value``, unchanged, when it is valid.

    Raises:
        DataIntegrityError: If ``value`` is not one of ``allowed``.
    """
    if value not in allowed:
        raise DataIntegrityError(
            f"{ticket_id}: {field} {value!r} is not one of {sorted(allowed)}"
        )
    return value


def _validate_header(fieldnames: list[str] | None, csv_path: Path) -> None:
    """Confirm the CSV carries every column the schema requires.

    Checked once, before any row is read, so a mis-named or missing column
    produces one clear message naming exactly what is absent - rather than a
    bare ``KeyError`` on the first row, which says nothing about which file or
    which column was at fault.

    Args:
        fieldnames: Header names as parsed by :class:`csv.DictReader`, or
            ``None`` when the file is completely empty.
        csv_path: Path to the CSV, for the error message.

    Raises:
        DataIntegrityError: If the header is absent or missing any column.
    """
    if not fieldnames:
        raise DataIntegrityError(f"{csv_path} has no header row")

    missing = [column for column in COLUMNS if column not in fieldnames]
    if missing:
        raise DataIntegrityError(
            f"{csv_path} is missing required column(s): {', '.join(missing)}"
        )


def _require_text(raw_row: dict[str, str | None], column: str, ticket_id: str) -> str:
    """Read a column that must hold a non-empty value.

    :class:`csv.DictReader` yields ``None`` for columns a short row simply
    does not reach, so calling ``.strip()`` on the result directly would raise
    ``AttributeError`` - an error that names neither the row nor the column.
    This converts that failure into a precise, actionable one.

    Args:
        raw_row: The row as parsed by :class:`csv.DictReader`.
        column: Name of the column to read.
        ticket_id: The owning ticket's id, for the error message.

    Returns:
        The column's value, stripped of surrounding whitespace.

    Raises:
        DataIntegrityError: If the column is absent or blank in this row.
    """
    value = raw_row.get(column)
    text = value.strip() if value is not None else ""
    if not text:
        raise DataIntegrityError(f"{ticket_id}: {column} is required but blank")
    return text


def _load_rows(csv_path: Path) -> tuple[list[tuple], datetime]:
    """Read and coerce every row of the source CSV.

    Args:
        csv_path: Path to ``support_tickets.csv``.

    Returns:
        A ``(rows, latest_created_at)`` pair. ``rows`` is a list of tuples in
        :data:`COLUMNS` order, ready for a parameterised ``INSERT``.
        ``latest_created_at`` is the maximum ``created_at`` seen, used to
        auto-anchor :data:`Database.as_of` when the operator has not pinned
        one explicitly.

    Raises:
        DataIntegrityError: If any row fails validation.
        FileNotFoundError: If ``csv_path`` does not exist.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Ticket CSV not found at {csv_path}")

    rows: list[tuple] = []
    latest_created_at: datetime | None = None

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        _validate_header(reader.fieldnames, csv_path)

        for line_number, raw_row in enumerate(reader, start=2):  # row 1 is the header
            # Fall back to the line number when the id itself is missing, so
            # the error can still point at a specific place in the file.
            raw_id = (raw_row.get("ticket_id") or "").strip()
            ticket_id = raw_id or f"line {line_number}"
            created_at = _parse_created_at(
                _require_text(raw_row, "created_at", ticket_id), ticket_id=ticket_id
            )

            if latest_created_at is None or created_at > latest_created_at:
                latest_created_at = created_at

            rows.append(
                (
                    _require_text(raw_row, "ticket_id", ticket_id),
                    created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    _validate_enum(
                        _require_text(raw_row, "category", ticket_id),
                        allowed=CATEGORIES,
                        field="category",
                        ticket_id=ticket_id,
                    ),
                    _validate_enum(
                        _require_text(raw_row, "priority", ticket_id),
                        allowed=PRIORITIES,
                        field="priority",
                        ticket_id=ticket_id,
                    ),
                    _validate_enum(
                        _require_text(raw_row, "status", ticket_id),
                        allowed=STATUSES,
                        field="status",
                        ticket_id=ticket_id,
                    ),
                    _parse_required_float(
                        _require_text(raw_row, "response_time_hrs", ticket_id),
                        field="response_time_hrs",
                        ticket_id=ticket_id,
                    ),
                    _parse_optional_float(
                        raw_row.get("resolution_time_hrs") or "",
                        field="resolution_time_hrs",
                        ticket_id=ticket_id,
                    ),
                    _require_text(raw_row, "agent_id", ticket_id),
                    _parse_optional_int(
                        raw_row.get("customer_rating") or "",
                        field="customer_rating",
                        ticket_id=ticket_id,
                    ),
                    _require_text(raw_row, "issue_summary", ticket_id),
                )
            )

    if latest_created_at is None:
        raise DataIntegrityError(f"{csv_path} contains no data rows")

    return rows, latest_created_at


def build_database(
    csv_path: Path | None = None, db_path: Path | None = None
) -> Database:
    """Build a fresh SQLite database from the source CSV.

    The database file is deleted and rebuilt from scratch on every call. It is
    a build artifact derived entirely from the CSV, not a source of truth in
    its own right, so there is nothing to preserve between runs - and rebuilding
    guarantees the running system always reflects the current CSV on disk.

    Args:
        csv_path: Path to the source CSV. Defaults to ``settings.csv_path``.
        db_path: Where to write the SQLite file. Defaults to a file named
            ``tickets.db`` next to the CSV. Excluded from version control by
            ``.gitignore`` (``*.db``).

    Returns:
        Metadata describing the database that was built.

    Raises:
        DataIntegrityError: If any row in the CSV fails validation.
        FileNotFoundError: If the CSV does not exist.
    """
    csv_path = csv_path or settings.csv_path
    db_path = db_path or csv_path.with_name("tickets.db")

    if db_path.exists():
        try:
            db_path.unlink()
        except PermissionError as exc:
            # Windows refuses to delete a file that another handle still has
            # open. In practice that means a previous connection was not
            # closed - a leak worth naming explicitly, because the raw OS
            # error ("process cannot access the file") gives no hint as to
            # which process or why.
            raise RuntimeError(
                f"Cannot rebuild {db_path.name}: the file is still open by "
                "another connection. Close existing connections (or restart "
                "the server) and try again."
            ) from exc

    rows, latest_created_at = _load_rows(csv_path)

    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(_CREATE_TABLE_SQL)
        connection.executemany(_INSERT_SQL, rows)
        connection.commit()
    finally:
        connection.close()

    as_of = settings.as_of or latest_created_at

    logger.info(
        "Built %s: %d rows, as_of=%s%s",
        db_path.name,
        len(rows),
        as_of.isoformat(sep=" "),
        " (configured)" if settings.as_of else " (auto-anchored)",
    )

    return Database(path=db_path, as_of=as_of, row_count=len(rows))


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to an already-built database.

    Read-only is enforced at the operating-system/SQLite level via the
    ``mode=ro`` URI parameter - independently of :mod:`app.sql_guard`'s SQL
    text validation. The two mechanisms fail independently: even if a
    malicious or buggy query slipped past the guard, this connection
    physically cannot execute a write.

    A new connection is returned on every call rather than sharing one
    module-level connection, because SQLite connections are not safe to share
    across threads by default, and FastAPI may serve concurrent requests on
    different threads. Opening a connection is inexpensive relative to a
    network-bound LLM call, so this trades a negligible cost for correctness.

    Args:
        db_path: Path to a database previously created by
            :func:`build_database`.

    Returns:
        A read-only :class:`sqlite3.Connection`. Rows are returned as
        :class:`sqlite3.Row` objects, which support both index and column-name
        access - convenient for serialising query results to JSON.

    Raises:
        sqlite3.OperationalError: If ``db_path`` does not exist. SQLite's
            ``mode=ro`` refuses to create a missing file, unlike a normal
            connection - which is exactly the safety property wanted here.
    """
    # Path.as_uri() - rather than hand-building an f-string - is what makes
    # this correct on Windows: it emits the required triple-slash form
    # ("file:///C:/...") and percent-encodes reserved characters. That
    # matters concretely on this machine, where the repository sits under
    # "AI Assessment", a folder name containing a literal space.
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def read_only_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection and guarantee it is closed.

    The preferred way to query. :func:`get_connection` hands back a connection
    the caller must remember to close; forgetting leaks a file handle, and on
    Windows a leaked handle also blocks the next :func:`build_database` from
    deleting the file. This wrapper removes that failure mode entirely, and
    closes correctly even when the body raises.

    Example:
        >>> with read_only_connection(db.path) as connection:
        ...     rows = connection.execute("SELECT COUNT(*) FROM tickets").fetchall()

    Args:
        db_path: Path to a database previously created by
            :func:`build_database`.

    Yields:
        A read-only :class:`sqlite3.Connection`.
    """
    connection = get_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()
