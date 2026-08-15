"""Tests for the Duke Energy manual authentication config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiodukeenergy import AuthorizationResult, AuthorizationTransaction
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import AbortFlow

from custom_components.duke_energy.config_flow import DukeEnergyConfigFlow
from custom_components.duke_energy.const import DOMAIN

pytestmark = pytest.mark.asyncio

CALLBACK_URL = "https://login.duke-energy.com/ios/com.duke-energy.app/callback?code=code&state=state"
TRANSACTION = AuthorizationTransaction(
    authorize_url="https://login.duke-energy.com/authorize?state=state",
    state="state",
    nonce="nonce",
    code_verifier="verifier",
)
TOKEN = {
    "access_token": "access",
    "refresh_token": "refresh",
    "id_token": "id-token",
    "expires_in": 1800,
}


def make_flow(source: str = SOURCE_USER) -> DukeEnergyConfigFlow:
    """Create a flow without a Home Assistant runtime."""
    flow = DukeEnergyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": source}
    return flow


async def test_login_link_and_callback_form() -> None:
    """The two manual handoff forms expose the expected URL and input."""
    flow = make_flow()
    with (
        patch(
            "custom_components.duke_energy.config_flow."
            "aiohttp_client.async_get_clientsession"
        ),
        patch(
            "custom_components.duke_energy.config_flow.Auth0Client."
            "create_authorization_transaction",
            return_value=TRANSACTION,
        ),
    ):
        result = await flow.async_step_user()
        assert result["step_id"] == "user"
        assert result["description_placeholders"]["authorization_url"]
        result = await flow.async_step_user({})

    assert result["step_id"] == "callback_url"


async def test_successful_manual_callback() -> None:
    """Verified claims create an OAuth-backed entry without transient data."""
    flow = make_flow()
    flow._auth_transaction = TRANSACTION
    authorization = AuthorizationResult(
        token=TOKEN,
        id_token_claims={
            "internal_identifier": "DUKE_USER",
            "email": "user@example.com",
        },
    )
    with (
        patch(
            "custom_components.duke_energy.config_flow."
            "aiohttp_client.async_get_clientsession"
        ),
        patch(
            "custom_components.duke_energy.config_flow.Auth0Client."
            "complete_authorization",
            AsyncMock(return_value=authorization),
        ),
        patch(
            "custom_components.duke_energy.config_flow."
            "DukeEnergyOAuth2Implementation.adjust_token_expiry",
            return_value=TOKEN,
        ),
        patch.object(flow, "async_set_unique_id", AsyncMock()),
        patch.object(flow, "_abort_if_unique_id_configured"),
    ):
        result = await flow.async_step_callback_url({"callback_url": CALLBACK_URL})

    assert result["type"] == "create_entry"
    assert result["title"] == "user@example.com"
    assert result["data"] == {
        "auth_implementation": DOMAIN,
        "token": TOKEN,
    }
    assert "code_verifier" not in result["data"]
    assert "state" not in result["data"]
    assert "nonce" not in result["data"]
    assert flow._auth_transaction is None


async def test_reauth_identity_mismatch() -> None:
    """Reauthentication rejects a different verified Duke identity."""
    flow = make_flow(SOURCE_REAUTH)
    flow._auth_transaction = TRANSACTION
    authorization = AuthorizationResult(
        token=TOKEN,
        id_token_claims={
            "internal_identifier": "ANOTHER_USER",
            "email": "other@example.com",
        },
    )
    with (
        patch(
            "custom_components.duke_energy.config_flow."
            "aiohttp_client.async_get_clientsession"
        ),
        patch(
            "custom_components.duke_energy.config_flow.Auth0Client."
            "complete_authorization",
            AsyncMock(return_value=authorization),
        ),
        patch(
            "custom_components.duke_energy.config_flow."
            "DukeEnergyOAuth2Implementation.adjust_token_expiry",
            return_value=TOKEN,
        ),
        patch.object(flow, "async_set_unique_id", AsyncMock()),
        patch.object(
            flow,
            "_abort_if_unique_id_mismatch",
            side_effect=AbortFlow("wrong_account"),
        ),
        pytest.raises(AbortFlow, match="wrong_account"),
    ):
        await flow.async_step_callback_url({"callback_url": CALLBACK_URL})


async def test_reconfigure_updates_credentials() -> None:
    """A user-initiated reconfigure replaces credentials and reloads the entry."""
    flow = make_flow(SOURCE_RECONFIGURE)
    flow._auth_transaction = TRANSACTION
    authorization = AuthorizationResult(
        token=TOKEN,
        id_token_claims={
            "internal_identifier": "DUKE_USER",
            "email": "user@example.com",
        },
    )
    expected = {"type": "abort", "reason": "reconfigure_successful"}
    entry = MagicMock()
    with (
        patch(
            "custom_components.duke_energy.config_flow."
            "aiohttp_client.async_get_clientsession"
        ),
        patch(
            "custom_components.duke_energy.config_flow.Auth0Client."
            "complete_authorization",
            AsyncMock(return_value=authorization),
        ),
        patch(
            "custom_components.duke_energy.config_flow."
            "DukeEnergyOAuth2Implementation.adjust_token_expiry",
            return_value=TOKEN,
        ),
        patch.object(flow, "async_set_unique_id", AsyncMock()),
        patch.object(flow, "_abort_if_unique_id_mismatch") as identity_check,
        patch.object(flow, "_get_reconfigure_entry", return_value=entry),
        patch.object(
            flow, "async_update_reload_and_abort", return_value=expected
        ) as update,
    ):
        result = await flow.async_step_callback_url({"callback_url": CALLBACK_URL})

    assert result == expected
    identity_check.assert_called_once_with(reason="wrong_account")
    update.assert_called_once_with(
        entry,
        data_updates={"auth_implementation": DOMAIN, "token": TOKEN},
    )


async def test_interrupted_flow_requires_restart() -> None:
    """Transient PKCE data is not reconstructed after interruption."""
    flow = make_flow()
    result = await flow.async_step_callback_url({"callback_url": CALLBACK_URL})
    assert result["type"] == "abort"
    assert result["reason"] == "authentication_restarted"
