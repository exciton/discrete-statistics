"""Where a sensor belongs, and what to call it there."""

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discrete_statistics.naming import entity_naming


async def with_device(hass, entity_id, device_name):
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", device_name)},
        name=device_name,
    )
    domain, object_id = entity_id.split(".")
    er.async_get(hass).async_get_or_create(
        domain,
        "test",
        entity_id,
        suggested_object_id=object_id,
        device_id=device.id,
    )


async def test_naming_uses_the_typed_name_verbatim(hass):
    # A typed name is what the person asked to see, device or no device.
    naming = entity_naming(hass, "light.kitchen", "ignored title", "My sensor")
    assert naming.has_entity_name is False
    assert naming.name == "My sensor"


async def test_naming_without_a_device_keeps_the_whole_title(hass):
    # has_entity_name with no device would render the part alone — "count
    # this month", with nothing saying what of.
    naming = entity_naming(hass, "light.no_device", "Kitchen count this month", None)
    assert naming.device is None
    assert naming.has_entity_name is False
    assert naming.name == "Kitchen count this month"


async def test_naming_on_a_device_is_relative_to_it(hass):
    # Device "Kitchen Lights": stripped from the title's front, leaving a
    # lowercase remainder that Home Assistant's own function capitalizes.
    await with_device(hass, "light.kitchen", "Kitchen Lights")
    naming = entity_naming(
        hass, "light.kitchen", "Kitchen Lights count this month", None
    )
    assert naming.device is not None
    assert naming.has_entity_name is True
    assert naming.name == "Count this month"


async def test_naming_keeps_what_the_device_does_not_cover(hass):
    # Device "Kitchen Lights", entity "Kitchen Lights Left".
    await with_device(hass, "light.left", "Kitchen Lights")
    naming = entity_naming(
        hass, "light.left", "Kitchen Lights Left count this month", None
    )
    assert naming.name == "Left count this month"


async def test_naming_leaves_a_title_the_device_does_not_prefix(hass):
    # Device "Kitchen", entity named "Utility Lights": no meaningful
    # prefix, so nothing is stripped and the title stands.
    await with_device(hass, "light.utility", "Kitchen")
    naming = entity_naming(
        hass, "light.utility", "Utility Lights count this month", None
    )
    assert naming.name == "Utility Lights count this month"


async def test_two_sensors_that_strip_alike_share_a_name(hass):
    # Documented, not prevented: their entity ids and unique ids differ,
    # and Home Assistant does the same for two entities named alike on one
    # device.
    await with_device(hass, "light.one", "Shed")
    await with_device(hass, "light.two", "Shed")
    first = entity_naming(hass, "light.one", "Shed count this month", None)
    second = entity_naming(hass, "light.two", "Shed count this month", None)
    assert first.name == second.name == "Count this month"
