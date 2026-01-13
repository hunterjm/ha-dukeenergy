"""OAuth2 implementation for Duke Energy."""

from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

import jwt
from homeassistant.helpers.config_entry_oauth2_flow import (
    LocalOAuth2ImplementationWithPkce,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .const import (
    AUTH0_CLIENT,
    MOBILE_REDIRECT_URI,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_SCOPES,
    OAUTH2_TOKEN,
)

_LOGGER = logging.getLogger(__name__)


class DukeEnergyOAuth2Implementation(LocalOAuth2ImplementationWithPkce):
    """Duke Energy OAuth2 implementation using mobile app credentials."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the Duke Energy OAuth2 implementation."""
        super().__init__(
            hass,
            domain="duke_energy",
            client_id=OAUTH2_CLIENT_ID,
            client_secret="",  # PKCE flow doesn't need a client secret
            authorize_url=OAUTH2_AUTHORIZE,
            token_url=OAUTH2_TOKEN,
        )

    @property
    def name(self) -> str:
        """Return the name of the implementation."""
        return "Duke Energy"

    @property
    def redirect_uri(self) -> str:
        """Return the redirect URI for Duke Energy mobile app OAuth flow."""
        return MOBILE_REDIRECT_URI

    @property
    def extra_authorize_data(self) -> dict:
        """Extra data for the authorize request."""
        data = {
            "scope": " ".join(OAUTH2_SCOPES),
            "auth0Client": AUTH0_CLIENT,
            "nonce": secrets.token_urlsafe(32),
        }
        data.update(super().extra_authorize_data)
        return data

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Resolve external data to tokens, adjusting expiry for id_token."""
        token = await super().async_resolve_external_data(external_data)
        return self._adjust_token_expiry(token)

    async def async_refresh_token(self, token: dict) -> dict:
        """Refresh tokens, adjusting expiry for id_token."""
        new_token = await super().async_refresh_token(token)
        return self._adjust_token_expiry(new_token)

    def _adjust_token_expiry(self, token: dict) -> dict:
        """
        Adjust expires_at/expires_in based on id_token's exp claim.

        Duke Energy's id_token expires much sooner (30 min) than the access_token
        (24 hours). Since we need the id_token to exchange for Duke Energy API
        tokens, we must refresh before the id_token expires.

        Raises:
            ValueError: If id_token is missing or cannot be decoded.

        """
        id_token = token.get("id_token")
        if not id_token:
            msg = "No id_token in token response"
            raise ValueError(msg)

        try:
            payload = jwt.decode(id_token, options={"verify_signature": False})
        except jwt.DecodeError as err:
            msg = f"Failed to decode id_token: {err}"
            raise ValueError(msg) from err

        exp = payload.get("exp")
        if not exp:
            msg = "No exp claim in id_token"
            raise ValueError(msg)

        # Set expires_at to the id_token's expiry time
        token["expires_at"] = float(exp)
        # Compute expires_in from current time
        token["expires_in"] = int(exp - time.time())

        _LOGGER.debug(
            "Adjusted token expiry to id_token exp: expires_in=%s seconds",
            token["expires_in"],
        )

        return token
