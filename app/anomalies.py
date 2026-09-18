"""Deterministic anomaly detection over the ticket dataset.

No language model participates in this module, by design. Statistics must be
reproducible, explainable and unit-testable: the same data must always yield
the same anomalies, and every flagged ticket must be able to state precisely
why it was flagged and against which threshold. A model asked to "find
anomalies" can do none of those things reliably.

A practical consequence worth demonstrating: ``/anomalies`` serves correctly
with no API key configured at all.

Two detectors ship here, chosen from what the data actually supports:

**Resolution-time outliers** use Tukey's interquartile fence rather than a
z-score. Resolution time is markedly right-skewed (mean 19.16 hours against a
median of 12.00, with a maximum of 119.7), and a z-score assumes a normal
distribution this data does not have. On the shipped dataset the IQR fence
flags 21 of 327 resolved tickets; ``z > 3`` flags only 7, missing most of the
genuinely slow ones.

**SLA breaches** are a business rule rather than a statistic: an unresolved
High or Critical ticket older than a configured age. Nothing statistical is
involved, so nothing statistical is pretended.

Response time is deliberately *not* monitored. It is bounded between 0.2 and
5.0 hours across all 500 rows with no outliers by either method, so a detector
over it would be code that can never fire.

Extending
---------
Detectors satisfy the :class:`AnomalyDetector` protocol and are added through
:func:`register_detector`. A new detector requires no change to any existing
one, nor to :func:`detect_anomalies` - the open/closed principle applied where
it earns its keep, since "what counts as anomalous" is exactly the kind of
requirement that grows after delivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import pandas as pd

from app.config import settings
from app.data import TABLE_NAME, read_only_connection

logger = logging.getLogger(__name__)

# Statuses that mean "this ticket is still outstanding". The brief's schema
# preview shows an Escalated ticket carrying a resolution time, but every
# Escalated row in the shipped data has none - so Escalated is treated as
# unresolved, following the data rather than the illustration.
UNRESOLVED_STATUSES: Final[frozenset[str]] = frozenset({"Open", "Escalated"})

# Priorities considered urgent enough for the SLA rule to apply.
URGENT_PRIORITIES: Final[frozenset[str]] = frozenset({"High", "Critical"})

# Quartiles computed from fewer than four values are not meaningful - with
# three points the "interquartile range" spans almost the entire sample, and
# the fence becomes arbitrary. Below this the detector reports nothing rather
# than inventing a threshold.
MIN_SAMPLE_FOR_QUARTILES: Final[int] = 4


@dataclass(frozen=True)
class Anomaly:
    """A single flagged ticket, carrying its own justification.

    Every field exists so the record can be explained without re-running the
    detector: the API returns it, the UI tabulates it, and the language model
    narrates it. A bare list of ticket ids would force each consumer to
    re-derive why the ticket was flagged.

    Attributes:
        ticket_id: Identifier of the flagged ticket.
        kind: Which detector flagged it.
        reason: Human-readable explanation, safe to show a user verbatim.
        value: The measured quantity that triggered the flag.
        threshold: The boundary the value crossed.
        created_at: When the ticket was raised.
        category: The ticket's category.
        priority: The ticket's priority.
        status: The ticket's status.
        agent_id: The assigned agent.
    """

    ticket_id: str
    kind: str
    reason: str
    value: float
    threshold: float
    created_at: datetime
    category: str
    priority: str
    status: str
    agent_id: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this anomaly.

        Returns:
            A mapping with the timestamp rendered as an ISO-8601 string, since
            ``datetime`` is not JSON-serialisable.
        """
        return {
            "ticket_id": self.ticket_id,
            "kind": self.kind,
            "reason": self.reason,
            "value": self.value,
            "threshold": self.threshold,
            "created_at": self.created_at.isoformat(sep=" "),
            "category": self.category,
            "priority": self.priority,
            "status": self.status,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True)
