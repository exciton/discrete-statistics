"""The card is served by the integration, and registered as a module."""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.components.http import StaticPathConfig

from custom_components.discrete_statistics import frontend
from custom_components.discrete_statistics.const import DOMAIN


def _hass_with_frontend() -> MagicMock:
    hass = MagicMock()
    hass.config.components = {"frontend", "http"}
    hass.data = {DATA_EXTRA_MODULE_URL: set()}
    hass.http.async_register_static_paths = AsyncMock()
    return hass


async def test_registers_the_built_file_and_a_versioned_module_url():
    hass = _hass_with_frontend()

    await frontend.async_register(hass, version="1.2.3")

    hass.http.async_register_static_paths.assert_awaited_once()
    (configs,) = hass.http.async_register_static_paths.await_args.args
    assert configs == [
        StaticPathConfig(frontend.CARD_URL_PATH, str(frontend.CARD_FILE), True)
    ]
    assert hass.data[DATA_EXTRA_MODULE_URL] == {f"{frontend.CARD_URL_PATH}?v=1.2.3"}


async def test_the_built_file_is_shipped():
    assert frontend.CARD_FILE.is_file()
    assert frontend.CARD_URL_PATH.startswith(f"/{DOMAIN}/")


async def test_skipped_when_frontend_is_not_loaded():
    hass = _hass_with_frontend()
    hass.config.components = {"http"}

    await frontend.async_register(hass, version="1.2.3")

    hass.http.async_register_static_paths.assert_not_awaited()
    assert hass.data[DATA_EXTRA_MODULE_URL] == set()
