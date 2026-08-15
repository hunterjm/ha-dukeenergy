"""Config flow for Duke Energy integration."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from aiodukeenergy import (
    Auth0Client,
    AuthorizationTransaction,
    DukeEnergyAuthError,
    DukeEnergyOAuthCallbackError,
)
from homeassistant import config_entries
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COST_TRACKING,
    CONF_EFFECTIVE_DATE,
    CONF_ENABLED,
    CONF_RATE,
    CONF_RATES,
    DOMAIN,
    SUPPORTED_METER_TYPES,
)
from .oauth import DukeEnergyOAuth2Implementation

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)


class DukeEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Duke Energy."""

    VERSION = 2
    MINOR_VERSION = 1

    DOMAIN = DOMAIN

    _auth_transaction: AuthorizationTransaction | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        _config_entry: ConfigEntry,
    ) -> DukeEnergyOptionsFlow:
        """Create the options flow."""
        return DukeEnergyOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a manual Duke Energy browser authorization."""
        if self._auth_transaction is None:
            client = Auth0Client(aiohttp_client.async_get_clientsession(self.hass))
            self._auth_transaction = client.create_authorization_transaction()
        if user_input is not None:
            return await self.async_step_callback_url()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            description_placeholders={
                "authorization_url": self._auth_transaction.authorize_url
            },
        )

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

    async def async_step_callback_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept and validate the complete Duke Energy callback URL."""
        if self._auth_transaction is None:
            return self.async_abort(reason="authentication_restarted")
        errors: dict[str, str] = {}
        if user_input is not None:
            client = Auth0Client(aiohttp_client.async_get_clientsession(self.hass))
            try:
                result = await client.complete_authorization(
                    self._auth_transaction, user_input["callback_url"]
                )
            except DukeEnergyOAuthCallbackError as err:
                return self.async_abort(
                    reason="oauth_callback_error",
                    description_placeholders={
                        "error": err.error,
                        "error_description": err.description or "No description",
                    },
                )
            except DukeEnergyAuthError:
                _LOGGER.exception("Duke Energy manual authorization failed")
                errors["base"] = "invalid_callback"
            else:
                self._auth_transaction = None
                implementation = DukeEnergyOAuth2Implementation(self.hass)
                token = implementation.adjust_token_expiry(result.token)
                return await self._async_create_or_update_entry(
                    token, result.id_token_claims
                )
        return self.async_show_form(
            step_id="callback_url",
            data_schema=vol.Schema({vol.Required("callback_url"): str}),
            errors=errors,
        )

    async def _async_create_or_update_entry(
        self, token: dict[str, Any], claims: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create or reauthenticate an entry using verified identity claims."""
        try:
            user_id = claims.get("internal_identifier", "").lower()
            email = claims.get("email", "").lower()
        except AttributeError:
            return self.async_abort(reason="invalid_token")

        if not user_id:
            return self.async_abort(reason="invalid_token")

        await self.async_set_unique_id(user_id)
        data = {"auth_implementation": DOMAIN, "token": token}
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=data,
            )
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=email or user_id, data=data)


class DukeEnergyOptionsFlow(OptionsFlowWithReload):
    """Configure Duke Energy cost tracking."""

    _working_options: dict[str, Any] | None = None
    _edit_dates: dict[str, str]

    def _ensure_working_options(self) -> dict[str, Any]:
        """Return the options being edited."""
        if self._working_options is None:
            self._working_options = deepcopy(dict(self.config_entry.options))
            self._edit_dates = {}
        return self._working_options

    def _cost_tracking(self) -> dict[str, Any]:
        """Return the working cost-tracking configuration."""
        options = self._ensure_working_options()
        return options.setdefault(CONF_COST_TRACKING, {})

    def _service_config(self, service_type: str) -> dict[str, Any]:
        """Return the working configuration for one service."""
        cost_tracking = self._cost_tracking()
        return cost_tracking.setdefault(
            service_type,
            {CONF_ENABLED: False, CONF_RATES: []},
        )

    async def async_step_init(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the rate-history menu."""
        self._ensure_working_options()
        services = self._discovered_services()
        menu_options = [service_type.lower() for service_type in services]
        menu_options.append("save")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_save(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save options and reload the config entry."""
        return self.async_create_entry(data=self._ensure_working_options())

    async def async_step_electric(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage electric rate history."""
        return self._show_service_menu("ELECTRIC")

    async def async_step_gas(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage gas rate history."""
        return self._show_service_menu("GAS")

    def _show_service_menu(self, service_type: str) -> ConfigFlowResult:
        """Show one service's rate-history menu."""
        prefix = service_type.lower()
        rates = self._service_config(service_type).get(CONF_RATES, [])
        menu_options = [f"{prefix}_settings", f"{prefix}_add"]
        if rates:
            menu_options.extend((f"{prefix}_edit", f"{prefix}_delete"))
        menu_options.append("init")
        return self.async_show_menu(
            step_id=prefix,
            menu_options=menu_options,
            description_placeholders={"rate_periods": self._format_rate_history(rates)},
        )

    async def async_step_electric_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure electric cost tracking."""
        return await self._async_settings_form("ELECTRIC", user_input)

    async def async_step_gas_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure gas cost tracking."""
        return await self._async_settings_form("GAS", user_input)

    async def _async_settings_form(
        self,
        service_type: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Configure whether cost tracking is enabled."""
        service_config = self._service_config(service_type)
        if user_input is not None:
            was_enabled = bool(service_config.get(CONF_ENABLED, False))
            enabled = bool(user_input[CONF_ENABLED])
            service_config[CONF_ENABLED] = enabled
            if was_enabled and not enabled:
                service_config[CONF_RATES] = self._upsert_period(
                    list(service_config.get(CONF_RATES, [])),
                    {
                        CONF_EFFECTIVE_DATE: dt_util.now().date().isoformat(),
                        CONF_RATE: None,
                    },
                )
            return self._show_service_menu(service_type)

        return self.async_show_form(
            step_id=f"{service_type.lower()}_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED,
                        default=bool(service_config.get(CONF_ENABLED, False)),
                    ): selector.BooleanSelector()
                }
            ),
        )

    async def async_step_electric_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add an electric rate period."""
        return await self._async_period_form("ELECTRIC", "electric_add", user_input)

    async def async_step_gas_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a gas rate period."""
        return await self._async_period_form("GAS", "gas_add", user_input)

    async def async_step_electric_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an electric rate period to edit."""
        return await self._async_select_edit_period("ELECTRIC", user_input)

    async def async_step_gas_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a gas rate period to edit."""
        return await self._async_select_edit_period("GAS", user_input)

    async def _async_select_edit_period(
        self,
        service_type: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Select an existing period to edit."""
        step_id = f"{service_type.lower()}_edit"
        if user_input is not None:
            selected_date = user_input[CONF_EFFECTIVE_DATE]
            self._edit_dates[service_type] = selected_date
            return await self._async_period_form(
                service_type,
                f"{step_id}_period",
                None,
                original_date=selected_date,
            )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self._period_selection_schema(service_type),
        )

    async def async_step_electric_edit_period(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit an electric rate period."""
        return await self._async_period_form(
            "ELECTRIC",
            "electric_edit_period",
            user_input,
            original_date=self._edit_dates["ELECTRIC"],
        )

    async def async_step_gas_edit_period(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit a gas rate period."""
        return await self._async_period_form(
            "GAS",
            "gas_edit_period",
            user_input,
            original_date=self._edit_dates["GAS"],
        )

    async def _async_period_form(
        self,
        service_type: str,
        step_id: str,
        user_input: dict[str, Any] | None,
        *,
        original_date: str | None = None,
    ) -> ConfigFlowResult:
        """Add or edit one rate period."""
        rates = list(self._service_config(service_type).get(CONF_RATES, []))
        existing = next(
            (
                period
                for period in rates
                if period[CONF_EFFECTIVE_DATE] == original_date
            ),
            None,
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            effective_date = self._date_string(user_input[CONF_EFFECTIVE_DATE])
            no_rate = bool(user_input["no_rate_boundary"])
            rate: str | None = None
            if not no_rate:
                try:
                    parsed_rate = Decimal(user_input.get(CONF_RATE, ""))
                except (InvalidOperation, TypeError, ValueError):
                    errors[CONF_RATE] = "invalid_rate"
                else:
                    if not parsed_rate.is_finite() or parsed_rate < 0:
                        errors[CONF_RATE] = "invalid_rate"
                    else:
                        rate = format(parsed_rate, "f")

            if not errors:
                if original_date is not None:
                    rates = [
                        period
                        for period in rates
                        if period[CONF_EFFECTIVE_DATE] != original_date
                    ]
                self._service_config(service_type)[CONF_RATES] = self._upsert_period(
                    rates,
                    {
                        CONF_EFFECTIVE_DATE: effective_date,
                        CONF_RATE: rate,
                    },
                )
                return self._show_service_menu(service_type)

        defaults = {
            CONF_EFFECTIVE_DATE: (
                existing[CONF_EFFECTIVE_DATE]
                if existing
                else dt_util.now().date().isoformat()
            ),
            CONF_RATE: (
                existing[CONF_RATE]
                if existing and existing[CONF_RATE] is not None
                else ""
            ),
            "no_rate_boundary": bool(existing and existing[CONF_RATE] is None),
        }
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_EFFECTIVE_DATE): selector.DateSelector(),
                        vol.Optional(CONF_RATE): selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.NUMBER
                            )
                        ),
                        vol.Required("no_rate_boundary"): selector.BooleanSelector(),
                    }
                ),
                user_input or defaults,
            ),
            errors=errors,
        )

    async def async_step_electric_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete an electric rate period."""
        return await self._async_delete_period("ELECTRIC", user_input)

    async def async_step_gas_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete a gas rate period."""
        return await self._async_delete_period("GAS", user_input)

    async def _async_delete_period(
        self,
        service_type: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Delete an existing rate period."""
        step_id = f"{service_type.lower()}_delete"
        if user_input is not None:
            selected_date = user_input[CONF_EFFECTIVE_DATE]
            service_config = self._service_config(service_type)
            service_config[CONF_RATES] = [
                period
                for period in service_config.get(CONF_RATES, [])
                if period[CONF_EFFECTIVE_DATE] != selected_date
            ]
            return self._show_service_menu(service_type)
        return self.async_show_form(
            step_id=step_id,
            data_schema=self._period_selection_schema(service_type),
        )

    def _period_selection_schema(self, service_type: str) -> vol.Schema:
        """Build a selector for existing effective dates."""
        rates = self._service_config(service_type).get(CONF_RATES, [])
        return vol.Schema(
            {
                vol.Required(CONF_EFFECTIVE_DATE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=period[CONF_EFFECTIVE_DATE],
                                label=self._format_period(period),
                            )
                            for period in rates
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )

    def _discovered_services(self) -> list[str]:
        """Return service types discovered by the coordinator."""
        runtime_data = self.config_entry.runtime_data
        meters = getattr(runtime_data, "meters", {})
        services = {
            meter["serviceType"]
            for meter in meters.values()
            if meter.get("serviceType") in SUPPORTED_METER_TYPES
        }

        if services:
            return sorted(services)

        configured = self._cost_tracking()
        services = set(configured).intersection(SUPPORTED_METER_TYPES)
        return sorted(services or SUPPORTED_METER_TYPES)

    @staticmethod
    def _upsert_period(
        rates: list[dict[str, Any]],
        period: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Insert or replace a rate period by effective date."""
        updated = [
            existing
            for existing in rates
            if existing[CONF_EFFECTIVE_DATE] != period[CONF_EFFECTIVE_DATE]
        ]
        updated.append(period)
        return sorted(updated, key=lambda item: item[CONF_EFFECTIVE_DATE])

    @staticmethod
    def _date_string(value: str | date) -> str:
        """Normalize a date selector value."""
        return value.isoformat() if isinstance(value, date) else value

    @staticmethod
    def _format_period(period: dict[str, Any]) -> str:
        """Format one rate period for display."""
        rate = period.get(CONF_RATE)
        return (
            f"{period[CONF_EFFECTIVE_DATE]} — {rate if rate is not None else 'No rate'}"
        )

    @classmethod
    def _format_rate_history(cls, rates: list[dict[str, Any]]) -> str:
        """Format rate history for a menu description."""
        if not rates:
            return "No rate periods configured."
        return "\n".join(
            cls._format_period(period)
            for period in sorted(
                rates,
                key=lambda item: item[CONF_EFFECTIVE_DATE],
            )
        )