class AnomalyReport:
    """The outcome of running one detector.

    Reports the method and threshold even when nothing was flagged. "No
    anomalies found" is only trustworthy if the reader can see what was looked
    for and how - otherwise it is indistinguishable from a detector that
    silently failed.

    Attributes:
        kind: Identifier of the detector that produced this report.
        description: What this detector looks for, in plain language.
        method: How the threshold was derived.
        threshold: The boundary applied, or ``None`` when too little data
            existed to establish one.
        as_of: The reference time used for any age calculation.
        considered: How many tickets were eligible for evaluation.
        anomalies: The flagged tickets.

    """

    kind: str
    description: str
    method: str
    threshold: float | None
    as_of: datetime
    considered: int
    anomalies: list[Anomaly] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Return the number of anomalies found."""
        return len(self.anomalies)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this report.

        Returns:
            A mapping suitable for an API response body.
        """
        return {
            "kind": self.kind,
            "description": self.description,
            "method": self.method,
            "threshold": self.threshold,
            "as_of": self.as_of.isoformat(sep=" "),
            "considered": self.considered,
            "count": self.count,
            "anomalies": [anomaly.to_dict() for anomaly in self.anomalies],
        }


@runtime_checkable
class AnomalyDetector(Protocol):
    """The contract every detector satisfies.

    Detectors are interchangeable: :func:`detect_anomalies` invokes them
    without knowing which it holds, so a new one can be added without touching
    the dispatch logic.

    Attributes:
        kind: Stable identifier, used as the registry key and in API responses.
        description: What this detector looks for, in plain language.
    """

    kind: str
    description: str

    def detect(
        self,
        frame: pd.DataFrame,
        *,
        as_of: datetime,
        baseline: pd.DataFrame | None = None,
    ) -> AnomalyReport:
        """Evaluate a set of tickets and report the anomalous ones.

        Args:
            frame: Tickets to evaluate, already filtered to any time window.
            as_of: Reference time for age calculations.
            baseline: Tickets from which to derive a statistical threshold,
                defaulting to ``frame``. These differ whenever a time window is
                applied: the threshold should describe normal behaviour across
                all history, while only the windowed tickets are judged against
                it. Detectors using a fixed business rule ignore this.

        Returns:
            The detector's findings.
        """
        ...


def _row_to_anomaly(
    row: pd.Series, *, kind: str, reason: str, value: float, threshold: float
) -> Anomaly:
    """Build an :class:`Anomaly` from a DataFrame row.

    Args:
        row: A single ticket row.
        kind: The detector's identifier.
        reason: Human-readable justification for the flag.
        value: The measured quantity that triggered the flag.
        threshold: The boundary the value crossed.

    Returns:
        The populated anomaly record.
    """
    return Anomaly(
        ticket_id=str(row["ticket_id"]),
        kind=kind,
        reason=reason,
        value=round(float(value), 2),
        threshold=round(float(threshold), 2),
        created_at=row["created_at"].to_pydatetime(),
        category=str(row["category"]),
        priority=str(row["priority"]),
        status=str(row["status"]),
        agent_id=str(row["agent_id"]),
    )


