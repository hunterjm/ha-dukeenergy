"""Unit tests for the pure statistic builders in the coordinator.

These exercise the price/consumption math directly, bypassing Home Assistant so
they need no recorder or event loop.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.duke_energy.const import CONF_MONTHLY_CHARGE
from custom_components.duke_energy.coordinator import DukeEnergyCoordinator

NY = ZoneInfo("America/New_York")


def test_build_consumption_rows_cumulative_and_resume() -> None:
    """Consumption sums accumulate, and resuming skips already-recorded hours."""
    energy = {datetime(2025, 7, 1, h): float(h) for h in range(1, 4)}  # 1, 2, 3

    rows = DukeEnergyCoordinator._build_consumption_rows(energy, (0.0, None))
    assert [r["state"] for r in rows] == [1.0, 2.0, 3.0]
    assert [r["sum"] for r in rows] == [1.0, 3.0, 6.0]

    resume = datetime(2025, 7, 1, 1).timestamp()
    rows = DukeEnergyCoordinator._build_consumption_rows(energy, (1.0, resume))
    assert [r["state"] for r in rows] == [2.0, 3.0]
    assert [r["sum"] for r in rows] == [3.0, 6.0]


def test_build_cost_rows_fixed_price() -> None:
    """A flat price yields energy * price cumulative sums."""
    coord = object.__new__(DukeEnergyCoordinator)
    energy = {datetime(2025, 7, 1, h): float(h) for h in range(1, 4)}

    rows = coord._build_cost_rows(energy, (0.0, None), lambda _s: 0.10, {})
    assert [round(r["sum"], 3) for r in rows] == [0.1, 0.3, 0.6]


def test_build_cost_rows_defers_on_missing_price() -> None:
    """Cost stops at the first unpriced hour so the running sum stays contiguous."""
    coord = object.__new__(DukeEnergyCoordinator)
    energy = {datetime(2025, 7, 1, h): 1.0 for h in range(3)}
    prices = {0: 0.10, 1: None, 2: 0.10}

    rows = coord._build_cost_rows(energy, (0.0, None), lambda s: prices[s.hour], {})
    assert len(rows) == 1
    assert rows[0]["start"].hour == 0


def test_build_cost_rows_includes_monthly_charge() -> None:
    """The fixed monthly charge is amortized across the hours of the month."""
    coord = object.__new__(DukeEnergyCoordinator)
    energy = {datetime(2025, 7, 1, 0): 0.0}  # July spans 744 hours

    rows = coord._build_cost_rows(
        energy, (0.0, None), lambda _s: 0.0, {CONF_MONTHLY_CHARGE: 31}
    )
    assert round(rows[0]["state"], 6) == round(31 / 744, 6)


def test_resume_baseline_anchors_to_lookback() -> None:
    """Resume anchors ~30 days before the newest row, not the window start."""
    base = datetime(2025, 6, 1, tzinfo=NY).timestamp()
    rows = [{"start": base + i * 3600, "sum": float(i)} for i in range(40 * 24)]
    resume = rows[-1]["start"]

    total, start = DukeEnergyCoordinator._resume_baseline(rows, resume)
    assert start == resume - 30 * 86400
    assert total == next(r["sum"] for r in rows if r["start"] == start)


def test_resume_baseline_without_history() -> None:
    """No rows or no resume point means start from zero."""
    assert DukeEnergyCoordinator._resume_baseline(None, 123.0) == (0.0, None)
    assert DukeEnergyCoordinator._resume_baseline(
        [{"start": 1.0, "sum": 5.0}], None
    ) == (0.0, None)


def test_hourly_fixed_charge_is_dst_aware() -> None:
    """Amortization uses the real hours in a month, including DST transitions."""
    coord = object.__new__(DukeEnergyCoordinator)
    opts = {CONF_MONTHLY_CHARGE: 30}

    naive_july = coord._hourly_fixed_charge(opts, datetime(2025, 7, 15))
    assert round(naive_july, 8) == round(30 / (31 * 24), 8)

    march = coord._hourly_fixed_charge(opts, datetime(2025, 3, 15, tzinfo=NY))
    assert round(march, 8) == round(30 / 743, 8)  # spring forward: 743 hours

    november = coord._hourly_fixed_charge(opts, datetime(2025, 11, 15, tzinfo=NY))
    assert round(november, 8) == round(30 / 721, 8)  # fall back: 721 hours
