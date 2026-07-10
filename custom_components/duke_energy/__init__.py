"""The Duke Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiodukeenergy import DukeEnergy
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .api import DukeEnergyAuth
from .const import (
    CONF_COST_MODE,
    CONF_FIXED_PRICE,
    CONF_METERS,
    CONF_MONTHLY_CHARGE,
    CONF_PRICE_ENTITY,
    DOMAIN,
)
from .coordinator import DukeEnergyConfigEntry, DukeEnergyCoordinator
from .oauth import DukeEnergyOAuth2Implementation

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: DukeEnergyConfigEntry) -> bool:
    """Set up Duke Energy from a config entry."""
    # Register our OAuth implementation
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        DukeEnergyOAuth2Implementation(hass),
    )

    # Check if this is an old entry that needs reauth
    if not entry.data.get("token"):
        msg = "Authentication method has changed. Please reauthenticate."
        raise ConfigEntryAuthFailed(msg)

    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    try:
        await session.async_ensure_token_valid()
    except Exception as err:
        raise ConfigEntryAuthFailed from err

    auth = DukeEnergyAuth(aiohttp_client.async_get_clientsession(hass), session)
    client = DukeEnergy(auth)

    coordinator = DukeEnergyCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    _migrate_flat_options(hass, entry, coordinator.meters)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


def _migrate_flat_options(
    hass: HomeAssistant,
    entry: DukeEnergyConfigEntry,
    meters: dict[str, dict[str, Any]],
) -> None:
    """Migrate pre-per-meter flat cost options onto every meter."""
    options = entry.options
    if CONF_METERS in options or CONF_COST_MODE not in options or not meters:
        return
    flat = {
        key: options[key]
        for key in (
            CONF_COST_MODE,
            CONF_FIXED_PRICE,
            CONF_PRICE_ENTITY,
            CONF_MONTHLY_CHARGE,
        )
        if key in options
    }
    hass.config_entries.async_update_entry(
        entry, options={CONF_METERS: {serial: dict(flat) for serial in meters}}
    )


async def async_update_options(
    _hass: HomeAssistant, entry: DukeEnergyConfigEntry
) -> None:
    """
    Recompute cost statistics locally when options change.

    Cost is derived from the stored consumption statistic, so a price change is a
    local recompute -- no entry reload, no Duke API calls, and consumption
    history is left untouched.
    """
    await entry.runtime_data.async_rebuild_costs()


async def async_migrate_entry(
    hass: HomeAssistant, entry: DukeEnergyConfigEntry
) -> bool:
    """Cannot migrate without reauth."""
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry, data={}, minor_version=1, version=2
        )
    return True


async def async_unload_entry(
    _hass: HomeAssistant, _entry: DukeEnergyConfigEntry
) -> bool:
    """Unload a config entry."""
    return True