@dataclass(frozen=True)
class ResolutionTimeOutlierDetector:
    """Flags tickets that took abnormally long to resolve.

    Uses Tukey's upper fence, ``Q3 + k * IQR``. Only the upper fence is
    applied: a ticket resolved unusually *fast* is not an operational problem,
    so flagging it would be noise.

    Attributes:
        multiplier: The ``k`` in the fence formula. 1.5 is the conventional
            value and flags 21 of 327 resolved tickets in the shipped data.
            Raising it flags fewer.
    """

    kind: str = "resolution_time_outlier"
    description: str = "Tickets whose resolution time is a statistical outlier"
    multiplier: float = 1.5

    def detect(
        self,
        frame: pd.DataFrame,
        *,
        as_of: datetime,
        baseline: pd.DataFrame | None = None,
    ) -> AnomalyReport:
        """Flag resolution times above the interquartile fence.

        The fence is derived from ``baseline`` - all history by default - while
        only ``frame`` is judged against it. The distinction matters whenever a
        time window is applied: recomputing quartiles from one quiet week of
        33 tickets pushed the threshold from 48h to 80h in testing, which would
        have silently excused a 60-hour resolution that is plainly abnormal by
        any historical standard. A threshold should describe normal behaviour,
        and one week is too small a sample to define it.

        Args:
            frame: Tickets to evaluate.
            as_of: Reference time, recorded in the report for traceability.
            baseline: Tickets defining "normal". Defaults to ``frame``.

        Returns:
            The detector's findings. When the baseline holds fewer than
            :data:`MIN_SAMPLE_FOR_QUARTILES` resolved tickets the report
            carries a ``None`` threshold and no anomalies, rather than a
            threshold derived from too little data.
        """
        method = f"Tukey upper fence: Q3 + {self.multiplier} x IQR"

        if baseline is None:
            baseline = frame

        # An empty DataFrame carries no columns, so column access would raise
        # KeyError rather than simply yielding nothing. A narrow time window
        # legitimately produces this, so it is an expected input, not an error.
        if frame.empty and baseline.empty:
            return AnomalyReport(
                kind=self.kind,
                description=self.description,
                method=method,
                threshold=None,
                as_of=as_of,
                considered=0,
            )

        # Unresolved tickets have no resolution time. Dropping them is what
        # makes the quartiles describe actual resolution behaviour; treating a
        # missing value as zero would pull every quartile downwards.
        resolved = frame[frame["resolution_time_hrs"].notna()] if not frame.empty else frame
        baseline_resolved = (
            baseline[baseline["resolution_time_hrs"].notna()]
            if not baseline.empty
            else baseline
        )

        if len(baseline_resolved) < MIN_SAMPLE_FOR_QUARTILES:
            logger.debug(
                "Too few resolved tickets in the baseline (%d) to compute a fence",
                len(baseline_resolved),
            )
            return AnomalyReport(
                kind=self.kind,
                description=self.description,
                method=method,
                threshold=None,
                as_of=as_of,
                considered=len(resolved),
            )

        times = baseline_resolved["resolution_time_hrs"]
        first_quartile = float(times.quantile(0.25))
        third_quartile = float(times.quantile(0.75))
        fence = third_quartile + self.multiplier * (third_quartile - first_quartile)

        flagged = resolved[resolved["resolution_time_hrs"] > fence]

        anomalies = [
            _row_to_anomaly(
                row,
                kind=self.kind,
                # The fence and its parts are stated to two decimals - the
                # precision of the reported threshold. At one decimal, 48.15
                # printed as "48.1" (it is stored as 48.1499...), so each row
                # contradicted its own threshold column.
                reason=(
                    f"Resolved in {row['resolution_time_hrs']:.1f}h, above the "
                    f"{fence:.2f}h outlier threshold "
                    f"(Q3 {third_quartile:.2f}h + {self.multiplier} x IQR "
                    f"{third_quartile - first_quartile:.2f}h)"
                ),
                value=row["resolution_time_hrs"],
                threshold=fence,
            )
            # Sorted worst-first so the most severe case leads any table or
            # narration, and so a truncated view shows what matters most.
            for _, row in flagged.sort_values(
                "resolution_time_hrs", ascending=False
            ).iterrows()
        ]

        return AnomalyReport(
            kind=self.kind,
            description=self.description,
            method=method,
            threshold=round(fence, 2),
            as_of=as_of,
            considered=len(resolved),
            anomalies=anomalies,
        )


