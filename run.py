"""Single-command launcher for the API and the user interface.

    python run.py

The brief requires the system to start with one command. Two processes are
needed - a uvicorn server and a Streamlit server - so this script owns starting
both, ordering them correctly, and stopping them together.

Most of the code here exists for the failure cases rather than the happy path,
because those are what go wrong on someone else's machine:

**Ports already in use.** Checked before anything starts, so the failure is one
clear sentence naming the port and the likely cause, rather than a traceback
from deep inside a server's socket setup.

**Racing the UI against the API.** Streamlit starts far faster than the API,
which must parse a CSV and build a database first. Launching them together
means the interface renders "cannot reach the API" as its first impression.
This waits for a genuine 200 from /health before starting the UI.

**Orphaned processes.** If the launcher exits while a server keeps running, the
next attempt fails on a port already in use - and the cause is invisible.
Shutdown is therefore handled on every exit path, including Ctrl+C, and
escalates from a polite terminate to a kill if a process ignores it.

Windows needs particular care on that last point, which is why signals are not
used to stop children: CTRL_C_EVENT propagates to the whole process group and
is awkward to target. Calling terminate() on each child is simpler and behaves
the same on every platform.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from app import __version__
from app.config import settings

# stdout is block-buffered when it is not a terminal, while stderr never is.
# Left alone, an error can surface above the progress line that was printed
# before it, making the checks look as though they ran out of order. Switching
# stdout to line buffering keeps the two streams in the order they were written.
sys.stdout.reconfigure(line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent
UI_SCRIPT = REPO_ROOT / "ui" / "streamlit_app.py"

# The API parses a 500-row CSV, builds a SQLite database and constructs the
# query service before it will answer. Twenty seconds is far longer than that
# takes, and only matters when something is genuinely wrong.
STARTUP_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.3

# How long a process is given to exit politely before it is killed.
SHUTDOWN_GRACE_SECONDS = 5.0


def port_is_free(host: str, port: int) -> bool:
    """Report whether a TCP port can be bound.

    Args:
        host: Interface to test.
        port: Port number to test.

    Returns:
        ``True`` when the port is available.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Without SO_REUSEADDR a port left in TIME_WAIT by a recent run would
        # read as occupied, and the launcher would refuse to start for no real
        # reason.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def check_ports() -> list[str]:
    """Check both required ports before starting anything.

    Returns:
        A list of human-readable problems. Empty when both ports are free.
    """
    problems: list[str] = []

    if not port_is_free(settings.api_host, settings.api_port):
        problems.append(
            f"Port {settings.api_port} (API) is already in use. Another copy of "
            "this app may still be running - close it, or change API_PORT in "
            "your .env file."
        )

    # Streamlit binds loopback per .streamlit/config.toml, so that is what is
    # tested - checking a different interface could pass while the real bind
    # fails.
    if not port_is_free("127.0.0.1", settings.ui_port):
        problems.append(
            f"Port {settings.ui_port} (UI) is already in use. Close the other "
            "process, or change UI_PORT in your .env file."
        )

    return problems


def start_api() -> subprocess.Popen[bytes]:
    """Launch the FastAPI server.

    Returns:
        The running server process.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            settings.api_host,
            "--port",
            str(settings.api_port),
        ],
        cwd=REPO_ROOT,
    )


def start_ui() -> subprocess.Popen[bytes]:
    """Launch the Streamlit server.

    Server options are passed on the command line as well as being set in
    ``.streamlit/config.toml``. The config file is the documented home for
    them, but a user with a conflicting global Streamlit config would otherwise
    override it - and the first-run email prompt would block startup.

    Returns:
        The running UI process.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(UI_SCRIPT),
            "--server.port",
            str(settings.ui_port),
            "--server.headless",
            "true",
            "--server.address",
            "localhost",
        ],
        cwd=REPO_ROOT,
    )


def wait_for_api(process: subprocess.Popen[bytes]) -> bool:
    """Block until the API answers /health, or until it fails.

    Polls rather than sleeping a fixed interval, so startup is as fast as the
    machine allows instead of a guessed constant. The child process is checked
    on each pass so that a server which dies immediately - a port conflict, a
    malformed CSV - is reported at once rather than after the full timeout.

    Args:
        process: The API process being waited on.

    Returns:
        ``True`` once the API is ready, ``False`` if it failed or timed out.
    """
    url = f"http://{settings.api_host}:{settings.api_port}/health"
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(
                f"\n  The API exited during startup (code {process.returncode}). "
                "Its error output is above.",
                file=sys.stderr,
            )
            return False

        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            # Not yet listening. Expected for the first second or so.
            pass

        time.sleep(POLL_INTERVAL_SECONDS)

    print(
        f"\n  The API did not become healthy within "
        f"{STARTUP_TIMEOUT_SECONDS:.0f}s.",
        file=sys.stderr,
    )
    return False


def stop(process: subprocess.Popen[bytes] | None, name: str) -> None:
    """Stop a child process, escalating if it does not comply.

    Args:
        process: The process to stop, or ``None`` if it never started.
        name: Label used in messages.
    """
    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # A process that ignores terminate() would otherwise be orphaned and
        # hold its port, making the next run fail for an invisible reason.
        print(f"  {name} did not stop cleanly; killing it.")
        process.kill()
        process.wait()


def main() -> int:
    """Start both services and run until interrupted.

    Returns:
        A shell exit code: 0 on clean shutdown, 1 if startup failed.
    """
    print(f"\n  AI Support Ticket Analyst v{__version__}")
    print("  " + "-" * 44)

    if not UI_SCRIPT.exists():  # pragma: no cover - guards a broken checkout
        print(f"  UI script missing: {UI_SCRIPT}", file=sys.stderr)
        return 1

    problems = check_ports()
    if problems:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    if not settings.llm_enabled:
        print(
            "  No GROQ_API_KEY set - starting without natural-language\n"
            "  querying. Anomaly detection and the API still work.\n"
        )

    api: subprocess.Popen[bytes] | None = None
    ui: subprocess.Popen[bytes] | None = None

    try:
        print(f"  [1/2] API  http://{settings.api_host}:{settings.api_port}")
        api = start_api()

        if not wait_for_api(api):
            return 1

        print(f"        docs http://{settings.api_host}:{settings.api_port}/docs")
        print(f"  [2/2] UI   http://localhost:{settings.ui_port}")
        ui = start_ui()

        print("\n  Both services running. Press Ctrl+C to stop.\n")

        # Wait on the UI: it is the process a user closes to end the session.
        # If it exits on its own, the API is torn down with it rather than left
        # running invisibly.
        ui.wait()

    except KeyboardInterrupt:
        print("\n  Stopping...")
    finally:
        # Reverse start order, so the UI stops before the API it depends on.
        stop(ui, "UI")
        stop(api, "API")
        print("  Stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
