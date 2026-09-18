"""Tests for :mod:`app.anomalies`, the deterministic detection engine.

Two kinds of test live here, and the distinction matters.

**Unit tests over hand-built frames.** The detectors are pure functions of a
DataFrame, so their arithmetic is verified against fixtures small enough to
compute by hand. ``[10, 20, 30, 40, 200]`` has quartiles 20 and 40, so the
fence is ``40 + 1.5 * 20 = 70`` and exactly one value exceeds it. Asserting
against numbers derived by the code under test would prove only that it is
self-consistent.

**Gate tests over the real dataset.** The assessment makes specific claims -
a fence of 48.15, 21 outliers, 80 SLA breaches, 6 outliers in the final week.
Those are claims about the shipped file, so they are checked against it.

The most valuable test in this module is
:func:`test_threshold_is_unchanged_by_windowing`. It pins a real defect: the
first implementation recomputed quartiles inside the requested window, so one
quiet week of 33 tickets pushed the fence from 48h to 80h and would have
quietly excused a 60-hour resolution. A threshold describes normal behaviour;
a single week is too small a sample to define it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.anomalies import (
    DETECTORS,
    Anomaly,
    AnomalyReport,
    ResolutionTimeOutlierDetector,
    SlaBreachDetector,
    apply_window,
    detect_anomalies,
    load_frame,
    register_detector,
)
from app.data import Database

# A fixed reference time, so no test depends on the wall clock.
AS_OF = datetime(2024, 3, 30, 18, 6)


@pytest.fixture
def make_frame() -> Callable[..., pd.DataFrame]:
    """Return a factory building a ticket DataFrame from partial rows.

    Each test supplies only the fields it cares about; everything else takes a
    sensible default, so what is under test stays visible instead of being
    buried in ten restated columns.

    Returns:
        A callable accepting any number of dicts and returning a DataFrame with
        ``created_at`` already parsed to timestamps.
    """

    def _template(index: int) -> dict[str, object]:
        return {
            "ticket_id": f"TKT-{index:03d}",
            "created_at": AS_OF - timedelta(days=10),
            "category": "Billing",
            "priority": "Medium",
            "status": "Resolved",
            "response_time_hrs": 1.0,
            "resolution_time_hrs": 5.0,
            "agent_id": "AGT-01",
            "customer_rating": 4,
            "issue_summary": "Test ticket",
        }

    def _make(*rows: dict[str, object]) -> pd.DataFrame:
        records = []
        for index, overrides in enumerate(rows, start=1):
            record = _template(index)
            record.update(overrides)
            records.append(record)

        # An empty frame is still given its columns, because that is what the
        # real loader returns: a SQL result with no rows still has a schema.
        # A column-less DataFrame would be an input the system never produces.
        frame = pd.DataFrame(records, columns=list(_template(0)))
        frame["created_at"] = pd.to_datetime(frame["created_at"])
        return frame

    return _make


@pytest.fixture
def resolution_times(make_frame: Callable[..., pd.DataFrame]) -> Callable[..., pd.DataFrame]:
    """Return a factory building resolved tickets with given resolution times.

    Args:
        make_frame: The general row factory.

    Returns:
        A callable taking resolution times and returning a matching frame.
    """

    def _make(*times: float | None) -> pd.DataFrame:
        return make_frame(
            *(
                {
                    "resolution_time_hrs": value,
                    "status": "Resolved" if value is not None else "Open",
                }
                for value in times
            )
        )

    return _make


# ---------------------------------------------------------------------------
# Resolution-time outliers: arithmetic verified by hand
# ---------------------------------------------------------------------------


def test_fence_is_computed_from_quartiles(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """The fence matches a hand-computed Tukey upper bound.

    For ``[10, 20, 30, 40, 200]`` pandas gives Q1 = 20 and Q3 = 40, so the
    fence is ``40 + 1.5 * (40 - 20) = 70``.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(10, 20, 30, 40, 200), as_of=AS_OF
    )

    assert report.threshold == 70.0
    assert report.count == 1
    assert report.anomalies[0].value == 200.0


