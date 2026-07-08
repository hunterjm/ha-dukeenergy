"""Coordinator to handle Duke Energy connections."""

import calendar
import logging
from bisect import bisect_right
from collections.abc import Callable
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
    CONF_MONTHLY_CHARGE,
    CONF_PRICE_ENTITY,
    COST_MODE_ENTITY,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_SUPPORTED_METER_TYPES = ("ELECTRIC",)

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

        for serial_number, meter in meters.items():
            if (
                not isinstance(meter["serviceType"], str)
                or meter["serviceType"] not in _SUPPORTED_METER_TYPES
            ):
                _LOGGER.debug(
                    "Skipping unsupported meter type %s", meter["serviceType"]
                )
                continue

            await self._async_insert_meter_statistics(serial_number, meter)

    async def _async_insert_meter_statistics(
        self, serial_number: str, meter: dict[str, Any]
    ) -> None:
        """Insert consumption and (if enabled) cost statistics for a meter."""
        id_prefix = f"{meter['serviceType'].lower()}_{serial_number}"
        consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
        cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"
        cost_enabled = (
            self.config_entry.options.get(CONF_COST_MODE, COST_MODE_NONE)
            != COST_MODE_NONE
        )
        self._statistic_ids.add(consumption_statistic_id)
        if cost_enabled:
            self._statistic_ids.add(cost_statistic_id)
        _LOGGER.debug(
            "Updating Statistics for %s",
            consumption_statistic_id,
        )

        statistic_ids = {consumption_statistic_id}
        if cost_enabled:
            statistic_ids.add(cost_statistic_id)

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            consumption_statistic_id,
            True,  # noqa: FBT003
            set(),
        )
        # Each baseline is the (sum, start time) to resume statistics from.
        cost_baseline: tuple[float, float | None] = (0.0, None)
        if not last_stat:
            _LOGGER.debug("Updating statistic for the first time")
            usage = await self._async_get_energy_usage(meter)
            consumption_baseline: tuple[float, float | None] = (0.0, None)
        else:
            usage = await self._async_get_energy_usage(
                meter,
                last_stat[consumption_statistic_id][0]["start"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
            )
            if not usage:
                _LOGGER.debug("No recent usage data. Skipping update")
                return
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                min(usage.keys()),
                None,
                statistic_ids,
                "hour",
                None,
                {"sum"},
            )
            consumption_baseline = (
                cast(
                    "float",
                    stats[consumption_statistic_id][0]["sum"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                ),
                stats[consumption_statistic_id][0]["start"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
            )
            if cost_enabled:
                cost_baseline = await self._async_get_cost_baseline(
                    cost_statistic_id, stats.get(cost_statistic_id)
                )

        price_at: Callable[[datetime], float | None] | None = None
        if cost_enabled and usage:
            price_at = await self._async_build_price_lookup(
                min(usage.keys()), max(usage.keys())
            )

        consumption_statistics, cost_statistics = self._build_statistic_rows(
            usage, consumption_baseline, cost_baseline, price_at
        )

        name_prefix = f"Duke Energy {meter['serviceType'].capitalize()} {serial_number}"
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name_prefix} Consumption",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
            if meter["serviceType"] == "ELECTRIC"
            else UnitOfVolume.CENTUM_CUBIC_FEET,
        )

        _LOGGER.debug(
            "Adding %s statistics for %s",
            len(consumption_statistics),
            consumption_statistic_id,
        )
        async_add_external_statistics(
            self.hass, consumption_metadata, consumption_statistics
        )

        if cost_enabled and cost_statistics:
            cost_metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"{name_prefix} Cost",
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_class=None,
                unit_of_measurement=None,
            )
            _LOGGER.debug(
                "Adding %s statistics for %s",
                len(cost_statistics),
                cost_statistic_id,
            )
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)

    def _build_statistic_rows(
        self,
        usage: dict[datetime, dict[str, float | int]],
        consumption_baseline: tuple[float, float | None],
        cost_baseline: tuple[float, float | None],
        price_at: Callable[[datetime], float | None] | None,
    ) -> tuple[list[StatisticData], list[StatisticData]]:
        """
        Build consumption and cost statistic rows from usage data.

        Each baseline is the (sum, start time) to resume from. Consumption and
        cost carry independent resume times so a cost statistic that lags
        behind (price entity unavailable, or cost enabled after consumption
        already existed) can catch up on later refreshes.
        """
        consumption_sum, last_stats_time = consumption_baseline
        cost_sum, cost_last_stats_time = cost_baseline
        consumption_statistics: list[StatisticData] = []
        cost_statistics: list[StatisticData] = []
        cost_incomplete = False

        for start, data in usage.items():
            energy = data["energy"]
            if last_stats_time is None or start.timestamp() > last_stats_time:
                consumption_sum += energy
                consumption_statistics.append(
                    StatisticData(start=start, state=energy, sum=consumption_sum)
                )

            if (
                price_at is None
                or cost_incomplete
                or (
                    cost_last_stats_time is not None
                    and start.timestamp() <= cost_last_stats_time
                )
            ):
                continue
            price = price_at(start)
            if price is None:
                # Stop instead of skipping so the sum stays contiguous;
                # the next refresh retries these hours.
                cost_incomplete = True
                _LOGGER.warning(
                    "No price available for %s; deferring cost statistics", start
                )
                continue
            cost_state = energy * price + self._hourly_fixed_charge(start)
            cost_sum += cost_state
            cost_statistics.append(
                StatisticData(start=start, state=cost_state, sum=cost_sum)
            )

        return consumption_statistics, cost_statistics

    async def _async_get_cost_baseline(
        self, cost_statistic_id: str, stats_in_window: list[Any] | None
    ) -> tuple[float, float | None]:
        """
        Get the sum and start time to resume cost statistics from.

        Prefer the oldest cost statistic within the current usage window so the
        30-day lookback can rewrite recent hours with corrected data, mirroring
        the consumption logic. If cost statistics exist only before the window
        (e.g. the price entity was unavailable for a while), resume from the
        newest one. If none exist at all, start from zero and backfill whatever
        usage the window covers.
        """
        if stats_in_window:
            return (
                cast("float", stats_in_window[0]["sum"]),
                stats_in_window[0]["start"],
            )
        last_cost_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            cost_statistic_id,
            True,  # noqa: FBT003
            {"sum"},
        )
        if last_cost_stat:
            return (
                cast("float", last_cost_stat[cost_statistic_id][0]["sum"]),
                last_cost_stat[cost_statistic_id][0]["start"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
            )
        return 0.0, None

    async def _async_build_price_lookup(
        self, start: datetime, end: datetime
    ) -> Callable[[datetime], float | None] | None:
        """
        Build a callable resolving the $/kWh price at a point in time.

        Fixed mode returns a constant. Entity mode resolves prices from the
        entity's long-term statistics (hourly mean) so time-of-use and
        seasonal rates apply to backfilled hours, falling back to the entity's
        current state for hours not covered by statistics.

        Returns None if cost tracking is misconfigured.
        """
        options = self.config_entry.options
        cost_mode = options.get(CONF_COST_MODE, COST_MODE_NONE)

        if cost_mode == COST_MODE_FIXED:
            fixed_price = options.get(CONF_FIXED_PRICE)
            if not fixed_price:
                _LOGGER.warning("Fixed cost mode is enabled but no price is set")
                return None
            return lambda _start: float(fixed_price)

        if cost_mode == COST_MODE_ENTITY:
            entity_id = options.get(CONF_PRICE_ENTITY)
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

    def _hourly_fixed_charge(self, start: datetime) -> float:
        """Return the fixed monthly charge amortized over the hours of a month."""
        monthly_charge = self.config_entry.options.get(CONF_MONTHLY_CHARGE)
        if not monthly_charge:
            return 0.0
        days_in_month = calendar.monthrange(start.year, start.month)[1]
        return float(monthly_charge) / (days_in_month * 24)

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
        tz = await dt_util.async_get_time_zone("America/New_York")
        lookback = timedelta(days=30)
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
