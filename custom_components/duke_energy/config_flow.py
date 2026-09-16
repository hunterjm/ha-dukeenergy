"""Config flow for Duke Energy integration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import jwt
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

from .const import DOMAIN
from .oauth import DukeEnergyOAuth2Implementation

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK_URL = "callback_url"

# Translation keys under `config.error` in strings.json
ERROR_INVALID_CALLBACK_URL = "invalid_callback_url"
ERROR_AUTHORIZE_REJECTED = "authorize_rejected"
ERROR_STATE_MISMATCH = "state_mismatch"

STEP_AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CALLBACK_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        )
    }
)


class InvalidCallbackURLError(Exception):
    """Raised when the pasted callback URL cannot be used."""

    def __init__(self, error_key: str) -> None:
        """Initialize with the translation key describing the problem."""
        super().__init__(error_key)
        self.error_key = error_key


class DukeEnergyOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle a config flow for Duke Energy."""

    VERSION = 2
    MINOR_VERSION = 1

    DOMAIN = DOMAIN

    def __init__(self) -> None:
        """Initialize the Duke Energy config flow."""
        super().__init__()
        self._authorize_url: str | None = None
        self._expected_state: str | None = None
        self._redirect_uri: str | None = None

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    async def async_step_pick_implementation(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle picking implementation - directly use our implementation."""
        implementation = DukeEnergyOAuth2Implementation(self.hass)
        self.flow_impl = implementation
        self._redirect_uri = implementation.redirect_uri
        return await self.async_step_auth()

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Link the account by pasting the Duke Energy callback URL.

        Duke Energy's Auth0 flow only accepts the mobile app redirect URI, so the
        browser cannot be sent back to Home Assistant automatically. Instead the user
        signs in, lands on the (empty) callback page, and pastes that address back
        here so the authorization code can be exchanged for tokens.
        """
        if self._authorize_url is None:
            try:
                async with asyncio.timeout(
                    config_entry_oauth2_flow.OAUTH_AUTHORIZE_URL_TIMEOUT_SEC
                ):
                    self._authorize_url = await self.async_generate_authorize_url()
            except TimeoutError:
                _LOGGER.exception("Timeout generating authorize url")
                return self.async_abort(reason="authorize_url_timeout")
            self._expected_state = URL(self._authorize_url).query.get("state")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self.external_data = self._parse_callback_url(
                    user_input[CONF_CALLBACK_URL]
                )
            except InvalidCallbackURLError as err:
                errors["base"] = err.error_key
            else:
                return await self.async_step_creation()

        return self.async_show_form(
            step_id="auth",
            data_schema=STEP_AUTH_SCHEMA,
            description_placeholders={"authorize_url": self._authorize_url},
            errors=errors,
        )

    def _parse_callback_url(self, raw_url: str) -> dict[str, Any]:
        """
        Turn the pasted callback URL into OAuth2 external data.

        The returned mapping mirrors what Home Assistant's OAuth2 callback view would
        normally hand to the flow: the authorization code plus the already decoded
        state, which the implementation reads the redirect URI back out of.

        Raises:
            InvalidCallbackURLError: If the URL is unusable, is missing the
                authorization code, or does not carry the state we issued.

        """
        try:
            query = URL(raw_url.strip()).query
        except ValueError as err:
            raise InvalidCallbackURLError(ERROR_INVALID_CALLBACK_URL) from err

        if "error" in query:
            raise InvalidCallbackURLError(ERROR_AUTHORIZE_REJECTED)

        if not (code := query.get("code")):
            raise InvalidCallbackURLError(ERROR_INVALID_CALLBACK_URL)

        if not self._expected_state or query.get("state") != self._expected_state:
            raise InvalidCallbackURLError(ERROR_STATE_MISMATCH)

        return {
            "code": code,
            "state": {
                "flow_id": self.flow_id,
                "redirect_uri": self._redirect_uri,
            },
        }

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