@dataclass(frozen=True)
class SlaBreachDetector:
    """Flags urgent tickets left unresolved beyond an agreed age.

    A business rule, not a statistic - the threshold is agreed by an operator
    rather than derived from the data, and the report says so.

    Ages are measured against the supplied ``as_of`` rather than the wall
    clock. The dataset is a static snapshot ending in March 2024; measuring
    against the real clock would report every outstanding ticket as years
    overdue, which is true but useless.

    Attributes:
        breach_hours: Age in hours beyond which an urgent unresolved ticket
            counts as breached.
        priorities: Priorities the rule applies to.
    """

    kind: str = "sla_breach"
    description: str = (
        "Unresolved High or Critical tickets older than the agreed SLA window"
    )
    breach_hours: int = 24
    priorities: frozenset[str] = URGENT_PRIORITIES

    def detect(
        self,
        frame: pd.DataFrame,
        *,
        as_of: datetime,
        baseline: pd.DataFrame | None = None,
    ) -> AnomalyReport:
        """Flag urgent unresolved tickets older than the SLA window.

        Args:
            frame: Tickets to evaluate.
            as_of: Reference time against which ticket age is measured.
            baseline: Accepted for protocol compatibility and deliberately
                unused - this threshold is an agreed business rule, not a
                property of the data, so no sample can inform it.

        Returns:
            The detector's findings.
        """
        method = (
            f"Unresolved ({'/'.join(sorted(UNRESOLVED_STATUSES))}) and "
            f"{'/'.join(sorted(self.priorities))} priority, "
            f"older than {self.breach_hours}h as of {as_of:%Y-%m-%d %H:%M}"
        )

        # An empty DataFrame carries no columns, so column access would raise
        # KeyError rather than simply yielding nothing.
        eligible = (
            frame[
                frame["status"].isin(UNRESOLVED_STATUSES)
                & frame["priority"].isin(self.priorities)
            ]
            if not frame.empty
            else frame
        )

        if eligible.empty:
            return AnomalyReport(
                kind=self.kind,
                description=self.description,
                method=method,
                threshold=float(self.breach_hours),
                as_of=as_of,
                considered=0,
            )

        # Age is attached as a real column before filtering, rather than kept
        # in a parallel Series. Assigning a non-empty Series onto an already
        # empty selection makes pandas align on the Series' index and
        # materialise a phantom all-NaN row - which then fails on any string
        # operation. Carrying the value in the frame avoids that entirely.
        measured = eligible.copy()
        measured["_age_hours"] = (
            as_of - measured["created_at"]
        ).dt.total_seconds() / 3600.0

        breached = measured[measured["_age_hours"] > self.breach_hours]

        anomalies = [
            _row_to_anomaly(
                row,
                kind=self.kind,
                reason=(
                    f"{row['priority']} priority, still {row['status'].lower()} "
                    f"after {row['_age_hours']:.1f}h "
                    f"(SLA {self.breach_hours}h)"
                ),
                value=row["_age_hours"],
                threshold=float(self.breach_hours),
            )
            # Oldest first: the longest-overdue ticket is the most urgent.
            for _, row in breached.sort_values(
                "_age_hours", ascending=False
            ).iterrows()
        ]

        return AnomalyReport(
            kind=self.kind,
            description=self.description,
            method=method,
            threshold=float(self.breach_hours),
            as_of=as_of,
            considered=len(eligible),
            anomalies=anomalies,
        )


class UnknownDetectorError(KeyError):
    """Raised when a caller names a detector that is not registered.

    A dedicated type so callers can catch *this* failure precisely. Catching
    ``KeyError`` instead also caught every KeyError raised inside a detector -
    a missing column, a bug - and reported it to the caller as "unknown
    detector", disguising a server fault as a client mistake. It subclasses
    ``KeyError`` because an unknown name is still a failed lookup.
    """

    def __str__(self) -> str:
        """Return the message itself.

        ``KeyError`` renders its argument with repr quotes, which would reach
        the caller as a message wrapped in stray apostrophes.

        Returns:
            The explanation, unquoted.
        """
        return str(self.args[0]) if self.args else "Unknown anomaly detector."


# The registry. Keyed by ``kind`` so callers - including the language model's
# tool arguments - select a detector by a stable string rather than by import.
DETECTORS: dict[str, AnomalyDetector] = {}


