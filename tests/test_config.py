"""Tests for :mod:`app.config`, the typed settings layer.

Scope is deliberately narrow. ``Settings`` is mostly declarative, and asserting
that pydantic can coerce ``"8000"`` into an ``int`` tests pydantic, not this
project. What *is* worth testing is the custom logic layered on top: the two
validators, and the operating mode they decide between.

The most important test here - :func:`test_settings_load_without_an_api_key` -
is a regression guard. The key was originally a required field, which meant
importing :mod:`app.config` raised without credentials. Because
:mod:`app.data` imports settings, that broke the entire application, including
the anomaly endpoints that use no LLM at all, and would have broken this test
suite at collection time. Nothing structural prevents that mistake returning,
so it is pinned here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

# A minimal .env holding nothing that affects the behaviour under test.
_MINIMAL_ENV = "GROQ_MODEL=openai/gpt-oss-120b\n"


def test_settings_load_without_an_api_key(env_file: Callable[[str], Path]) -> None:
    """The application starts with no Groq key configured.

    Regression cover for a defect that broke two commitments at once: that
    ``/anomalies`` serves with the key unset, and that the test suite runs
    offline with no credentials.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file(_MINIMAL_ENV)))

    assert settings.groq_api_key is None
    assert settings.llm_enabled is False


@pytest.mark.parametrize(
    "raw_key",
    [
        pytest.param("", id="blank value"),
        pytest.param("   ", id="whitespace only"),
        pytest.param("gsk_replace_me", id="placeholder from .env.example"),
        pytest.param("gsk_paste_your_real_key_here", id="paste-me placeholder"),
    ],
)
def test_unusable_keys_normalise_to_none(
    raw_key: str, env_file: Callable[[str], Path]
) -> None:
    """Blank and placeholder keys are treated as "not configured".

    All three shapes mean the same thing operationally, so they collapse to one
    value. Otherwise every call site would repeat the same three-way check, and
    eventually one of them would forget a case.

    Args:
        raw_key: A key value that should be rejected as unusable.
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file(f"GROQ_API_KEY={raw_key}\n")))

    assert settings.groq_api_key is None
    assert settings.llm_enabled is False


def test_real_key_is_accepted_and_enables_the_llm(
    env_file: Callable[[str], Path],
) -> None:
    """A plausible key is kept verbatim and switches on the LLM path.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file("GROQ_API_KEY=gsk_realkey123\n")))

    assert settings.groq_api_key == "gsk_realkey123"
    assert settings.llm_enabled is True


def test_api_key_is_stripped_of_whitespace(env_file: Callable[[str], Path]) -> None:
    """Surrounding whitespace is removed from the key.

    A key pasted from a browser often carries a trailing space or newline,
    which the Groq API would reject as invalid credentials - an error that
    gives no hint the cause is whitespace.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file("GROQ_API_KEY=  gsk_padded  \n")))

    assert settings.groq_api_key == "gsk_padded"


def test_blank_as_of_means_auto_anchor(env_file: Callable[[str], Path]) -> None:
    """``AS_OF=`` is read as "not set" rather than failing to parse.

    Regression cover for a real startup crash. python-dotenv reports a blank
    assignment as the empty string, not as an absent key, so pydantic's
    ``None`` default never applied and datetime parsing failed on ``""``.
    ``.env.example`` documents blank as meaning "auto-anchor", so it must be
    accepted.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file("AS_OF=\n")))

    assert settings.as_of is None


def test_explicit_as_of_is_parsed(env_file: Callable[[str], Path]) -> None:
    """A pinned AS_OF timestamp is honoured.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file("AS_OF=2024-03-30 18:06:00\n")))

    assert settings.as_of == datetime(2024, 3, 30, 18, 6)


def test_relative_csv_path_resolves_against_the_repository_root(
    env_file: Callable[[str], Path],
) -> None:
    """A relative CSV path becomes absolute, anchored to the repo root.

    Without this the path would resolve against the current working directory,
    so the application would work when launched from the repo root and fail
    when launched from anywhere else.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file("CSV_PATH=data/support_tickets.csv\n")))

    assert settings.csv_path.is_absolute()
    assert settings.csv_path.name == "support_tickets.csv"


