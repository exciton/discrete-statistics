"""Construction of external statistic IDs.

IDs are write-only: they are never parsed back into their parts. The
registry in storage.py owns the reverse mapping.
"""

from __future__ import annotations

import re

from homeassistant.util import slugify

from .const import DOMAIN

# Copied from homeassistant.components.recorder.statistics so that an
# upstream change surfaces here as a test failure rather than a runtime
# HomeAssistantError from the recorder.
VALID_STATISTIC_ID = re.compile(
    r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$"
)


class InvalidStatisticIdError(ValueError):
    """Raised when the parts do not produce a valid statistic ID."""


def build(entity_id: str, state: str, metric: str) -> str:
    """Return the external statistic ID for an entity/state/metric triple.

    slugify collapses runs of separators, so no combination of parts can
    produce the double underscore that VALID_STATISTIC_ID forbids.
    """
    # Validate that state contains at least one alphanumeric character
    if not re.search(r"[a-z0-9_]", state, re.IGNORECASE):
        raise InvalidStatisticIdError(
            f"Cannot build a valid statistic ID from entity_id={entity_id!r}, "
            f"state={state!r}, metric={metric!r} (state contains no alphanumeric characters)"
        )

    object_id = slugify(f"{entity_id} {state} {metric}", separator="_")
    statistic_id = f"{DOMAIN}:{object_id}"
    if not VALID_STATISTIC_ID.match(statistic_id):
        raise InvalidStatisticIdError(
            f"Cannot build a valid statistic ID from entity_id={entity_id!r}, "
            f"state={state!r}, metric={metric!r} (got {statistic_id!r})"
        )
    return statistic_id
