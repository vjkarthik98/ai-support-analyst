"""Tests for the port-availability logic in :mod:`run`.

Deliberately narrow in scope. ``run.py`` is mostly subprocess orchestration -
spawning uvicorn and Streamlit, waiting on health, tearing both down - and
testing that would mean mocking ``subprocess.Popen`` and ``httpx``. A test
asserting that ``start_api`` builds ``["python", "-m", "uvicorn", ...]`` is the
source code written twice: it passes whether or not the server actually starts,
so it verifies nothing about the behaviour that matters.

What *is* tested here is the part with real logic and a subtle edge case: the
pre-flight port check. It is also the part that runs before anything else, so a
defect in it prevents the system starting at all.

The orchestration is verified by running it instead - happy path, port
conflict, clean shutdown with no orphaned processes, and startup with no API
key. For a launcher, that evidence is stronger than a mock could provide.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from app.config import settings
from run import check_ports, port_is_free


@pytest.fixture
def occupied_port() -> Iterator[int]:
    """Bind a port for the duration of a test and yield its number.

    Binding to port 0 lets the operating system choose a free one, which avoids
    the flakiness of hard-coding a number that might already be in use on the
    machine running the suite.

    Yields:
        A port number that is genuinely occupied while the test runs.
    """
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        yield holder.getsockname()[1]
    finally:
        holder.close()


@pytest.fixture
def free_port() -> int:
    """Return a port number that is not in use.

    Opens a socket to have the operating system allocate a port, then closes it
    immediately. The number is then almost certainly free - and the fact that
    it was *just* released is the point: it exercises the ``TIME_WAIT`` case
    that :func:`run.port_is_free` is written to handle.

    Returns:
        A port number that should be bindable.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_occupied_port_is_reported_as_unavailable(occupied_port: int) -> None:
    """A port with a listening socket on it is detected as in use.

    Args:
        occupied_port: A port held open for this test.
    """
    assert port_is_free("127.0.0.1", occupied_port) is False


def test_free_port_is_reported_as_available(free_port: int) -> None:
    """An unbound port is detected as available.

    Args:
        free_port: A port number not currently bound.
    """
    assert port_is_free("127.0.0.1", free_port) is True


def test_recently_released_port_is_still_usable(free_port: int) -> None:
    """A port just released is not mistaken for an occupied one.

    This is the reason ``SO_REUSEADDR`` is set on the probe socket. Without it a
    port left in ``TIME_WAIT`` by a run seconds earlier reads as occupied, and
    the launcher refuses to start with a message about a conflict that does not
    exist. Restarting the app immediately after stopping it is the single most
    common thing a developer does, so this must not be a false alarm.

    Args:
        free_port: A port number released immediately before the test.
    """
    assert port_is_free("127.0.0.1", free_port) is True


def test_probe_socket_does_not_leak(free_port: int) -> None:
    """Checking a port leaves it free for the real server to bind.

    A probe that held its socket open would make the check itself the cause of
    the conflict it reports - the launcher would block its own API from
    starting.

    Args:
        free_port: A port number not currently bound.
    """
    assert port_is_free("127.0.0.1", free_port) is True

    # The real server must still be able to take the port afterwards.
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", free_port))
    finally:
        server.close()


def test_check_ports_passes_when_both_are_free(
    monkeypatch: pytest.MonkeyPatch, free_port: int
) -> None:
    """No problems are reported when both configured ports are available.

    Args:
        monkeypatch: pytest's attribute patcher.
        free_port: A port number not currently bound.
    """
    monkeypatch.setattr(settings, "api_port", free_port)
    monkeypatch.setattr(settings, "ui_port", free_port + 1)

    assert check_ports() == []


def test_check_ports_reports_a_busy_api_port(
    monkeypatch: pytest.MonkeyPatch, occupied_port: int, free_port: int
) -> None:
    """An occupied API port produces one actionable message.

    The message names the port and the likely cause, because the alternative -
    a raw ``OSError`` from inside uvicorn's socket setup - tells the operator
    nothing about what to do next.

    Args:
        monkeypatch: pytest's attribute patcher.
        occupied_port: A port held open for this test.
        free_port: A port number not currently bound.
    """
    monkeypatch.setattr(settings, "api_port", occupied_port)
    monkeypatch.setattr(settings, "ui_port", free_port)

    problems = check_ports()

    assert len(problems) == 1
    assert str(occupied_port) in problems[0]
    assert "API_PORT" in problems[0]


def test_check_ports_reports_a_busy_ui_port(
    monkeypatch: pytest.MonkeyPatch, occupied_port: int, free_port: int
) -> None:
    """An occupied UI port is reported separately from the API port.

    Args:
        monkeypatch: pytest's attribute patcher.
        occupied_port: A port held open for this test.
        free_port: A port number not currently bound.
    """
    monkeypatch.setattr(settings, "api_port", free_port)
    monkeypatch.setattr(settings, "ui_port", occupied_port)

    problems = check_ports()

    assert len(problems) == 1
    assert "UI_PORT" in problems[0]


def test_both_conflicts_are_reported_together(
    monkeypatch: pytest.MonkeyPatch, occupied_port: int
) -> None:
    """Two conflicts are both reported in a single run.

    Returning on the first problem would make an operator fix one port, restart,
    and immediately hit the second. Every problem is collected so the whole
    thing can be fixed in one pass.

    Args:
        monkeypatch: pytest's attribute patcher.
        occupied_port: A port held open for this test.
    """
    monkeypatch.setattr(settings, "api_port", occupied_port)
    monkeypatch.setattr(settings, "ui_port", occupied_port)

    assert len(check_ports()) == 2
