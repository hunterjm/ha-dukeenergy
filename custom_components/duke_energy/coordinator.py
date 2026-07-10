"""Coordinator to handle Duke Energy connections."""

import logging
from bisect import bisect_right
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, cast

from aiodukeenergy import DukeEnergy, DukeEnergyAuthError
from aiohttp import ClientError
from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_PRICE,
    CONF_METERS,
    CONF_MONTHLY_CHARGE,
    CONF_PRICE_ENTITY,
    COST_MODE_ENTITY,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_SUPPORTED_METER_TYPES = ("ELECTRIC",)

# Duke Energy service areas are all in America/New_York.
_TIMEZONE = "America/New_York"
# Re-fetch/recompute this far before a resume point so corrected hours are rewritten.
_LOOKBACK = timedelta(days=30)

type DukeEnergyConfigEntry = ConfigEntry[DukeEnergyCoordinator]


class DukeEnergyCoordinator(DataUpdateCoordinator[None]):
    """Handle inserting statistics."""

    config_entry: DukeEnergyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: DukeEnergy,
        config_entry: DukeEnergyConfigEntry,
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Duke Energy",
            # Data is updated daily on Duke Energy.
            # Refresh every 12h to be at most 12h behind.
            update_interval=timedelta(hours=12),
        )
        self.api = api
        self._statistic_ids: set = set()
        # Supported meters discovered on the last refresh, for the options flow.
        self.meters: dict[str, dict[str, Any]] = {}

        @callback
        def _dummy_listener() -> None:
            pass

        # Force the coordinator to periodically update.
        # Duke Energy does not provide forecast data, so all information is historical.
        # This makes _async_update_data get periodically called to insert statistics.
        self.async_add_listener(_dummy_listener)

        self.config_entry.async_on_unload(self._clear_statistics)

    def _clear_statistics(self) -> None:
        """Clear statistics."""
        get_instance(self.hass).async_clear_statistics(list(self._statistic_ids))

    async def _async_update_data(self) -> None:
        """Insert Duke Energy statistics."""
        try:
            meters: dict[str, dict[str, Any]] = await self.api.get_meters()
        except DukeEnergyAuthError as err:
            raise ConfigEntryAuthFailed from err

        self.meters = {}
        for serial_number, meter in meters.items():
            if (
                not isinstance(meter["serviceType"], str)
                or meter["serviceType"] not in _SUPPORTED_METER_TYPES
            ):
                _LOGGER.debug(
                    "Skipping unsupported meter type %s", meter["serviceType"]
                )
                continue

            self.meters[serial_number] = meter
            await self._async_insert_meter_statistics(serial_number, meter)

    async def _async_insert_meter_statistics(
        self, serial_number: str, meter: dict[str, Any]
    ) -> None:
        """Insert a meter's consumption (from the API) and cost (derived locally)."""
        consumption_id, cost_id = self._meter_statistic_ids(serial_number, meter)
        self._statistic_ids.add(consumption_id)
        await self._async_update_consumption(serial_number, meter)

        meter_options = self._meter_options(serial_number)
        if meter_options.get(CONF_COST_MODE, COST_MODE_NONE) != COST_MODE_NONE:
            self._statistic_ids.add(cost_id)
            await self._async_update_cost(serial_number, meter, meter_options)

    async def async_rebuild_costs(self) -> None:
        """
        Recompute cost from stored consumption after an options change.

        Cost is derived from the consumption statistic, so changing a price is a
        local recompute -- no Duke API calls and no consumption rebuild.
        """
        for serial_number, meter in self.meters.items():
            _, cost_id = self._meter_statistic_ids(serial_number, meter)
            get_instance(self.hass).async_clear_statistics([cost_id])
            meter_options = self._meter_options(serial_number)
            if meter_options.get(CONF_COST_MODE, COST_MODE_NONE) == COST_MODE_NONE:
                self._statistic_ids.discard(cost_id)
                continue
            self._statistic_ids.add(cost_id)
            await self._async_update_cost(
                serial_number, meter, meter_options, full=True
            )

    async def _async_update_consumption(
        self, serial_number: str, meter: dict[str, Any]
    ) -> None:
        """Insert consumption statistics from the Duke API (incremental)."""
        consumption_id, _ = self._meter_statistic_ids(serial_number, meter)
        resume = await self._async_last_stat_start(consumption_id)
        usage = await self._async_get_energy_usage(meter, resume)
        if resume is not None and not usage:
            _LOGGER.debug("No recent usage for %s. Skipping update", consumption_id)
            return

        baseline: tuple[float, float | None] = (0.0, None)
        if resume is not None:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                min(usage.keys()),
                None,
                {consumption_id},
                "hour",
                None,
                {"sum"},
            )
            baseline = self._resume_baseline(stats.get(consumption_id), resume)

        rows = self._build_consumption_rows(
            {start: data["energy"] for start, data in usage.items()}, baseline
        )
        _LOGGER.debug("Adding %s consumption stats for %s", len(rows), consumption_id)
        async_add_external_statistics(
            self.hass, self._energy_metadata(serial_number, meter), rows
        )

    async def _async_update_cost(
        self,
        serial_number: str,
        meter: dict[str, Any],
        meter_options: Mapping[str, Any],
        *,
        full: bool = False,
    ) -> None:
        """
        Insert cost statistics derived from the stored consumption statistic.

        Reads the per-hour energy already recorded for consumption and prices it
        locally, so cost never triggers its own Duke API fetch. ``full`` rebuilds
        the entire history (after an options change); otherwise it resumes.
        """
        consumption_id, cost_id = self._meter_statistic_ids(serial_number, meter)
        resume = None if full else await self._async_last_stat_start(cost_id)
        energy = await self._async_consumption_energy(consumption_id, resume)
        if not energy:
            return

        baseline: tuple[float, float | None] = (0.0, None)
        if resume is not None:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                min(energy),
                None,
                {cost_id},
                "hour",
                None,
                {"sum"},
            )
            baseline = self._resume_baseline(stats.get(cost_id), resume)

        price_at = await self._async_build_price_lookup(
            meter_options, min(energy), max(energy)
        )
        if price_at is None:
            return
        rows = self._build_cost_rows(energy, baseline, price_at, meter_options)
        if not rows:
            return
        _LOGGER.debug("Adding %s cost stats for %s", len(rows), cost_id)
        async_add_external_statistics(
            self.hass, self._cost_metadata(serial_number, meter), rows
        )

    @staticmethod
    def _meter_statistic_ids(
        serial_number: str, meter: dict[str, Any]
    ) -> tuple[str, str]:
        """Return the (consumption, cost) external statistic ids for a meter."""
        id_prefix = f"{meter['serviceType'].lower()}_{serial_number}"
        return (
            f"{DOMAIN}:{id_prefix}_energy_consumption",
            f"{DOMAIN}:{id_prefix}_energy_cost",
        )

    def _energy_metadata(
        self, serial_number: str, meter: dict[str, Any]
    ) -> StatisticMetaData:
        """Build consumption statistic metadata for a meter."""
        consumption_id, _ = self._meter_statistic_ids(serial_number, meter)
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=(
                f"Duke Energy {meter['serviceType'].capitalize()} "
                f"{serial_number} Consumption"
            ),
            source=DOMAIN,
            statistic_id=consumption_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
            if meter["serviceType"] == "ELECTRIC"
            else UnitOfVolume.CENTUM_CUBIC_FEET,
        )

    def _cost_metadata(
        self, serial_number: str, meter: dict[str, Any]
    ) -> StatisticMetaData:
        """Build cost statistic metadata for a meter."""
        _, cost_id = self._meter_statistic_ids(serial_number, meter)
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=(
                f"Duke Energy {meter['serviceType'].capitalize()} {serial_number} Cost"
            ),
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=None,
        )

    @staticmethod
    def _build_consumption_rows(
        energy_by_hour: dict[datetime, float],
        baseline: tuple[float, float | None],
    ) -> list[StatisticData]:
        """Build cumulative consumption rows for hours after the baseline."""
        total, resume = baseline
        rows: list[StatisticData] = []
        for start, energy in energy_by_hour.items():
            if resume is not None and start.timestamp() <= resume:
                continue
            total += energy
            rows.append(StatisticData(start=start, state=energy, sum=total))
        return rows

    def _build_cost_rows(
        self,
        energy_by_hour: dict[datetime, float],
        baseline: tuple[float, float | None],
        price_at: Callable[[datetime], float | None],
        meter_options: Mapping[str, Any],
    ) -> list[StatisticData]:
        """
        Build cumulative cost rows from per-hour energy.

        Stops at the first hour without a resolvable price so the sum stays
        contiguous; a later refresh resumes there once a price is available.
        """
        total, resume = baseline
        rows: list[StatisticData] = []
        for start, energy in energy_by_hour.items():
            if resume is not None and start.timestamp() <= resume:
                continue
            price = price_at(start)
            if price is None:
                _LOGGER.debug("No price available for %s; deferring cost", start)
                break
            charge = energy * price + self._hourly_fixed_charge(meter_options, start)
            total += charge
            rows.append(StatisticData(start=start, state=charge, sum=total))
        return rows

    async def _async_consumption_energy(
        self, consumption_id: str, resume: float | None
    ) -> dict[datetime, float]:
        """
        Return per-hour energy from the stored consumption statistic.

        Reads locally recorded data (no Duke API call). When resuming it starts a
        lookback before the resume point so corrected hours are recomputed; a
        full rebuild (resume is None) reads the whole history.
        """
        tz = await dt_util.async_get_time_zone(_TIMEZONE)
        start = (
            dt_util.utc_from_timestamp(resume) - _LOOKBACK
            if resume is not None
            else dt_util.utc_from_timestamp(0)
        )
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            None,
            {consumption_id},
            "hour",
            None,
            {"state"},
        )
        return {
            dt_util.utc_from_timestamp(row["start"]).astimezone(tz): cast(
                "float", row["state"]
            )
            for row in stats.get(consumption_id, [])
            if row.get("state") is not None
        }

    async def _async_last_stat_start(self, statistic_id: str) -> float | None:
        """Return the start time of a statistic's newest row, or None if empty."""
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,  # noqa: FBT003
            set(),
        )
        if last_stat:
            return last_stat[statistic_id][0]["start"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
        return None

    @staticmethod
    def _resume_baseline(
        rows: list[Any] | None, resume: float | None
    ) -> tuple[float, float | None]:
        """
        Return the (sum, start) to resume a statistic from.

        Anchors to roughly the lookback window before the statistic's own newest
        row, so recently corrected hours are rewritten without dragging the
        resume point back to the (possibly older) start of the query window.
        """
        if not rows or resume is None:
            return 0.0, None
        threshold = resume - _LOOKBACK.total_seconds()
        for row in rows:
            if row["start"] >= threshold:
                return cast("float", row["sum"]), row["start"]
        return cast("float", rows[-1]["sum"]), rows[-1]["start"]

    def _meter_options(self, serial_number: str) -> Mapping[str, Any]:
        """
        Return a meter's cost options.

        Falls back to the pre-per-meter flat options so entries configured
        before per-meter pricing keep working until reconfigured.
        """
        meters = self.config_entry.options.get(CONF_METERS)
        if meters is None:
            return self.config_entry.options
        return meters.get(serial_number, {})

    async def _async_build_price_lookup(
        self,
        meter_options: Mapping[str, Any],
        start: datetime,
        end: datetime,
    ) -> Callable[[datetime], float | None] | None:
        """
        Build a callable resolving the $/kWh price at a point in time.

        Fixed mode returns a constant. Entity mode resolves prices from the
        entity's long-term statistics (hourly mean) so time-of-use and
        seasonal rates apply to backfilled hours, falling back to the entity's
        current state for hours not covered by statistics.

        Returns None if cost tracking is misconfigured.
        """
        cost_mode = meter_options.get(CONF_COST_MODE, COST_MODE_NONE)

        if cost_mode == COST_MODE_FIXED:
            fixed_price = meter_options.get(CONF_FIXED_PRICE)
            if not fixed_price:
                _LOGGER.warning("Fixed cost mode is enabled but no price is set")
                return None
            return lambda _start: float(fixed_price)

        if cost_mode == COST_MODE_ENTITY:
            entity_id = meter_options.get(CONF_PRICE_ENTITY)
            if not entity_id:
                _LOGGER.warning("Entity cost mode is enabled but no entity is set")
                return None

            current_price: float | None = None
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                try:
                    current_price = float(state.state)
                except ValueError:
                    _LOGGER.warning(
                        "Price entity %s has a non-numeric state: %s",
                        entity_id,
                        state.state,
                    )

            price_stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end + timedelta(hours=1),
                {entity_id},
                "hour",
                None,
                {"mean"},
            )
            price_rows = [
                (row["start"], cast("float", row["mean"]))
                for row in price_stats.get(entity_id, [])
                if row.get("mean") is not None
            ]
            price_starts = [row[0] for row in price_rows]

            def _lookup(point: datetime) -> float | None:
                index = bisect_right(price_starts, point.timestamp()) - 1
                if index >= 0:
                    return price_rows[index][1]
                if price_rows:
                    # Before the entity's recorded history: use the earliest
                    # known price rather than today's.
                    return price_rows[0][1]
                return current_price

            return _lookup

        return None

    def _hourly_fixed_charge(
        self, meter_options: Mapping[str, Any], start: datetime
    ) -> float:
        """Return the monthly charge amortized over the actual hours of the month."""
        monthly_charge = meter_options.get(CONF_MONTHLY_CHARGE)
        if not monthly_charge:
            return 0.0
        month_start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        years, month_index = divmod(month_start.month, 12)
        next_month = month_start.replace(
            year=month_start.year + years, month=month_index + 1
        )
        # timestamp() spans real time, so DST-transition months get 23/25h days.
        hours = (next_month.timestamp() - month_start.timestamp()) / 3600
        return float(monthly_charge) / hours

    async def _async_get_energy_usage(
        self, meter: dict[str, Any], start_time: float | None = None
    ) -> dict[datetime, dict[str, float | int]]:
        """
        Get energy usage.

        If start_time is None, get usage since account activation (or as far
        back as possible), otherwise since start_time - 30 days to allow
        corrections in data.

        Duke Energy provides hourly data all the way back to ~3 years.
        """
        # All of Duke Energy Service Areas are currently in America/New_York timezone
        # May need to re-think this if that ever changes and determine timezone based
        # on the service address somehow.
        tz = await dt_util.async_get_time_zone(_TIMEZONE)
        lookback = _LOOKBACK
        one = timedelta(days=1)
        if start_time is None:
            # Max 3 years of data
            start = dt_util.now(tz) - timedelta(days=3 * 365)
        else:
            start = datetime.fromtimestamp(start_time, tz=tz) - lookback
        agreement_date = dt_util.parse_datetime(meter["agreementActiveDate"])
        if agreement_date is not None:
            start = max(agreement_date.replace(tzinfo=tz), start)

        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = dt_util.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - one
        _LOGGER.debug("Data lookup range: %s - %s", start, end)

        start_step = max(end - lookback, start)
        end_step = end
        usage: dict[datetime, dict[str, float | int]] = {}
        while True:
            _LOGGER.debug("Getting hourly usage: %s - %s", start_step, end_step)
            try:
                # Get data
                try:
                    results = await self.api.get_energy_usage(
                        meter["serialNum"], "HOURLY", "DAY", start_step, end_step
                    )
                except DukeEnergyAuthError as err:
                    raise ConfigEntryAuthFailed from err

                usage = {**results["data"], **usage}

                for missing in results["missing"]:
                    _LOGGER.debug("Missing data: %s", missing)

                # Set next range
                end_step = start_step - one
                start_step = max(start_step - lookback, start)

                # Make sure we don't go back too far
                if end_step < start:
                    break
            except (TimeoutError, ClientError):
                # ClientError is raised when there is no more data for the range
                break

        _LOGGER.debug("Got %s meter usage reads", len(usage))
        return usage