def test_absolute_csv_path_is_left_alone(
    env_file: Callable[[str], Path], tmp_path: Path
) -> None:
    """An absolute CSV path is used exactly as given.

    Args:
        env_file: Factory writing a temporary ``.env``.
        tmp_path: pytest's per-test temporary directory.
    """
    absolute = tmp_path / "elsewhere.csv"
    settings = Settings(_env_file=str(env_file(f"CSV_PATH={absolute}\n")))

    assert settings.csv_path == absolute


def test_unknown_env_keys_are_rejected(env_file: Callable[[str], Path]) -> None:
    """A misspelled variable fails loudly instead of being ignored.

    ``extra="forbid"`` exists so that ``GROK_MODEL=...`` - a plausible typo -
    surfaces immediately, rather than silently leaving the real setting at its
    default and producing behaviour nobody asked for.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=str(env_file("GROK_MODEL=typo\n")))


@pytest.mark.parametrize(
    "assignment",
    [
        pytest.param("IQR_MULTIPLIER=0", id="iqr multiplier of zero"),
        pytest.param("IQR_MULTIPLIER=-1.5", id="negative iqr multiplier"),
        pytest.param("SLA_BREACH_HOURS=0", id="sla window of zero hours"),
        pytest.param("MAX_RESULT_ROWS=0", id="zero row cap"),
        pytest.param("API_PORT=0", id="port below range"),
        pytest.param("API_PORT=70000", id="port above range"),
        pytest.param("QUERY_TIMEOUT_SECONDS=0", id="zero query timeout"),
        pytest.param("QUERY_TIMEOUT_SECONDS=-1", id="negative query timeout"),
    ],
)
def test_out_of_range_values_are_rejected(
    assignment: str, env_file: Callable[[str], Path]
) -> None:
    """Numerically impossible settings are refused at startup.

    Each of these would otherwise fail much later and far less clearly - a zero
    row cap returning nothing, or a zero IQR multiplier flagging most of the
    dataset as anomalous.

    Args:
        assignment: A single invalid ``KEY=value`` line.
        env_file: Factory writing a temporary ``.env``.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=str(env_file(f"{assignment}\n")))


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        pytest.param("DEBUG", "DEBUG", id="upper case"),
        pytest.param("debug", "DEBUG", id="lower case is normalised"),
        pytest.param("  warning  ", "WARNING", id="whitespace is trimmed"),
    ],
)
def test_log_level_is_normalised(
    configured: str, expected: str, env_file: Callable[[str], Path]
) -> None:
    """A valid level is accepted in any case.

    Args:
        configured: The value as written in .env.
        expected: The normalised result.
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file(f"LOG_LEVEL={configured}\n")))

    assert settings.log_level == expected


def test_misspelled_log_level_is_rejected(env_file: Callable[[str], Path]) -> None:
    """An unrecognised level fails rather than being silently ignored.

    ``logging`` accepts an unknown level name without complaint and leaves the
    level unchanged, so a typo such as "DEUBG" would quietly explain nothing -
    at precisely the moment someone raised the level to diagnose a problem.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    with pytest.raises(ValidationError, match="not recognised"):
        Settings(_env_file=str(env_file("LOG_LEVEL=DEUBG\n")))


def test_defaults_match_the_documented_values(env_file: Callable[[str], Path]) -> None:
    """An empty configuration yields the values ``.env.example`` documents.

    Keeps the documented defaults and the code's defaults from drifting apart.

    Args:
        env_file: Factory writing a temporary ``.env``.
    """
    settings = Settings(_env_file=str(env_file("")))

    assert settings.groq_model == "openai/gpt-oss-120b"
    assert settings.iqr_multiplier == 1.5
    assert settings.sla_breach_hours == 24
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.ui_port == 8501
    assert settings.max_result_rows == 500
    assert settings.query_timeout_seconds == 5.0
    assert settings.log_level == "INFO"
