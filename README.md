# Duke Energy Custom Integration

A custom component to sync historical electricity usage for Duke Energy customers.

## Why?

In November 2025, Duke Energy migrated their API authentication to use Auth0 which broke the existing core integration. In order to get around this, we needed to build a custom chrome extension that captured the OAuth callback from the mobile app flow to restore functionality. Because of the extensive and limited configuration options, it was decided that this integration would be better served as a custom integration than to try and put it back in core.

## Install

> [!IMPORTANT]
> All steps below must be performed in the Google Chrome browser on a desktop.
> The chrome extension is required to successfully authenticate with Duke Energy. Do not skip this step!

1. Download the latest chrome extension from the aiodukeenergy release page [here](https://github.com/hunterjm/aiodukeenergy/releases/latest/download/chrome-extension.zip).
2. Extract the folder.
3. In Google Chrome, visit [chrome://extensions/](chrome://extensions/).
4. Enable `Developer mode` in the top right.
5. Click `Load unpacked` and select the extracted extension.
6. Add [this repository](https://my.home-assistant.io/redirect/hacs_repository/?owner=hunterjm&repository=ha-dukeenergy&category=integration) to HACS and install.
7. Restart Home Assistant
8. If you already had the core integration installed, it should prompt you to re-authenticate. Otherwise, add the integration from Devices and Services.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hunterjm&repository=ha-dukeenergy&category=integration)
## Energy cost tracking

The integration publishes hourly consumption as an external statistic (`duke_energy:electric_<serial>_energy_consumption`). Because it is an external statistic, Home Assistant's Energy dashboard will not accept a static price on the grid source (`Use stat_cost instead`). To track dollars, the integration can publish a paired cost statistic (`duke_energy:electric_<serial>_energy_cost`) that you select under **"Use an entity tracking the total costs"** in the Energy dashboard grid source.

Duke Energy's API does not expose billed cost, so the cost is estimated from your usage and a price you configure. Open the integration's **Configure** dialog, pick the meter to set up (if you have more than one, each is priced independently), and choose a price source:

- **Fixed price per kWh** — a single all-in rate (derive it from a bill: total charges ÷ kWh). Tracks real bills within a couple percent for flat-rate plans.
- **Track a price entity (dynamic)** — an entity whose state is the current price in $/kWh, e.g. an `input_number` you update when rates change, or a template sensor implementing time-of-use or seasonal rates. Backfilled hours are priced from the entity's long-term statistics when available (enable `state_class: measurement` on a template sensor to record them), falling back to the entity's current state for hours without recorded history.

Optionally add a **fixed monthly charge** (basic customer charge); it is spread evenly across the hours of each month.

Changing these options reloads the integration, which rebuilds the statistics from scratch — cost history (up to ~3 years) is recomputed with the new settings.