def test_only_slow_resolutions_are_flagged(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """A very fast resolution is not an anomaly.

    Only the upper fence is applied. A ticket resolved unusually quickly is not
    an operational problem, so flagging it would be noise in a report meant to
    direct attention.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(1, 20, 30, 40, 50), as_of=AS_OF
    )

    assert report.count == 0


def test_unresolved_tickets_are_excluded_from_quartiles(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """Null resolution times do not participate in the statistics.

    Were they treated as zero, every quartile would be dragged down and the
    fence with them, flagging ordinary tickets as outliers.

    Args:
        resolution_times: Factory building resolved and unresolved tickets.
    """
    with_nulls = ResolutionTimeOutlierDetector().detect(
        resolution_times(10, 20, 30, 40, 200, None, None, None), as_of=AS_OF
    )
    without_nulls = ResolutionTimeOutlierDetector().detect(
        resolution_times(10, 20, 30, 40, 200), as_of=AS_OF
    )

    assert with_nulls.threshold == without_nulls.threshold
    assert with_nulls.considered == 5  # the nulls are not "considered"


def test_multiplier_widens_the_fence(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """A larger multiplier flags fewer tickets.

    The multiplier is configurable so an operator can tune sensitivity without
    a code change.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    frame = resolution_times(10, 20, 30, 40, 200)

    strict = ResolutionTimeOutlierDetector(multiplier=1.5).detect(frame, as_of=AS_OF)
    lenient = ResolutionTimeOutlierDetector(multiplier=10.0).detect(frame, as_of=AS_OF)

    assert strict.count == 1
    assert lenient.count == 0
    assert lenient.threshold > strict.threshold


def test_anomalies_are_ordered_worst_first(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """The slowest resolution leads the list.

    Any truncated view - a UI table, or the capped rows sent to the narration
    call - then shows the most severe cases rather than an arbitrary subset.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(10, 20, 30, 40, 150, 300, 200), as_of=AS_OF
    )

    values = [anomaly.value for anomaly in report.anomalies]
    assert values == sorted(values, reverse=True)


def test_reason_states_the_measurement_and_the_threshold(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """Each anomaly explains itself without reference to the detector.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(10, 20, 30, 40, 200), as_of=AS_OF
    )
    reason = report.anomalies[0].reason

    assert "200.0h" in reason
    # Two decimals, the precision of the reported threshold - see
    # test_reason_states_the_same_threshold_as_the_report.
    assert "70.00h" in reason


# ---------------------------------------------------------------------------
# Resolution-time outliers: degenerate inputs
# ---------------------------------------------------------------------------


def test_too_few_samples_yields_no_threshold(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """Quartiles are not invented from three data points.

    With so small a sample the "interquartile range" spans nearly the whole
    set and the fence is arbitrary. Reporting ``None`` is honest; reporting a
    number would not be.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(10, 20, 1000), as_of=AS_OF
    )

    assert report.threshold is None
    assert report.count == 0


def test_empty_frame_is_handled(make_frame: Callable[..., pd.DataFrame]) -> None:
    """A frame with no rows produces an empty report rather than an error.

    Reachable in normal use: a narrow time window can legitimately contain no
    tickets at all.

    Args:
        make_frame: The general row factory.
    """
    report = ResolutionTimeOutlierDetector().detect(make_frame(), as_of=AS_OF)

    assert report.count == 0
    assert report.considered == 0
    assert report.threshold is None


def test_all_unresolved_yields_no_threshold(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """A frame where nothing has been resolved produces no statistics.

    Args:
        resolution_times: Factory building unresolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(None, None, None, None, None), as_of=AS_OF
    )

    assert report.threshold is None
    assert report.count == 0


def test_identical_values_produce_no_anomalies(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """When every resolution takes the same time, none is anomalous.

    The interquartile range collapses to zero, so the fence equals Q3 and
    nothing exceeds it - which is the correct answer, not a division hazard.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(12, 12, 12, 12, 12), as_of=AS_OF
    )

    assert report.count == 0
    assert report.threshold == 12.0


def test_single_outlier_among_identical_values_is_still_caught(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """A zero interquartile range does not suppress a genuine outlier.

    With ``[12, 12, 12, 12, 500]`` the fence sits at 12, and the 500-hour
    ticket is correctly flagged. A guard that skipped detection whenever the
    range was zero would miss exactly this case.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(12, 12, 12, 12, 500), as_of=AS_OF
    )

    assert report.count == 1
    assert report.anomalies[0].value == 500.0


# ---------------------------------------------------------------------------
# SLA breaches
# ---------------------------------------------------------------------------


def test_urgent_unresolved_ticket_past_the_window_is_flagged(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """An old, unresolved, high-priority ticket breaches the SLA.

    Args:
        make_frame: The general row factory.
    """
    report = SlaBreachDetector().detect(
        make_frame(
            {
                "status": "Open",
                "priority": "High",
                "created_at": AS_OF - timedelta(hours=25),
                "resolution_time_hrs": None,
            }
        ),
        as_of=AS_OF,
    )

    assert report.count == 1


@pytest.mark.parametrize(
    ("age_hours", "expected"),
    [
        pytest.param(23.9, 0, id="just inside the window"),
        pytest.param(24.0, 0, id="exactly at the boundary"),
        pytest.param(24.1, 1, id="just past the boundary"),
    ],
)
def test_sla_boundary_is_exclusive(
    age_hours: float, expected: int, make_frame: Callable[..., pd.DataFrame]
) -> None:
    """A ticket exactly at the SLA age has not yet breached it.

    "Older than 24 hours" is a strict comparison. Off-by-one at a boundary is
    the classic defect in rules like this, so the boundary itself is pinned.

    Args:
        age_hours: Age of the ticket under test.
        expected: Number of breaches expected.
        make_frame: The general row factory.
    """
    report = SlaBreachDetector().detect(
        make_frame(
            {
                "status": "Open",
                "priority": "Critical",
                "created_at": AS_OF - timedelta(hours=age_hours),
                "resolution_time_hrs": None,
            }
        ),
        as_of=AS_OF,
    )

    assert report.count == expected


@pytest.mark.parametrize(
    ("status", "priority", "expected"),
    [
        pytest.param("Open", "Critical", 1, id="open critical breaches"),
        pytest.param("Escalated", "High", 1, id="escalated counts as unresolved"),
        pytest.param("Resolved", "Critical", 0, id="resolved never breaches"),
        pytest.param("Open", "Low", 0, id="low priority is out of scope"),
        pytest.param("Open", "Medium", 0, id="medium priority is out of scope"),
    ],
)
def test_sla_applies_only_to_urgent_unresolved_tickets(
    status: str, priority: str, expected: int, make_frame: Callable[..., pd.DataFrame]
) -> None:
    """Only unresolved High and Critical tickets are in scope.

    Escalated is treated as unresolved, following the shipped data - every
    Escalated row has no resolution time, despite the brief's schema preview
    showing otherwise.

    Args:
        status: Ticket status under test.
        priority: Ticket priority under test.
        expected: Number of breaches expected.
        make_frame: The general row factory.
    """
    report = SlaBreachDetector().detect(
        make_frame(
            {
                "status": status,
                "priority": priority,
                "created_at": AS_OF - timedelta(days=5),
                "resolution_time_hrs": None if status != "Resolved" else 3.0,
            }
        ),
        as_of=AS_OF,
    )

    assert report.count == expected


def test_age_is_measured_against_as_of_not_the_wall_clock(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """Moving the reference time changes which tickets have breached.

    The dataset is a static 2024 snapshot. Measuring against the real clock
    would report every outstanding ticket as years overdue - accurate, and
    useless. This is the same anchoring decision that makes "this week"
    questions meaningful.

    Args:
        make_frame: The general row factory.
    """
    frame = make_frame(
        {
            "status": "Open",
            "priority": "High",
            "created_at": AS_OF - timedelta(hours=10),
            "resolution_time_hrs": None,
        }
    )

    assert SlaBreachDetector().detect(frame, as_of=AS_OF).count == 0

    later = SlaBreachDetector().detect(frame, as_of=AS_OF + timedelta(hours=20))
    assert later.count == 1


def test_sla_reports_its_threshold_even_with_no_breaches(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """An empty report still states what was looked for.

    "No breaches" is only trustworthy alongside the rule that was applied.

    Args:
        make_frame: The general row factory.
    """
    report = SlaBreachDetector().detect(
        make_frame({"status": "Resolved", "priority": "Low"}), as_of=AS_OF
    )

    assert report.count == 0
    assert report.threshold == 24.0
    assert "24h" in report.method


def test_configurable_sla_window(make_frame: Callable[..., pd.DataFrame]) -> None:
    """The breach threshold can be tightened without a code change.

    Args:
        make_frame: The general row factory.
    """
    frame = make_frame(
        {
            "status": "Open",
            "priority": "High",
            "created_at": AS_OF - timedelta(hours=10),
            "resolution_time_hrs": None,
        }
    )

    assert SlaBreachDetector(breach_hours=24).detect(frame, as_of=AS_OF).count == 0
    assert SlaBreachDetector(breach_hours=4).detect(frame, as_of=AS_OF).count == 1


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------


def test_window_keeps_only_recent_tickets(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """Windowing selects tickets raised within N days of the reference time.

    Args:
        make_frame: The general row factory.
    """
    frame = make_frame(
        {"created_at": AS_OF - timedelta(days=2)},
        {"created_at": AS_OF - timedelta(days=40)},
    )

    assert len(apply_window(frame, as_of=AS_OF, window_days=7)) == 1


def test_window_excludes_tickets_after_the_reference_time(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """A window ends at the reference time, not at the end of the data.

    Only reachable when AS_OF is pinned before the last ticket. "The last 7
    days" previously had a lower bound and no upper one, so tickets raised
    after the reference time were counted as recent.

    Args:
        make_frame: The general row factory.
    """
    frame = make_frame(
        {"created_at": AS_OF - timedelta(days=2)},
        {"created_at": AS_OF + timedelta(days=2)},
    )

    assert len(apply_window(frame, as_of=AS_OF, window_days=7)) == 1


def test_no_window_returns_every_ticket(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """``window_days=None`` applies no filtering.

    Args:
        make_frame: The general row factory.
    """
    frame = make_frame({}, {}, {})

    assert len(apply_window(frame, as_of=AS_OF, window_days=None)) == 3


@pytest.mark.parametrize(
    "window_days",
    [pytest.param(0, id="zero days"), pytest.param(-5, id="negative days")],
)
def test_invalid_window_is_rejected(
    window_days: int, make_frame: Callable[..., pd.DataFrame]
) -> None:
    """A non-positive window raises rather than returning nothing.

    Silently returning an empty frame would render as "no anomalies found",
    which is indistinguishable from a genuine all-clear.

    Args:
        window_days: An invalid window length.
        make_frame: The general row factory.
    """
    with pytest.raises(ValueError, match="positive"):
        apply_window(make_frame({}), as_of=AS_OF, window_days=window_days)


# ---------------------------------------------------------------------------
# Registry and dispatch
# ---------------------------------------------------------------------------


def test_both_detectors_are_registered() -> None:
    """The two shipped detectors are available by name."""
    assert set(DETECTORS) == {"resolution_time_outlier", "sla_breach"}


def test_all_detectors_run_by_default(make_frame: Callable[..., pd.DataFrame]) -> None:
    """Omitting ``kinds`` runs every registered detector.

    Args:
        make_frame: The general row factory.
    """
    reports = detect_anomalies(make_frame({}), as_of=AS_OF)

    assert len(reports) == len(DETECTORS)


def test_specific_detector_can_be_selected(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """A caller may request a single detector by name.

    Args:
        make_frame: The general row factory.
    """
    reports = detect_anomalies(make_frame({}), as_of=AS_OF, kinds=["sla_breach"])

    assert [report.kind for report in reports] == ["sla_breach"]


def test_unknown_detector_lists_the_valid_names(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """Requesting a detector that does not exist names the ones that do.

    This argument can originate from a language model's tool call, and the
    error text is fed back to it - so it has to be useful enough to act on.

    Args:
        make_frame: The general row factory.
    """
    with pytest.raises(KeyError, match="sla_breach"):
        detect_anomalies(make_frame({}), as_of=AS_OF, kinds=["does_not_exist"])


def test_duplicate_registration_is_refused() -> None:
    """Registering a second detector under an existing name fails.

    Replacing it silently would make the system's behaviour depend on module
    import order, which is invisible and painful to debug.
    """
    with pytest.raises(ValueError, match="already registered"):
        register_detector(SlaBreachDetector())


def test_detectors_satisfy_the_protocol() -> None:
    """Both shipped detectors conform to the shared interface.

    Substitutability is what lets :func:`detect_anomalies` invoke them without
    knowing which it holds.
    """
    from app.anomalies import AnomalyDetector

    for detector in DETECTORS.values():
        assert isinstance(detector, AnomalyDetector)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_report_serialises_for_an_api_response(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """A report converts to plain JSON-safe types.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(10, 20, 30, 40, 200), as_of=AS_OF
    )
    payload = report.to_dict()

    assert payload["kind"] == "resolution_time_outlier"
    assert payload["count"] == 1
    assert isinstance(payload["as_of"], str)
    assert isinstance(payload["anomalies"][0]["created_at"], str)


# ---------------------------------------------------------------------------
# Gates against the shipped dataset
# ---------------------------------------------------------------------------


def test_shipped_dataset_outlier_gate(real_database: Database) -> None:
    """The real data yields a 48.15-hour fence and 21 outliers.

    Args:
        real_database: Database built from the shipped CSV.
    """
    frame = load_frame(real_database.path)
    report = detect_anomalies(
        frame, as_of=real_database.as_of, kinds=["resolution_time_outlier"]
    )[0]

    assert report.threshold == 48.15
    assert report.considered == 327
    assert report.count == 21


def test_reason_states_the_same_threshold_as_the_report(real_database: Database) -> None:
    """Each flagged ticket's reason quotes the threshold its row reports.

    At one decimal place 48.15 printed as "48.1" - it is stored as 48.1499... -
    so every row read "above the 48.1h threshold" beside a threshold column of
    48.15, and Q3 read 22.9 where the true value is 22.95.

    Args:
        real_database: Database built from the shipped CSV.
    """
    report = detect_anomalies(
        load_frame(real_database.path),
        as_of=real_database.as_of,
        kinds=["resolution_time_outlier"],
    )[0]

    worst = report.anomalies[0]
    assert worst.threshold == 48.15
    assert worst.reason == (
        "Resolved in 119.7h, above the 48.15h outlier threshold "
        "(Q3 22.95h + 1.5 x IQR 16.80h)"
    )


def test_shipped_dataset_sla_gate(real_database: Database) -> None:
    """The real data yields 80 SLA breaches.

    Args:
        real_database: Database built from the shipped CSV.
    """
    frame = load_frame(real_database.path)
    report = detect_anomalies(frame, as_of=real_database.as_of, kinds=["sla_breach"])[0]

    assert report.count == 80


def test_threshold_is_unchanged_by_windowing(real_database: Database) -> None:
    """The fence is derived from all history, whatever window is requested.

    Regression cover for a real defect. The first implementation recomputed
    quartiles inside the window, so one quiet week of 33 tickets moved the
    fence from 48.15h to 80.45h - quietly excusing a 60-hour resolution that
    is plainly abnormal against any historical baseline. A threshold describes
    normal behaviour; a single week cannot define it.

    Args:
        real_database: Database built from the shipped CSV.
    """
    frame = load_frame(real_database.path)

    thresholds = {
        window: detect_anomalies(
            frame,
            as_of=real_database.as_of,
            kinds=["resolution_time_outlier"],
            window_days=window,
        )[0].threshold
        for window in (None, 7, 30, 90)
    }

    assert set(thresholds.values()) == {48.15}


def test_shipped_dataset_recent_window_gate(real_database: Database) -> None:
    """Six outliers fall in the final week of the dataset.

    Args:
        real_database: Database built from the shipped CSV.
    """
    frame = load_frame(real_database.path)
    report = detect_anomalies(
        frame,
        as_of=real_database.as_of,
        kinds=["resolution_time_outlier"],
        window_days=7,
    )[0]

    assert report.count == 6


def test_every_anomaly_carries_a_reason(real_database: Database) -> None:
    """No flagged ticket is reported without a justification.

    Args:
        real_database: Database built from the shipped CSV.
    """
    frame = load_frame(real_database.path)

    for report in detect_anomalies(frame, as_of=real_database.as_of):
        for anomaly in report.anomalies:
            assert anomaly.reason.strip()
            assert anomaly.ticket_id.startswith("TKT-")


def test_debug_log_shows_each_detector_decision(
    real_database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """At DEBUG, every detector logs what it applied and what it found.

    Args:
        real_database: Database built from the shipped dataset.
        caplog: pytest's log capture.
    """
    import logging

    caplog.set_level(logging.DEBUG, logger="app")

    detect_anomalies(load_frame(real_database.path), as_of=AS_OF)

    assert "resolution_time_outlier: flagged 21 of 327 considered (threshold 48.15" in caplog.text
    assert "sla_breach: flagged 80 of" in caplog.text
    assert "window all history" in caplog.text


# ---------------------------------------------------------------------------
# The IQR rationale
# ---------------------------------------------------------------------------


def test_rationale_states_the_skew_and_both_flag_counts(real_database: Database) -> None:
    """The report justifies the IQR with figures computed from the real data.

    These are the benchmark's expected figures for "why the IQR rather than a
    standard deviation?": mean 19.16 against median 12.00, and 7 tickets
    flagged by z > 3 against 21 by the fence.

    Args:
        real_database: Database built from the shipped dataset.
    """
    (report,) = detect_anomalies(
        load_frame(real_database.path),
        as_of=real_database.as_of,
        kinds=["resolution_time_outlier"],
    )

    assert report.rationale is not None
    assert "mean 19.16h" in report.rationale
    assert "median 12.00h" in report.rationale
    assert "flags only 7 tickets" in report.rationale
    assert f"flags {report.count}." in report.rationale
    assert report.count == 21
    assert report.to_dict()["rationale"] == report.rationale


def test_rationale_survives_a_constant_column(
    resolution_times: Callable[..., pd.DataFrame],
) -> None:
    """Identical resolution times have no spread, so no z-score is defined.

    Args:
        resolution_times: Factory building resolved tickets.
    """
    report = ResolutionTimeOutlierDetector().detect(
        resolution_times(5.0, 5.0, 5.0, 5.0), as_of=AS_OF
    )

    assert report.rationale is not None
    assert "flags only 0 tickets" in report.rationale


def test_business_rule_carries_no_rationale(
    make_frame: Callable[..., pd.DataFrame],
) -> None:
    """The SLA rule is agreed, not chosen from the data, so it justifies nothing.

    Args:
        make_frame: The general row factory.
    """
    report = SlaBreachDetector().detect(make_frame({"status": "Open"}), as_of=AS_OF)

    assert report.rationale is None
