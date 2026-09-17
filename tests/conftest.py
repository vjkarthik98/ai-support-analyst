"""Shared pytest fixtures for the test suite.

Two principles drive everything in this file:

**Tests must be hermetic.** They never read the developer's real ``.env``,
never require a Groq API key, and never reach the network. A test suite that
passes only on the machine that wrote it is worth very little, and the
evaluator will run these on their own machine with no credentials configured.

**Tests must not mutate the working repository.** Every database a test builds
goes to pytest's temporary directory, never to ``data/tickets.db``. Otherwise
a test run would quietly change the state of a running development server.

The real ``data/support_tickets.csv`` *is* read, because the assessment's
correctness gates (500 rows, 173 unresolved, an anchor of 2024-03-30 18:06)
are claims about that specific file. Verifying them against a synthetic
fixture would prove nothing about the data actually being shipped.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.data import COLUMNS, Database, build_database

# The repository root, derived from this file's location so it holds wherever
# pytest is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Environment variables that would otherwise leak a developer's real settings
# into a test run. Cleared before every test - see :func:`isolated_environment`.
_LEAKY_ENV_VARS = (
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "CSV_PATH",
    "AS_OF",
    "IQR_MULTIPLIER",
    "SLA_BREACH_HOURS",
    "API_HOST",
    "API_PORT",
    "UI_PORT",
    "MAX_RESULT_ROWS",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove configuration environment variables for the duration of a test.

    Applied automatically to every test. ``pydantic-settings`` reads real
    environment variables in addition to any ``.env`` file, so a developer with
    ``GROQ_API_KEY`` exported in their shell would otherwise get different
    results from a colleague - or from CI - without either knowing why.

    Args:
        monkeypatch: pytest's environment patcher; it restores the original
            values automatically when the test ends.
    """
    for name in _LEAKY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def project_csv() -> Path:
    """Return the path to the real ticket dataset shipped with the project.

    Returns:
        Path to ``data/support_tickets.csv``.
    """
    csv_path = REPO_ROOT / "data" / "support_tickets.csv"
    if not csv_path.exists():  # pragma: no cover - guards a broken checkout
        pytest.fail(f"Dataset missing from the repository: {csv_path}")
    return csv_path


@pytest.fixture(scope="session")
def real_database(project_csv: Path, tmp_path_factory: pytest.TempPathFactory) -> Database:
    """Build a database from the real dataset, in a temporary location.

    Session-scoped because the result is read-only for every consumer and
    rebuilding it per test would repeat the same work for no benefit.

    Args:
        project_csv: Path to the shipped dataset.
        tmp_path_factory: pytest factory for session-scoped temporary paths.

    Returns:
        Metadata for the freshly built database.
    """
    db_path = tmp_path_factory.mktemp("database") / "tickets.db"
    return build_database(csv_path=project_csv, db_path=db_path)


@pytest.fixture
def csv_header() -> str:
    """Return the dataset's header row.

    Derived from :data:`app.data.COLUMNS` rather than hard-coded, so a schema
    change updates the fixtures automatically instead of leaving tests asserting
    against a header the code no longer expects.

    Returns:
        A comma-separated header line, without a trailing newline.
    """
    return ",".join(COLUMNS)


@pytest.fixture
def write_csv(tmp_path: Path, csv_header: str) -> Callable[..., Path]:
    """Return a factory that writes a CSV file for a single test.

    Most data-layer tests need a deliberately malformed file - a missing
    column, a bad enum, a truncated row. This keeps that setup to one line per
    test instead of repeated boilerplate.

    Args:
        tmp_path: pytest's per-test temporary directory.
        csv_header: The standard header row.

    Returns:
        A callable ``write_csv(*rows, header=None, name="tickets.csv")`` that
        writes the given data rows beneath a header and returns the path. Pass
        ``header`` explicitly to test a malformed or missing header.
    """

    def _write(*rows: str, header: str | None = None, name: str = "tickets.csv") -> Path:
        lines: list[str] = []
        if header is not None:
            lines.append(header)
        elif header is None and rows:
            lines.append(csv_header)

        lines.extend(rows)
        path = tmp_path / name
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def valid_row() -> Callable[..., str]:
    """Return a factory producing a single valid CSV data row.

    Tests override one field at a time to isolate exactly which validation rule
    is under test, without restating the other nine columns each time.

    Returns:
        A callable accepting any column name as a keyword argument and
        returning a comma-separated row using the defaults for the rest.
    """

    def _row(**overrides: str) -> str:
        fields: dict[str, str] = {
            "ticket_id": "TKT-001",
            "created_at": "2024-01-15 09:30",
            "category": "Billing",
            "priority": "High",
            "status": "Resolved",
            "response_time_hrs": "1.5",
            "resolution_time_hrs": "4.2",
            "agent_id": "AGT-01",
            "customer_rating": "4",
            "issue_summary": "Incorrect charge on invoice",
        }
        fields.update(overrides)
        return ",".join(fields[column] for column in COLUMNS)

    return _row


@pytest.fixture
def env_file(tmp_path: Path) -> Callable[[str], Path]:
    """Return a factory that writes a temporary ``.env`` file.

    Configuration tests must never read the developer's real ``.env``, which
    holds a live API key.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        A callable taking the file's contents and returning its path.
    """

    def _write(contents: str) -> Path:
        path = tmp_path / ".env.test"
        path.write_text(contents, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def path_with_space(tmp_path: Path) -> Iterator[Path]:
    """Yield a directory whose name contains a space.

    Regression cover for a real defect class on this project: the repository
    lives under ``AI Assessment``, and a hand-built ``file:`` URI breaks on the
    space. Only :meth:`pathlib.Path.as_uri` percent-encodes it correctly.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Yields:
        An existing directory named with a space in it.
    """
    directory = tmp_path / "AI Assessment"
    directory.mkdir()
    yield directory


@pytest.fixture
def no_leaked_env() -> Iterator[None]:
    """Assert a test left no configuration variables behind.

    A safety net for tests that set environment variables themselves: if one
    forgets to clean up, the failure surfaces here rather than as a mysterious
    failure in an unrelated test later in the run.

    Yields:
        ``None``; the assertion runs after the test body completes.
    """
    yield
    leaked = [name for name in _LEAKY_ENV_VARS if name in os.environ]
    assert not leaked, f"Test leaked environment variables: {leaked}"
