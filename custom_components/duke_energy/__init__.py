"""The Duke Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiodukeenergy import DukeEnergy
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .api import DukeEnergyAuth
from .const import DOMAIN
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

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_update_options(
    hass: HomeAssistant, entry: DukeEnergyConfigEntry
) -> None:
    """
    Reload the entry when options change.

    Unloading clears the inserted statistics, so the following setup rebuilds
    consumption and cost history from scratch with the new price settings.
    """
    await hass.config_entries.async_reload(entry.entry_id)


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
