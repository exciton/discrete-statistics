"""What to call an entity, in a title or on a chart.

Not pure - it reads the entity registry and the state machine - which is why
it is its own module rather than living in `config`.
"""

from __future__ import annotations

from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


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
