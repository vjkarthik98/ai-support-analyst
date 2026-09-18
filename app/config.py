"""Typed application settings, loaded once from the environment.

Every other module reads configuration through the single :class:`Settings`
instance exported here (``settings``) rather than calling ``os.getenv``
directly. Centralising this serves the Single Responsibility Principle: one
module owns "where configuration comes from and whether it is valid", so a
bad value is rejected the moment the process starts, with a clear error -
never discovered later as a confusing failure three calls deep into a query.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved relative to this file, not the process's current working directory,
# so behaviour does not depend on which folder the app happens to be launched
# from (repo root, app/, or an IDE's own working directory).
REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application configuration, validated at process startup.

    Values are read from a ``.env`` file at the repository root, falling back
    to real environment variables, then to the defaults declared below. See
    ``.env.example`` for a description of every field from the operator's
    point of view.

    Attributes:
        groq_api_key: Secret key for the Groq API, or ``None`` when not
            configured. Absent, blank and placeholder values all normalise to
            ``None``. Without it the natural-language path is unavailable, but
            the deterministic anomaly and health endpoints still serve
            normally - see :attr:`llm_enabled`.
        groq_model: Model id to use for tool-calling and narration.
        llm_max_retries: Attempts to retry a transient provider failure. Rate
            limits and timeouts are never retried regardless of this value.
        llm_timeout_seconds: Wall-clock limit on a single provider call.
        csv_path: Path to the source ticket data, relative to the repo root.
        as_of: Optional fixed reference "now" for relative-time questions.
            Left as ``None`` to auto-anchor to the dataset's own latest
            timestamp - see :mod:`app.data`.
        iqr_multiplier: Tukey-fence multiplier for resolution-time outliers.
        sla_breach_hours: Age, in hours, after which an unresolved
            High/Critical ticket counts as an SLA breach.
        api_host: Interface the FastAPI server binds to.
        api_port: Port the FastAPI server binds to.
        ui_port: Port the Streamlit UI binds to.
        max_result_rows: Hard cap on rows a single SQL query may return.
        query_timeout_seconds: Wall-clock limit on a single query's execution,
            after which it is aborted.
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        # Unrecognised keys in .env are rejected rather than silently ignored,
        # so a typo'd variable name fails loudly instead of quietly using a
        # default that no one intended.
        extra="forbid",
    )

    # --- LLM provider --------------------------------------------------
    # Optional by design, not by oversight. The anomaly detectors are pure
    # statistics and need no LLM, so the API must start and serve /health and
    # /anomalies with no key present - and the test suite must import this
    # module without credentials. A missing key therefore degrades the system
    # to its deterministic subset rather than preventing startup; only the
    # natural-language path refuses, and it says exactly why.
    groq_api_key: str | None = Field(
        default=None, description="Groq API key from console.groq.com"
    )
    groq_model: str = Field(default="openai/gpt-oss-120b")

    # How many times a *transient* provider failure is retried - a dropped
    # connection or a 5xx. Rate limits and timeouts are deliberately never
    # retried; see app.llm.is_retryable for why.
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # Wall-clock ceiling on one provider call. The SDK's own default allows 60
    # seconds for a read, longer than a user will wait. A call that times out
    # is not retried, so this bounds each call; the UI derives its own request
    # timeout from it, so the two cannot drift apart.
    llm_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Dataset ---------------------------------------------------------
    csv_path: Path = Field(default=Path("data/support_tickets.csv"))
    as_of: datetime | None = Field(default=None)

    # --- Anomaly detection thresholds ------------------------------------
    iqr_multiplier: float = Field(default=1.5, gt=0)
    sla_breach_hours: int = Field(default=24, gt=0)

    # --- Service ports -----------------------------------------------------
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1, le=65535)
    ui_port: int = Field(default=8501, ge=1, le=65535)

    # --- Query safety ------------------------------------------------------
    max_result_rows: int = Field(default=500, gt=0)

    # A generated query can be a perfectly valid read-only SELECT and still run
    # forever - a recursive CTE is the obvious case. Validation cannot catch
    # that, because the statement is legitimate; only an execution limit can.
    # Five seconds is far longer than any honest query over 500 rows needs.
    query_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- Diagnostics -------------------------------------------------------
    # Configurable so someone running this for the first time can raise it to
    # DEBUG and watch the model's tool choice, the SQL it ran and each
    # detector's decision, without editing code to find out what it did.
    # Applied to this application's loggers only - see app.main's
    # configure_logging for why libraries stay at INFO.
    log_level: str = Field(default="INFO")

    @field_validator("groq_api_key", mode="before")
    @classmethod
    def _normalise_api_key(cls, value: object) -> object:
        """Reduce a blank or placeholder key to ``None``.

        Three inputs mean the same thing operationally - the key is not
        configured - but arrive in different shapes: the variable is absent,
        present but empty (``GROQ_API_KEY=``), or still carrying the template
        value from ``.env.example``. Collapsing all three to ``None`` here
        means the rest of the codebase has exactly one condition to check,
        instead of every call site repeating the same three-way test.

        Deliberately does not raise. A missing key is a valid operating mode
        (deterministic endpoints only), so it must not prevent startup. The
        LLM client raises the actionable error, at the point where a key is
        genuinely required.

        Args:
            value: The raw value from the environment, or ``None`` if absent.

        Returns:
            ``None`` when the key is blank or a known placeholder; otherwise
            the value stripped of surrounding whitespace.
        """
        if not isinstance(value, str):
            return value

        key = value.strip()
        if not key or key.startswith(("gsk_replace", "gsk_paste")):
            return None
        return key

    @property
    def llm_enabled(self) -> bool:
        """Whether natural-language querying is available.

        Returns:
            ``True`` when a usable API key is configured. Callers use this to
            decide between serving the LLM path and reporting a clear,
            specific "not configured" response - and ``/health`` surfaces it
            so an operator can see the running mode at a glance.
        """
        return self.groq_api_key is not None

    @field_validator("as_of", mode="before")
    @classmethod
    def _blank_as_of_means_auto(cls, value: object) -> object:
        """Treat an empty AS_OF value as "not set" rather than a parse error.

        A ``.env`` line of ``AS_OF=`` gives python-dotenv the string ``""``,
        not a missing key - so pydantic's ``Optional[datetime] = None``
        default never kicks in, and datetime parsing fails on an empty
        string. Blank is exactly what ``.env.example`` documents as "leave
        blank to auto-anchor", so it must be treated as ``None`` here rather
        than surfaced as a startup error.

        Args:
            value: The raw value for AS_OF before type coercion - normally a
                string from the environment, or already a datetime/None if
                constructed programmatically (e.g. in tests).

        Returns:
            ``None`` when the value is a blank or whitespace-only string;
            otherwise the value unchanged, for pydantic to parse normally.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("as_of")
    @classmethod
    def _reject_timezone(cls, value: datetime | None) -> datetime | None:
        """Refuse an ``AS_OF`` that carries a timezone.

        The dataset's timestamps carry none. Python refuses to compare or
        subtract a timezone-aware datetime and a naive one, so an ``AS_OF``
        such as ``2024-03-30T18:06Z`` started cleanly and then failed on the
        first anomaly check with an error that never mentioned AS_OF.
        Converting it silently would be worse: there is no way to know which
        zone the dataset was recorded in.

        Args:
            value: The parsed timestamp, or ``None`` when not set.

        Returns:
            ``value`` unchanged when it has no timezone.

        Raises:
            ValueError: If ``value`` includes a timezone.
        """
        if value is not None and value.tzinfo is not None:
            raise ValueError(
                "AS_OF must not include a timezone, because the ticket "
                "timestamps have none. Write it as YYYY-MM-DD HH:MM."
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        """Accept a log level in any case, rejecting anything unrecognised.

        Validated here rather than passed straight to :mod:`logging`, which
        silently ignores a name it does not recognise - so a typo like "DEUBG"
        would leave the level at its default and quietly explain nothing, at
        exactly the moment someone was trying to diagnose a problem.

        Args:
            value: The configured level name.

        Returns:
            The level name in upper case.

        Raises:
            ValueError: If the name is not a standard logging level.
        """
        level = value.strip().upper()
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in valid:
            raise ValueError(
                f"LOG_LEVEL {value!r} is not recognised. "
                f"Use one of: {', '.join(sorted(valid))}"
            )
        return level

    @field_validator("csv_path")
    @classmethod
    def _resolve_csv_path(cls, value: Path) -> Path:
        """Anchor a relative CSV path to the repository root.

        Args:
            value: The path as given in configuration, absolute or relative.

        Returns:
            An absolute path: ``value`` unchanged if already absolute,
            otherwise ``value`` resolved against :data:`REPO_ROOT`.
        """
        return value if value.is_absolute() else REPO_ROOT / value


# Instantiated once at import time. Every module that needs configuration
# imports this shared instance:
#
#     from app.config import settings
#
# rather than constructing its own Settings(), so the whole process agrees on
# one set of values and pays the .env parsing and validation cost only once.
settings = Settings()
