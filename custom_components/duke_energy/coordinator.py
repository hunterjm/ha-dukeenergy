"""Coordinator to handle Duke Energy connections."""

import json
import logging
import math
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict, cast

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
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .const import DOMAIN, SUPPORTED_METER_TYPES
from .cost import (
    CostLedger,
    CostStatistic,
    CostStatisticsBatch,
    ManualRateProvider,
    UsageData,
)

_LOGGER = logging.getLogger(__name__)


class DukeEnergyCostData(TypedDict):
    """Cost data exposed for a Duke Energy meter."""

    meter: dict[str, Any]
    total_cost: Decimal


type DukeEnergyConfigEntry = ConfigEntry[DukeEnergyCoordinator]


class DukeEnergyCoordinator(DataUpdateCoordinator[dict[str, DukeEnergyCostData]]):
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
        self.meters: dict[str, dict[str, Any]] = {}
        self.rate_provider = ManualRateProvider(config_entry.options)
        self.cost_ledger = CostLedger(hass, config_entry.entry_id)

        @callback
        def _dummy_listener() -> None:
            pass

        # Force the coordinator to periodically update.
        # Duke Energy does not provide forecast data, so all information is historical.
        # This makes _async_update_data get periodically called to insert statistics.
        self.async_add_listener(_dummy_listener)

    async def async_initialize_costs(self) -> None:
        """Load persisted cumulative cost data."""
        await self.cost_ledger.async_load()

    async def _async_update_data(self) -> dict[str, DukeEnergyCostData]:
        """Insert Duke Energy statistics."""
        cost_data: dict[str, DukeEnergyCostData] = {}

        try:
            meters: dict[str, dict[str, Any]] = await self.api.get_meters()
        except DukeEnergyAuthError as err:
            raise ConfigEntryAuthFailed from err
        self.meters = meters

        for serial_number, meter in meters.items():
            if (
                not isinstance(meter["serviceType"], str)
                or meter["serviceType"] not in SUPPORTED_METER_TYPES
            ):
                _LOGGER.debug(
                    "Skipping unsupported meter type %s", meter["serviceType"]
                )
                continue

            id_prefix = f"{meter['serviceType'].lower()}_{serial_number}"
            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
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
                usage = await self._async_get_energy_usage(meter)
                consumption_sum = 0.0
                last_stats_time = None
            else:
                usage = await self._async_get_energy_usage(
                    meter,
                    last_stat[consumption_statistic_id][0]["start"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                )
                if not usage:
                    _LOGGER.debug("No recent usage data. Skipping update")
                else:
                    stats = await get_instance(self.hass).async_add_executor_job(
                        statistics_during_period,
                        self.hass,
                        min(usage),
                        None,
                        {consumption_statistic_id},
                        "hour",
                        None,
                        {"sum"},
                    )
                    consumption_sum = cast(
                        "float",
                        stats[consumption_statistic_id][0]["sum"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                    )
                    last_stats_time = stats[consumption_statistic_id][0]["start"]  # pyright: ignore[reportTypedDictNotRequiredAccess]

            service_type = meter["serviceType"]
            if self.rate_provider.has_history(service_type):
                await self._async_update_cost_statistics(
                    meter,
                    serial_number,
                    id_prefix,
                    usage,
                    cost_data,
                )

            if not usage:
                continue

            consumption_statistics = []

            for start, data in usage.items():
                if last_stats_time is not None and start.timestamp() <= last_stats_time:
                    continue
                consumption_sum += data["energy"]

                consumption_statistics.append(
                    StatisticData(
                        start=start, state=data["energy"], sum=consumption_sum
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
                unit_class=(
                    EnergyConverter.UNIT_CLASS
                    if meter["serviceType"] == "ELECTRIC"
                    else VolumeConverter.UNIT_CLASS
                ),
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

        await self.cost_ledger.async_save()
        await self._async_write_sanitized_diagnostic()

        return cost_data

    async def _async_write_sanitized_diagnostic(self) -> None:
        """Write the temporary diagnostic containing no private values."""
        report_getter = getattr(self.api, "sanitized_diagnostic", None)
        if report_getter is None:
            return
        report = report_getter()
        target = Path(self.hass.config.path("duke_energy_sanitized_diagnostic.json"))
        content = json.dumps(report, indent=2, sort_keys=True) + "\n"
        await self.hass.async_add_executor_job(target.write_text, content, "utf-8")

    async def _async_update_cost_statistics(
        self,
        meter: dict[str, Any],
        serial_number: str,
        meter_id: str,
        usage: UsageData,
        cost_data: dict[str, DukeEnergyCostData],
    ) -> None:
        """Update a meter's cost ledger, sensor data, and statistics."""
        service_type = meter["serviceType"]
        statistic_id = f"{DOMAIN}:{meter_id}_total_cost"
        required_start = self.rate_provider.earliest_rate_date(service_type)
        covered_from = self.cost_ledger.history_covered_from(meter_id)
        completed_history_start: date | None = None

        if required_start is not None and (
            covered_from is None or covered_from > required_start
        ):
            history_end = (
                covered_from - timedelta(days=1)
                if covered_from is not None
                else dt_util.now().date() - timedelta(days=1)
            )
            historical_usage = await self._async_get_historical_usage(
                meter,
                required_start,
                history_end,
            )
            if historical_usage is not None:
                usage = {**historical_usage, **usage}
                completed_history_start = required_start

        self.cost_ledger.reprice(meter_id, service_type, self.rate_provider)
        total_cost = self.cost_ledger.update(
            meter_id,
            service_type,
            usage,
            self.rate_provider,
        )
        if completed_history_start is not None:
            self.cost_ledger.mark_history_covered_from(
                meter_id,
                completed_history_start,
            )
        if self.rate_provider.enabled(service_type):
            cost_data[serial_number] = DukeEnergyCostData(
                meter=meter,
                total_cost=total_cost,
            )

        if (cost_batch := self.cost_ledger.cost_statistics(meter_id)) is None:
            return

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=(
                f"Duke Energy {service_type.capitalize()} {serial_number} Total cost"
            ),
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency,
        )
        statistics = [
            StatisticData(
                start=statistic.start,
                state=float(statistic.interval_cost),
                sum=float(statistic.cumulative_cost),
            )
            for statistic in cost_batch.statistics
        ]

        # Persist the pending range before queueing the recorder import.
        await self.cost_ledger.async_save()
        _LOGGER.debug("Adding %s statistics for %s", len(statistics), statistic_id)
        async_add_external_statistics(self.hass, metadata, statistics)
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_confirm_cost_statistics(
                meter_id,
                statistic_id,
                cost_batch,
            ),
            f"confirm {statistic_id} import",
        )

    async def _async_confirm_cost_statistics(
        self,
        meter_id: str,
        statistic_id: str,
        batch: CostStatisticsBatch,
    ) -> None:
        """Wait for recorder and acknowledge a verified cost import."""
        await get_instance(self.hass).async_block_till_done()

        if await self._async_cost_statistics_imported(statistic_id, batch):
            self.cost_ledger.acknowledge_cost_statistics(
                meter_id,
                batch.dirty_from,
            )
            await self.cost_ledger.async_save()

    async def _async_cost_statistics_imported(
        self,
        statistic_id: str,
        batch: CostStatisticsBatch,
    ) -> bool:
        """Verify that recorder committed both ends of a cost batch."""
        first_expected = batch.statistics[0]
        last_expected = batch.statistics[-1]
        results = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            first_expected.start,
            last_expected.start + timedelta(hours=1),
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        rows = results.get(statistic_id, [])

        def _matches(expected: CostStatistic) -> bool:
            expected_start = expected.start.timestamp()
            expected_state = float(expected.interval_cost)
            expected_sum = float(expected.cumulative_cost)

            for row in rows:
                if row["start"] != expected_start:
                    continue

                state = row.get("state")
                statistic_sum = row.get("sum")
                return (
                    state is not None
                    and statistic_sum is not None
                    and math.isclose(state, expected_state)
                    and math.isclose(statistic_sum, expected_sum)
                )

            return False

        if _matches(first_expected) and _matches(last_expected):
            return True

        _LOGGER.warning(
            "Recorder did not confirm both ends of cost statistics import "
            "for %s from %s through %s; the range will be retried",
            statistic_id,
            first_expected.start,
            last_expected.start,
        )
        return False

    async def _async_get_historical_usage(
        self,
        meter: dict[str, Any],
        requested_start: date,
        requested_end: date,
    ) -> dict[datetime, dict[str, float | int]] | None:
        """Fetch a complete bounded historical range."""
        tz = await self._async_get_service_timezone()
        start = datetime.combine(requested_start, time.min, tzinfo=tz)
        end = datetime.combine(requested_end, time.min, tzinfo=tz)

        agreement_date = dt_util.parse_datetime(meter["agreementActiveDate"])
        if agreement_date is not None:
            start = max(self._normalize_datetime(agreement_date, tz), start)

        if start > end:
            return {}

        lookback = timedelta(days=30)
        one_day = timedelta(days=1)
        start_step = max(end - lookback, start)
        end_step = end
        usage: dict[datetime, dict[str, float | int]] = {}

        while True:
            _LOGGER.debug(
                "Getting bounded historical usage: %s - %s",
                start_step,
                end_step,
            )
            try:
                results = await self.api.get_energy_usage(
                    meter["serialNum"],
                    "HOURLY" if meter["serviceType"] == "ELECTRIC" else "DAILY",
                    "DAY" if meter["serviceType"] == "ELECTRIC" else "BILLINGCYCLE",
                    start_step,
                    end_step,
                )
            except DukeEnergyAuthError as err:
                raise ConfigEntryAuthFailed from err
            except (TimeoutError, ClientError):
                _LOGGER.warning(
                    "Historical usage fetch did not complete for meter %s "
                    "from %s through %s; coverage will not be advanced",
                    meter["serialNum"],
                    requested_start,
                    requested_end,
                )
                return None

            usage = {**results["data"], **usage}
            for missing in results["missing"]:
                _LOGGER.debug("Missing historical data: %s", missing)

            if start_step == start:
                break

            end_step = start_step - one_day
            start_step = max(start_step - lookback, start)

        return usage

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
        tz = await self._async_get_service_timezone()
        lookback = timedelta(days=30)
        one = timedelta(days=1)
        if start_time is None:
            # Max 3 years of data
            start = dt_util.now(tz) - timedelta(days=3 * 365)
        else:
            start = datetime.fromtimestamp(start_time, tz=tz) - lookback
        agreement_date = dt_util.parse_datetime(meter["agreementActiveDate"])
        if agreement_date is not None:
            start = max(self._normalize_datetime(agreement_date, tz), start)

        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = dt_util.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - one
        _LOGGER.debug("Data lookup range: %s - %s", start, end)

        start_step = max(end - lookback, start)
        end_step = end
        usage: dict[datetime, dict[str, float | int]] = {}
        while True:
            interval = "HOURLY" if meter["serviceType"] == "ELECTRIC" else "DAILY"
            period = "DAY" if meter["serviceType"] == "ELECTRIC" else "BILLINGCYCLE"
            _LOGGER.debug(
                "Getting %s/%s usage: %s - %s",
                interval,
                period,
                start_step,
                end_step,
            )
            try:
                # Get data
                try:
                    results = await self.api.get_energy_usage(
                        meter["serialNum"],
                        interval,
                        period,
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
        return usage

    async def _async_get_service_timezone(self) -> tzinfo:
        """Return the timezone used for Duke usage calendar dates."""
        timezone = await dt_util.async_get_time_zone(self.hass.config.time_zone)
        if timezone is None:
            msg = f"Invalid Home Assistant timezone: {self.hass.config.time_zone}"
            raise ValueError(msg)
        return timezone

    @staticmethod
    def _normalize_datetime(value: datetime, timezone: tzinfo) -> datetime:
        """Normalize a parsed timestamp into the service timezone."""
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone)
        return value.astimezone(timezone)
