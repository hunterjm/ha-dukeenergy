"""Config flow for Duke Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import jwt
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_PRICE,
    CONF_MONTHLY_CHARGE,
    CONF_PRICE_ENTITY,
    COST_MODE_ENTITY,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODES,
    DOMAIN,
)
from .oauth import DukeEnergyOAuth2Implementation

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)


class DukeEnergyOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle a config flow for Duke Energy."""

    VERSION = 2
    MINOR_VERSION = 1

    DOMAIN = DOMAIN

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @staticmethod
    @callback
    def async_get_options_flow(
        _config_entry: ConfigEntry,
    ) -> DukeEnergyOptionsFlowHandler:
        """Create the options flow."""
        return DukeEnergyOptionsFlowHandler()

    async def async_step_pick_implementation(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle picking implementation - directly use our implementation."""
        self.flow_impl = DukeEnergyOAuth2Implementation(self.hass)
        return await self.async_step_auth()

    async def async_step_reauth(self, _: Mapping[str, Any]) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create an entry for the flow."""
        # Extract user info from id_token
        try:
            id_token = data["token"]["id_token"]
            token_data = jwt.decode(id_token, options={"verify_signature": False})
            user_id = token_data.get("internal_identifier", "").lower()
            email = token_data.get("email", "").lower()
        except (KeyError, ValueError):
            _LOGGER.exception("Failed to decode ID token")
            return self.async_abort(reason="oauth_error")

        if not user_id:
            _LOGGER.error("No internal_identifier in ID token claims")
            return self.async_abort(reason="oauth_error")

        await self.async_set_unique_id(user_id)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=data,
            )
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=email or user_id, data=data)


class DukeEnergyOptionsFlowHandler(OptionsFlow):
    """Handle Duke Energy options for cost statistics."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the cost tracking options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            cost_mode = user_input[CONF_COST_MODE]
            if cost_mode == COST_MODE_FIXED and not user_input.get(CONF_FIXED_PRICE):
                errors[CONF_FIXED_PRICE] = "fixed_price_required"
            elif cost_mode == COST_MODE_ENTITY and not user_input.get(
                CONF_PRICE_ENTITY
            ):
                errors[CONF_PRICE_ENTITY] = "price_entity_required"
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        options = user_input or self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_COST_MODE,
                    default=options.get(CONF_COST_MODE, COST_MODE_NONE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=COST_MODES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_COST_MODE,
                    )
                ),
                vol.Optional(
                    CONF_FIXED_PRICE,
                    description={
                        "suggested_value": options.get(CONF_FIXED_PRICE),
                    },
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step="any",
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="$/kWh",
                    )
                ),
                vol.Optional(
                    CONF_PRICE_ENTITY,
                    description={
                        "suggested_value": options.get(CONF_PRICE_ENTITY),
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "input_number", "number"])
                ),
                vol.Optional(
                    CONF_MONTHLY_CHARGE,
                    description={
                        "suggested_value": options.get(CONF_MONTHLY_CHARGE),
                    },
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step="any",
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="$/month",
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
