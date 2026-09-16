# Duke Energy Custom Integration

A custom component to sync historical electricity usage for Duke Energy customers.

## Why?

In November 2025, Duke Energy migrated their API authentication to use Auth0 which broke the existing core integration.
Duke's Auth0 flow only accepts the mobile app's redirect URI, so the browser cannot be sent back to Home Assistant
automatically the way a normal OAuth integration works. Rather, this signin path leads on an empty callback page. The
URL of the empty callback page is pasted into configuration flow. Because of the extensive and limited configuration
options, it was decided that this integration would be better served as a custom integration than to try and put it back
in core.

## Install

> [!NOTE]
> Earlier versions of this integration required use of a custom Chrome extension for initial configuration. That is no
> longer required. If you still have the `Duke Energy OAuth Helper` extension installed, it can be safely disabled or
> removed.

1. Add [this repository](https://my.home-assistant.io/redirect/hacs_repository/?owner=hunterjm&repository=ha-dukeenergy&category=integration)
   to HACS and install.
2. Restart Home Assistant.
3. If you already had the core integration installed, it should prompt you to re-authenticate. Otherwise, add the
   integration from Devices and Services.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hunterjm&repository=ha-dukeenergy&category=integration)
