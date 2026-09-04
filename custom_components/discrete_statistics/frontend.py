"""Serve the Lovelace card that ships inside the integration.

The built card is committed at `frontend/discrete-statistics-card.js`. It
is registered as a frontend module URL rather than a dashboard resource,
so it loads on every dashboard with nothing for the user to add. The
version is appended to the URL so browsers drop the cached card on
upgrade.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

CARD_URL_PATH = f"/{DOMAIN}/discrete-statistics-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "discrete-statistics-card.js"


async def async_register(hass: HomeAssistant, version: str) -> None:
    """Register the card's static path and module URL.

    Nothing to do without the frontend: the module URL set only exists once
    it has loaded, and a headless instance has no dashboards to draw on.
    """
    if "frontend" not in hass.config.components:
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_PATH, str(CARD_FILE), True)]
    )
    add_extra_js_url(hass, f"{CARD_URL_PATH}?v={version}")
