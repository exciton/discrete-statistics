"""What to call an entity, and what to call the states it is in.

Not pure - it reads the entity registry, the state machine and the
translation cache - which is why it is its own module rather than living in
`config`. It needs `hass` but never the recorder, which is what separates it
from `compiler`.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import (
    async_get_translations,
    async_translate_state,
)

from .const import NO_DATA

# States `async_translate_state` cannot render. It returns `unavailable` and
# `unknown` untouched (translation.py:469) because the frontend renders those
# from its own `state.default` strings, which the backend never sees; and
# `no_data` is ours, so nothing has a translation for it. Without these a
# legend reads `unavailable` and `no_data` beside a rendered `Closed`.
# English only, and only for the states nothing else can name.
_UNRENDERED_STATES = {
    STATE_UNAVAILABLE: "Unavailable",
    STATE_UNKNOWN: "Unknown",
    NO_DATA: "No Data",
}


def display_name(
    hass: HomeAssistant, entity_id: str, name: str | None = None
) -> str:
    """A typed name, else the entity's own, else its ID.

    The registry is consulted at all because attributes are stripped while an
    entity is unavailable, and falling back to the ID there would rename
    everything and rename it back when it returned. It is consulted *first*
    because it holds what the user asked for: the two disagree only after a
    rename the integration has not yet republished.
    """
    if name:
        return name
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and (resolved := entry.name or entry.original_name):
        return resolved
    state = hass.states.get(entity_id)
    if state is not None and (resolved := state.attributes.get(ATTR_FRIENDLY_NAME)):
        return resolved
    return entity_id


def describe(hass: HomeAssistant, entity_id: str, name: str | None = None) -> str:
    """Name an entity for a reader, with its ID so there is no doubt which.

    The name is what they typed or what the entity calls itself; the ID is
    what they search for in the logs and in Settings > Statistics. An entity
    with no name of its own has nothing to distinguish, so the ID stands
    alone rather than being printed twice.
    """
    label = display_name(hass, entity_id, name)
    return f"{label} ({entity_id})" if label != entity_id else entity_id


def state_translator(hass: HomeAssistant, entity_id: str) -> Callable[[str], str]:
    """Render canonical states the way Home Assistant renders them.

    A `binary_sensor` with `device_class: door` reads Open/Closed everywhere
    else in the UI, so a chart legend saying on/off looks wrong.
    `async_translate_state` returns the raw state when there is no
    translation, which covers our own `no_data` and any enum sensor without
    one.

    LIMITATION: `hass.config.language` is instance-wide, while the frontend
    translates per viewing user. The name is one stored string with no viewer
    in scope, so everyone sees the instance language.
    """
    entry = er.async_get(hass).async_get(entity_id)
    domain = entity_id.partition(".")[0]
    device_class = entry.device_class or entry.original_device_class if entry else None
    if device_class is None and (state := hass.states.get(entity_id)) is not None:
        device_class = state.attributes.get(ATTR_DEVICE_CLASS)

    def translate(state: str) -> str:
        if (rendered := _UNRENDERED_STATES.get(state)) is not None:
            return rendered
        return async_translate_state(
            hass,
            state,
            domain,
            entry.platform if entry else None,
            entry.translation_key if entry else None,
            device_class,
        )

    return translate


async def async_warm_state_translations(
    hass: HomeAssistant, entity_id: str
) -> None:
    """Load what `state_translator` reads from the cache.

    `async_translate_state` is a callback over a cache and answers with the
    raw state when it is cold - so without this the same statistic could be
    named `Closed` on one compile and `closed` on the next, rewriting its
    metadata each time.

    Not covered by a test: setting a component up loads its translations, so
    any test that makes a translation resolvable has already warmed the
    cache. This is reasoning, not evidence.
    """
    language = hass.config.language
    await async_get_translations(hass, language, "entity_component")
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.translation_key:
        await async_get_translations(hass, language, "entity", {entry.platform})
