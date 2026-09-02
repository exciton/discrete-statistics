"""Construction and matching of external statistic IDs.

An ID is three parts joined by underscores:

    discrete_statistics:<entity slug>_<state token>_<metric>

The state is slugified with *no* separator, so it is always exactly one
token. That is what makes the ID readable from the right - the last token is
the metric, the second-to-last is the state, and everything before is the
entity. Without it `climate.kitchen` in state `heat_cool` and
`climate.kitchen_heat` in state `cool` produce the same ID and write to the
same series.

The state token is lossy on purpose: `heat_cool` and `heatcool` collapse
together and are recorded as one statistic, which behaves like a free state
mapping. The readable state survives in the metadata name.
"""

from __future__ import annotations

import re

from homeassistant.util import slugify

from .const import DOMAIN, METRIC_COUNT, METRIC_DURATION

# Copied from homeassistant.components.recorder.statistics so that an
# upstream change surfaces here as a test failure rather than a runtime
# HomeAssistantError from the recorder.
VALID_STATISTIC_ID = re.compile(
    r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$"
)

METRICS = (METRIC_DURATION, METRIC_COUNT)


class InvalidStatisticIdError(ValueError):
    """Raised when the parts do not produce a valid statistic ID."""


def state_token(state: str) -> str:
    """Return the single-token form of a state, as it appears in an ID.

    Two states with the same token are the same statistic. Callers that
    build payloads must fold their buckets by this, not by the raw state,
    or one state's seconds would overwrite the other's instead of adding
    to them - and the durations would stop summing to wall-clock time.
    """
    return slugify(state, separator="")


def build(entity_id: str, state: str, metric: str) -> str:
    """Return the external statistic ID for an entity/state/metric triple."""
    # Detect states that tokenise to "unknown", which would collide with a
    # genuine "unknown" state. slugify returns the literal string "unknown"
    # for inputs that don't contain any sluggable characters.
    token = state_token(state)
    if token == "unknown" and state.strip().lower() != "unknown":
        raise InvalidStatisticIdError(
            f"State {state!r} does not slugify to anything distinguishable "
            f"(would collide with the 'unknown' state's statistic ID)"
        )

    object_id = f"{slugify(entity_id, separator='_')}_{token}_{metric}"
    statistic_id = f"{DOMAIN}:{object_id}"
    if not VALID_STATISTIC_ID.match(statistic_id):
        raise InvalidStatisticIdError(
            f"Cannot build a valid statistic ID from entity_id={entity_id!r}, "
            f"state={state!r}, metric={metric!r} (got {statistic_id!r})"
        )
    return statistic_id


def parse(statistic_id: str) -> tuple[str, str, str] | None:
    """Return (entity_slug, state_token, metric), or None if it is not ours.

    Reads from the right, which is only unambiguous because the state is a
    single token. A None result means the ID was not built by `build` -
    it has been renamed by hand in Settings > System > Tools > Statistics,
    or it predates this scheme.
    """
    domain, _, object_id = statistic_id.partition(":")
    if domain != DOMAIN or object_id.count("_") < 2:
        return None
    entity_slug, token, metric = object_id.rsplit("_", 2)
    if metric not in METRICS or not entity_slug or not token:
        return None
    return entity_slug, token, metric


def belongs_to(statistic_id: str, entity_id: str) -> bool:
    """True when this ID was built for this entity."""
    if (parts := parse(statistic_id)) is None:
        return False
    return parts[0] == slugify(entity_id, separator="_")