def register_detector(detector: AnomalyDetector) -> None:
    """Add a detector to the registry.

    Args:
        detector: Any object satisfying :class:`AnomalyDetector`.

    Raises:
        ValueError: If a detector with the same ``kind`` is already registered.
            Silently replacing one would make the active behaviour depend on
            import order.
    """
    if detector.kind in DETECTORS:
        raise ValueError(f"A detector named {detector.kind!r} is already registered")
    DETECTORS[detector.kind] = detector


register_detector(ResolutionTimeOutlierDetector(multiplier=settings.iqr_multiplier))
register_detector(SlaBreachDetector(breach_hours=settings.sla_breach_hours))


def load_frame(db_path: Path) -> pd.DataFrame:
    """Read the ticket table into a DataFrame.

    Separated from the detectors so they remain pure functions over a
    DataFrame, testable against hand-built fixtures with no database involved.

    Args:
        db_path: Path to a database built by :func:`app.data.build_database`.

    Returns:
        Every ticket, with ``created_at`` parsed to real timestamps rather than
        left as the text SQLite stores.
    """
    with read_only_connection(db_path) as connection:
        frame = pd.read_sql_query(f"SELECT * FROM {TABLE_NAME}", connection)

    frame["created_at"] = pd.to_datetime(frame["created_at"])
    return frame


def apply_window(
    frame: pd.DataFrame, *, as_of: datetime, window_days: int | None
) -> pd.DataFrame:
    """Restrict a frame to tickets raised within a recent window.

    Supports questions such as "any anomalies this week", which resolve
    relative to ``as_of`` rather than the wall clock.

    Args:
        frame: Tickets to filter.
        as_of: The reference "now".
        window_days: Length of the window in days. ``None`` means no filtering.

    Returns:
        The filtered frame, or the original when ``window_days`` is ``None``.

    Raises:
        ValueError: If ``window_days`` is zero or negative, which would
            silently yield an empty result that looks like "no anomalies".
    """
    if window_days is None:
        return frame

    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")

    cutoff = as_of - timedelta(days=window_days)
    # Bounded above as well as below. "The last 7 days" ends at as_of; a
    # ticket raised after it is not in the window. Ingestion already drops such
    # tickets when AS_OF is pinned, so this is a second, local guarantee
    # rather than the only one.
    return frame[(frame["created_at"] >= cutoff) & (frame["created_at"] <= as_of)]


def detect_anomalies(
    frame: pd.DataFrame,
    *,
    as_of: datetime,
    kinds: list[str] | None = None,
    window_days: int | None = None,
) -> list[AnomalyReport]:
    """Run the requested detectors and return their reports.

    Args:
        frame: Every ticket under consideration.
        as_of: Reference time for age calculations and windowing.
        kinds: Detector identifiers to run. ``None`` runs all registered
            detectors.
        window_days: Restrict to tickets raised in the last N days before
            ``as_of``. ``None`` considers the full history.

    Returns:
        One report per detector, in the order requested.

    Raises:
        UnknownDetectorError: If a requested detector is not registered. The
            message lists the valid identifiers, because this argument may
            originate from a language model's tool call and the error is fed
            back to it.
        ValueError: If ``window_days`` is not positive.
    """
    selected = kinds if kinds is not None else list(DETECTORS)

    unknown = [kind for kind in selected if kind not in DETECTORS]
    if unknown:
        raise UnknownDetectorError(
            f"Unknown anomaly detector(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(DETECTORS))}"
        )

    windowed = apply_window(frame, as_of=as_of, window_days=window_days)

    # The unwindowed frame is passed as the baseline so statistical detectors
    # derive their thresholds from all available history, while judging only
    # the tickets inside the requested window.
    reports = [
        DETECTORS[kind].detect(windowed, as_of=as_of, baseline=frame)
        for kind in selected
    ]

    for report in reports:
        # Each detector's decision: what it applied, to how many tickets, and
        # what it found - enough to see why a ticket was or was not flagged.
        logger.debug(
            "%s: flagged %d of %d considered (threshold %s; %s; window %s)",
            report.kind,
            report.count,
            report.considered,
            report.threshold,
            report.method,
            f"{window_days} days" if window_days else "all history",
        )

    return reports
