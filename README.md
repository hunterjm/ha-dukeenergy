# Duke Energy Custom Integration

A custom component to sync historical electricity usage for Duke Energy customers.

## Why?

In November 2025, Duke Energy migrated its API authentication to Auth0, which broke the existing core integration. This custom integration uses Duke Energy's browser login and a manual callback handoff while retaining OAuth PKCE and refresh-token authentication.

## Install

1. Add this repository to HACS and install the integration.
2. Restart Home Assistant.
3. Add Duke Energy from **Settings > Devices & services**. Existing entries will prompt for reauthentication when needed.
4. Select the Duke Energy login link shown by Home Assistant and sign in using a normal browser.
5. After a successful login, Duke Energy redirects to a 404 page. This is expected.
6. Copy the complete URL from that page's browser address bar and paste it into Home Assistant.

No Chrome extension is required. Home Assistant stores the resulting OAuth tokens and refreshes them automatically until Duke Energy invalidates the refresh token.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hunterjm&repository=ha-dukeenergy&category=integration)
