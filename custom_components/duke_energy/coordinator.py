"""Coordinator to handle Duke Energy connections."""

import logging
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
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_SUPPORTED_METER_TYPES = ("ELECTRIC", "GAS")

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

            id_prefix = f"{meter['serviceType'].lower()}_{serial_number}"
            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
            self._statistic_ids.add(consumption_statistic_id)
            _LOGGER.debug(
                "Updating Statistics for %s",
                consumption_statistic_id,
            )

            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,
                consumption_statistic_id,
                True,  # noqa: FBT003
                set(),
            )
            if not last_stat:
                _LOGGER.debug("Updating statistic for the first time")
                usage, interval = await self._async_get_energy_usage(meter)
                consumption_sum = 0.0
                last_stats_time = None
            else:
                usage, interval = await self._async_get_energy_usage(
                    meter,
                    last_stat[consumption_statistic_id][0]["start"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                )
                if not usage:
                    _LOGGER.debug("No recent usage data. Skipping update")
                    continue
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    min(usage.keys()),
                    None,
                    {consumption_statistic_id},
                    "hour" if interval == "HOURLY" else "day",
                    None,
                    {"sum"},
                )
                consumption_sum = cast(
                    "float",
                    stats[consumption_statistic_id][0]["sum"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                )
                last_stats_time = stats[consumption_statistic_id][0]["start"]  # pyright: ignore[reportTypedDictNotRequiredAccess]

            consumption_statistics = []

            for start, data in usage.items():
                if last_stats_time is not None and start.timestamp() <= last_stats_time:
                    continue
                consumption_sum += data["energy"]

                # For daily intervals, register usage at noon to better represent
                # when daily usage occurred rather than at midnight (start of day).
                stat_start = (
                    start + timedelta(hours=12) if interval == "DAILY" else start
                )
                consumption_statistics.append(
                    StatisticData(
                        start=stat_start, state=data["energy"], sum=consumption_sum
                    )
                )

            name_prefix = (
                f"Duke Energy {meter['serviceType'].capitalize()} {serial_number}"
            )
            consumption_metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"{name_prefix} Consumption",
                source=DOMAIN,
                statistic_id=consumption_statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS
                if meter["serviceType"] == "ELECTRIC"
                else "volume",
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

    async def _async_get_energy_usage(
        self, meter: dict[str, Any], start_time: float | None = None
    ) -> tuple[dict[datetime, dict[str, float | int]], str]:
        """
        Get energy usage.

        If start_time is None, get usage since account activation (or as far
        back as possible), otherwise since start_time - 30 days to allow
        corrections in data.

        Duke Energy provides hourly data all the way back to ~3 years.

        Returns a tuple of (usage, interval) where interval is "HOURLY" or "DAILY".
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

        if meter["serviceType"] == "GAS":
            run_interval = "DAILY"
            run_period = "WEEK"
        else:
            run_interval = "HOURLY"
            run_period = "DAY"

        start_step = max(end - lookback, start)
        end_step = end
        usage: dict[datetime, dict[str, float | int]] = {}
        while True:
            _LOGGER.debug(
                "Getting %s %s usage: %s - %s",
                meter["serviceType"].lower().capitalize(),
                run_interval.lower(),
                start_step,
                end_step,
            )
            try:
                # Get data
                try:
                    results = await self.api.get_energy_usage(
                        meter["serialNum"],
                        run_interval,
                        run_period,
                        start_step,
                        end_step,
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
        return usage, run_interval
