"""Construction and matching of external statistic IDs.

An ID is three parts joined by underscores:

    discrete_statistics:<entity slug>_<state token>_<metric>

The state is slugified with *no* separator, so it is always exactly one
token, and the ID reads back from the right. Without that,
`climate.zone`/`heat_cool` and `climate.zone_heat`/`cool` produce the same
ID and write to the same series.

The token is lossy on purpose - `heat_cool` and `heatcool` become one
statistic, which behaves like a free state mapping - and the readable state
survives in the metadata name.
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

    Two states with the same token are the same statistic, so callers must
    fold their buckets by this rather than by the raw state - otherwise one
    state's seconds overwrite the other's instead of adding.
    """
    return slugify(state, separator="")


def is_blank(state: str) -> bool:
    """True when a state has no name to put in a statistic ID.

    `state_token` keeps only letters and digits, so this is exactly the set
    with none of either: empty - what the recorder stores as NULL when an
    entity is removed or reloaded, and the history API hands back as "" - or
    whitespace, or punctuation alone.

    It also catches the rare state that slugifies onto the literal "unknown"
    without being it. That is a collision rather than a blank, but it wants
    the same treatment: there is no name here a statistic could carry.

    Callers substitute `cfg.blank` for these before resolving, so they
    inherit a real state's disposition rather than needing a case of their own.
    """
    token = state_token(state)
    if not token:
        return True
    return token == "unknown" and state.strip().lower() != "unknown"


def build(entity_id: str, state: str, metric: str) -> str:
    """Return the external statistic ID for an entity/state/metric triple."""
    if is_blank(state):
        raise InvalidStatisticIdError(
            f"State {state!r} has no name to build an ID from; callers "
            f"should substitute cfg.blank before reaching here"
        )

    token = state_token(state)
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

    Unambiguous only because the state is a single token. None means the ID
    was not built by `build`: renamed by hand, or from an older scheme.
    """
    domain, _, object_id = statistic_id.partition(":")
    if domain != DOMAIN or object_id.count("_") < 2:
        return None
    entity_slug, token, metric = object_id.rsplit("_", 2)
    if metric not in METRICS or not entity_slug or not token:
        return None
    return entity_slug, token, metric


def belongs_to(statistic_id: str, entity_id: str) -> bool:
    """True when this ID was built for this entity.

    Exact at the state boundary. Not at the domain/object_id one:
    `sensor.a_b` and `sensor_a.b` slugify alike and would claim each other's
    IDs, as they did under the previous scheme too.
    """
    if (parts := parse(statistic_id)) is None:
        return False
    return parts[0] == slugify(entity_id, separator="_")
