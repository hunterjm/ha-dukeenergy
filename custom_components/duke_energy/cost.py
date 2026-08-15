"""Cost calculation and persistence for Duke Energy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.helpers.storage import Store

from .const import (
    CONF_COST_TRACKING,
    CONF_EFFECTIVE_DATE,
    CONF_ENABLED,
    CONF_RATE,
    CONF_RATES,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_STORAGE_VERSION = 1

type UsageData = Mapping[datetime, Mapping[str, float | int]]


@dataclass(frozen=True, slots=True)
class CostStatistic:
    """One cumulative cost statistic."""

    start: datetime
    interval_cost: Decimal
    cumulative_cost: Decimal


@dataclass(frozen=True, slots=True)
class CostStatisticsBatch:
    """A pending range of cumulative cost statistics."""

    dirty_from: datetime
    statistics: tuple[CostStatistic, ...]


class RateProvider(Protocol):
    """Provide usage rates by service and effective timestamp."""

    def enabled(self, service_type: str) -> bool:
        """Return whether cost tracking is enabled."""

    def has_history(self, service_type: str) -> bool:
        """Return whether rate history exists."""

    def rate_at(self, service_type: str, timestamp: datetime) -> Decimal | None:
        """Return the rate effective at a timestamp."""


class ManualRateProvider:
    """Provide manually configured rates from config-entry options."""

    def __init__(self, options: Mapping[str, Any]) -> None:
        """Initialize the manual rate provider."""
        self._configuration = options.get(CONF_COST_TRACKING, {})

    def enabled(self, service_type: str) -> bool:
        """Return whether cost tracking is enabled."""
        return bool(self._configuration.get(service_type, {}).get(CONF_ENABLED, False))

    def has_history(self, service_type: str) -> bool:
        """Return whether rate history exists."""
        return bool(self._configuration.get(service_type, {}).get(CONF_RATES, []))

    def earliest_rate_date(self, service_type: str) -> date | None:
        """Return the earliest effective date with a non-null rate."""
        dates = [
            date.fromisoformat(period[CONF_EFFECTIVE_DATE])
            for period in self._configuration.get(service_type, {}).get(CONF_RATES, [])
            if period.get(CONF_RATE) is not None
        ]
        return min(dates) if dates else None

    def rate_at(self, service_type: str, timestamp: datetime) -> Decimal | None:
        """Return the rate effective at a timestamp."""
        rates = self._configuration.get(service_type, {}).get(CONF_RATES, [])
        usage_date = timestamp.date().isoformat()
        for period in sorted(
            rates,
            key=lambda item: item[CONF_EFFECTIVE_DATE],
            reverse=True,
        ):
            if period[CONF_EFFECTIVE_DATE] > usage_date:
                continue
            if period[CONF_RATE] is None:
                return None
            return Decimal(period[CONF_RATE])
        return None


class CostLedger:
    """Persist cumulative costs and processed usage intervals."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the cost ledger."""
        self._store = Store[dict[str, Any]](
            hass, _STORAGE_VERSION, f"{DOMAIN}.costs.{entry_id}"
        )
        self._data: dict[str, Any] = {"meters": {}}
        self._dirty = False

    async def async_load(self) -> None:
        """Load persisted cost data."""
        if stored := await self._store.async_load():
            self._data = stored
        for meter in self._data["meters"].values():
            if "statistics_dirty_from" not in meter and (
                intervals := meter.get("intervals")
            ):
                meter["statistics_dirty_from"] = min(
                    datetime.fromisoformat(interval_key) for interval_key in intervals
                ).isoformat()
                self._dirty = True

    def total(self, meter_id: str) -> Decimal:
        """Return the persisted total for a meter."""
        meter = self._data["meters"].get(meter_id, {})
        return Decimal(meter.get("total", "0"))

    def history_covered_from(self, meter_id: str) -> date | None:
        """Return the start of verified contiguous cost-history coverage."""
        meter = self._data["meters"].get(meter_id, {})
        if not (covered_from := meter.get("history_covered_from")):
            return None
        return date.fromisoformat(covered_from)

    def mark_history_covered_from(self, meter_id: str, covered_from: date) -> None:
        """Record an earlier verified contiguous history boundary."""
        meter = self._meter(meter_id)
        current_value = meter.get("history_covered_from")
        if current_value is not None:
            current = date.fromisoformat(current_value)
            if current <= covered_from:
                return
        meter["history_covered_from"] = covered_from.isoformat()
        self._dirty = True

    def reprice(
        self,
        meter_id: str,
        service_type: str,
        rate_provider: RateProvider,
    ) -> Decimal:
        """Recalculate stored intervals using effective-dated rates."""
        meter = self._meter(meter_id)
        total = Decimal(meter["total"])
        for interval_key, interval in meter["intervals"].items():
            timestamp = datetime.fromisoformat(interval_key)
            usage = Decimal(interval["usage"])
            new_cost = self._calculate_cost(
                service_type, timestamp, usage, rate_provider
            )
            previous_cost = self._decimal_or_zero(interval.get("cost"))
            if previous_cost == self._decimal_or_zero(new_cost):
                continue
            total += self._decimal_or_zero(new_cost) - previous_cost
            interval["cost"] = str(new_cost) if new_cost is not None else None
            self._dirty = True
            self._mark_statistics_dirty(meter_id, timestamp)
        meter["total"] = str(total)
        return total

    def update(
        self,
        meter_id: str,
        service_type: str,
        usage: UsageData,
        rate_provider: RateProvider,
    ) -> Decimal:
        """Insert or correct fetched usage intervals."""
        meter = self._meter(meter_id)
        intervals = meter["intervals"]
        total = Decimal(meter["total"])
        for timestamp, data in usage.items():
            interval_key = timestamp.isoformat()
            usage_value = Decimal(str(data["energy"]))
            new_cost = self._calculate_cost(
                service_type, timestamp, usage_value, rate_provider
            )
            previous = intervals.get(interval_key)
            previous_cost = self._decimal_or_zero(
                previous.get("cost") if previous else None
            )
            if (
                previous is not None
                and previous["usage"] == str(usage_value)
                and previous_cost == self._decimal_or_zero(new_cost)
            ):
                continue
            total += self._decimal_or_zero(new_cost) - previous_cost
            intervals[interval_key] = {
                "usage": str(usage_value),
                "cost": str(new_cost) if new_cost is not None else None,
            }
            self._dirty = True
            self._mark_statistics_dirty(meter_id, timestamp)
        meter["total"] = str(total)
        return total

    def cost_statistics(self, meter_id: str) -> CostStatisticsBatch | None:
        """Return cumulative cost statistics for the pending dirty range."""
        meter = self._meter(meter_id)
        if not (dirty_from_value := meter.get("statistics_dirty_from")):
            return None
        dirty_from = datetime.fromisoformat(dirty_from_value)
        cumulative_cost = Decimal(0)
        statistics: list[CostStatistic] = []
        for interval_key, interval in sorted(meter["intervals"].items()):
            timestamp = datetime.fromisoformat(interval_key)
            interval_cost = self._decimal_or_zero(interval.get("cost"))
            cumulative_cost += interval_cost
            if timestamp >= dirty_from:
                statistics.append(
                    CostStatistic(
                        start=timestamp,
                        interval_cost=interval_cost,
                        cumulative_cost=cumulative_cost,
                    )
                )
        if not statistics:
            return None
        return CostStatisticsBatch(dirty_from=dirty_from, statistics=tuple(statistics))

    def acknowledge_cost_statistics(
        self, meter_id: str, imported_from: datetime
    ) -> None:
        """Clear a pending range after recorder confirms its import."""
        meter = self._meter(meter_id)
        if meter.get("statistics_dirty_from") != imported_from.isoformat():
            return
        meter.pop("statistics_dirty_from")
        self._dirty = True

    def _mark_statistics_dirty(self, meter_id: str, timestamp: datetime) -> None:
        """Persist the earliest timestamp affected by a cost change."""
        meter = self._meter(meter_id)
        current_value = meter.get("statistics_dirty_from")
        if current_value is not None:
            current = datetime.fromisoformat(current_value)
            if current <= timestamp:
                return
        meter["statistics_dirty_from"] = timestamp.isoformat()
        self._dirty = True

    async def async_save(self) -> None:
        """Persist changed cost data."""
        if not self._dirty:
            return
        await self._store.async_save(self._data)
        self._dirty = False

    def _meter(self, meter_id: str) -> dict[str, Any]:
        """Return or create persisted data for a meter."""
        return self._data["meters"].setdefault(
            meter_id,
            {
                "total": "0",
                "intervals": {},
                "statistics_dirty_from": None,
                "history_covered_from": None,
            },
        )

    @staticmethod
    def _calculate_cost(
        service_type: str,
        timestamp: datetime,
        usage: Decimal,
        rate_provider: RateProvider,
    ) -> Decimal | None:
        """Calculate one interval's cost."""
        if (rate := rate_provider.rate_at(service_type, timestamp)) is None:
            return None
        return usage * rate

    @staticmethod
    def _decimal_or_zero(value: str | Decimal | None) -> Decimal:
        """Convert a persisted optional decimal to Decimal."""
        return Decimal(value) if value is not None else Decimal(0)
