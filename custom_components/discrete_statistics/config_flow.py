"""Config flow for discrete_statistics.

Hand-written rather than SchemaConfigFlowHandler: the YAML-clash guard and
the fixed entity_id are both awkward to express through the schema helper,
and it is one fewer Home Assistant API whose signature moves between
releases.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import States
from homeassistant.components.recorder.util import session_scope
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ENTITY_ID,
    CONF_NAME,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import (
    CONF_BLANK,
    CONF_DEFAULT,
    CONF_MIN_DURATION,
    CONF_STATES,
    DISPOSITIONS,
    blank_error,
    is_configured,
    min_duration_error,
)
from .const import (
    DEFAULT_IGNORE_SHORT,
    DEFAULT_IGNORE_SHORT_UNKNOWN,
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DISPOSITION_IGNORE,
    DISPOSITION_IGNORE_SHORT,
    DISPOSITION_RECORD,
    DOMAIN,
)
from .naming import describe, display_name
from .statistic_ids import is_blank

# `ignore` is deliberately absent. With no per-state mapping to supply
# exceptions it makes resolve() return None for every state, so nothing is
# ever recordable and the entity never compiles an hour.
# It stays valid in YAML, where `states:` supplies those exceptions.
UI_DEFAULTS = [
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DEFAULT_IGNORE_SHORT_UNKNOWN,
    DEFAULT_IGNORE_SHORT,
]

# Offered, not exhaustive: `blank` takes any state name, and mapping to a
# real one is the point for a text sensor whose blank means "no error".
# custom_value lets the dropdown be typed into.
BLANK_SUGGESTIONS = [STATE_UNKNOWN, DISPOSITION_IGNORE]

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        vol.Required(
            CONF_DEFAULT, default=DEFAULT_RECORD_KNOWN
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=UI_DEFAULTS,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=CONF_DEFAULT,
            )
        ),
        vol.Required(CONF_BLANK, default=STATE_UNKNOWN): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=BLANK_SUGGESTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=CONF_BLANK,
                custom_value=True,
            )
        ),
        # Read only under `ignore_short`; always shown, because a form is
        # one fixed schema and cannot grow a field on a dropdown choice.
        vol.Optional(CONF_MIN_DURATION): selector.DurationSelector(
            selector.DurationSelectorConfig(enable_day=False)
        ),
    }
)

USER_SCHEMA = vol.Schema({vol.Required(CONF_ENTITY_ID): selector.EntitySelector()})


def _seconds(duration: dict[str, float] | None) -> float | None:
    """The duration selector's value, as the seconds the config stores."""
    if duration is None:
        return None
    return timedelta(**duration).total_seconds()


def _duration(seconds: float | None) -> dict[str, float] | None:
    """Stored seconds, as the duration selector shows them."""
    if not seconds:
        return None
    whole = int(seconds)
    return {
        "hours": whole // 3600,
        "minutes": whole % 3600 // 60,
        "seconds": whole % 60,
    }


# The choice that leaves a state to `default`. Not a disposition: it is
# what the form shows for a state with no entry, and it stores nothing.
DISPOSITION_DEFAULT = "default"
FIXED_DISPOSITIONS = [
    DISPOSITION_DEFAULT,
    DISPOSITION_RECORD,
    DISPOSITION_IGNORE,
    DISPOSITION_IGNORE_SHORT,
]

# Not imported from homeassistant.components.sensor: that would make sensor a
# manifest dependency for two strings.
ATTR_STATE_CLASS = "state_class"
ATTR_OPTIONS = "options"

# A row per state, so the list is bounded before it is drawn. An entity
# with more distinct states than this is not discrete; the rest are left
# off the form.
MAX_KNOWN_STATES = 50


def _stored_states(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Every state the recorder holds for the entity. Recorder thread."""
    with session_scope(hass=hass, read_only=True) as session:
        metadata_id = get_instance(hass).states_meta_manager.get(
            entity_id, session, False
        )
        if metadata_id is None:
            return []
        return _distinct_states(session, metadata_id)


def _distinct_states(session: Session, metadata_id: int) -> list[str]:
    stmt = (
        select(States.state)
        .where(States.metadata_id == metadata_id)
        .distinct()
        .limit(MAX_KNOWN_STATES)
    )
    return [state for state in session.execute(stmt).scalars() if state]


async def async_known_states(
    hass: HomeAssistant, entity_id: str, states: Mapping[str, str]
) -> list[str]:
    """The states the mapping form offers a row for, sorted.

    The entity's own history is the truth about what it reports, and the
    live state and an enum sensor's `options` cover what it has not yet
    been in. The mapping's own keys and targets are kept so an entry made
    with a state the recorder has since purged still shows it. Blank states
    are left out: `blank` is their setting.
    """
    known = set(states) | {v for v in states.values() if v not in DISPOSITIONS}
    if (state := hass.states.get(entity_id)) is not None:
        known.add(state.state)
        known.update(state.attributes.get(ATTR_OPTIONS) or [])
    known.update(
        await get_instance(hass).async_add_executor_job(_stored_states, hass, entity_id)
    )
    return sorted((s for s in known if not is_blank(s)), key=str.casefold)


def _disposition_field(state: str, known: list[str]) -> selector.SelectSelector:
    """The choices for one state: the fixed dispositions, then a target."""
    choices = FIXED_DISPOSITIONS + [
        other for other in known if other != state and other not in FIXED_DISPOSITIONS
    ]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=choice, label=choice)
                for choice in choices
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="disposition",
            # A target the entity has never reported can still be typed.
            custom_value=True,
        )
    )


def _rows_schema(known: list[str]) -> vol.Schema:
    """A mapping row for each known state.

    Built per request: the rows are the entity's states, which a fixed
    schema cannot know.
    """
    return vol.Schema(
        {
            vol.Required(state, default=DISPOSITION_DEFAULT): _disposition_field(
                state, known
            )
            for state in known
        }
    )


def _options_schema(known: list[str], mapped: bool) -> vol.Schema:
    """The options form, with the rows in a section.

    The section opens when something is mapped, and stays folded away
    otherwise, since most entities need no mapping.
    """
    if not known:
        return OPTIONS_SCHEMA
    return OPTIONS_SCHEMA.extend(
        {
            vol.Required(CONF_STATES): section(
                _rows_schema(known), {"collapsed": not mapped}
            )
        }
    )


def _mapping(rows: Mapping[str, str]) -> dict[str, str]:
    """The `states` map the submitted rows describe."""
    return {
        state: disposition
        for state, disposition in rows.items()
        if disposition and disposition != DISPOSITION_DEFAULT
    }


def _mapping_errors(mapping: Mapping[str, str]) -> dict[str, str]:
    """Form errors for a submitted mapping.

    Form errors, not field ones: the frontend does not carry errors into a
    section's fields (ha-form-expandable.ts).
    """
    if any(
        target not in DISPOSITIONS and is_blank(target) for target in mapping.values()
    ):
        return {"base": "target_unusable"}
    return {}


def _options(user_input: dict[str, Any]) -> dict[str, Any]:
    """The options an entry stores, from a validated form."""
    options = {
        CONF_NAME: user_input.get(CONF_NAME) or None,
        CONF_DEFAULT: user_input[CONF_DEFAULT],
        CONF_BLANK: user_input[CONF_BLANK],
    }
    if (seconds := _seconds(user_input.get(CONF_MIN_DURATION))) is not None:
        options[CONF_MIN_DURATION] = seconds
    if mapping := _mapping(user_input.get(CONF_STATES, {})):
        options[CONF_STATES] = mapping
    return options


def _suggested(options: Mapping[str, Any]) -> dict[str, Any]:
    """Stored options, in the form the fields show them."""
    suggested = dict(options)
    if (duration := _duration(options.get(CONF_MIN_DURATION))) is not None:
        suggested[CONF_MIN_DURATION] = duration
    else:
        suggested.pop(CONF_MIN_DURATION, None)
    return suggested


def _errors(user_input: dict[str, Any]) -> dict[str, str]:
    """Field errors for a submitted form, shared by both flows."""
    errors: dict[str, str] = {}
    if problem := blank_error(user_input[CONF_BLANK]):
        errors[CONF_BLANK] = problem
    mapping = _mapping(user_input.get(CONF_STATES, {}))
    if problem := min_duration_error(
        _seconds(user_input.get(CONF_MIN_DURATION)), user_input[CONF_DEFAULT], mapping
    ):
        errors[CONF_MIN_DURATION] = problem
    return errors | _mapping_errors(mapping)


def _has_continuous_state(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity measures rather than reports a set of states.

    `state_class` is the strongest signal - it is precisely what tells the
    recorder to build its own long-term statistics - and a unit is the next
    best: units are for numbers.

    Both the registry and the live state are consulted. The registry keeps
    working while an entity is unavailable, when attributes have been
    stripped; the live state covers entities that never registered, such as
    template sensors. Either saying yes is enough, and a false positive is
    not a risk in practice, because a genuinely discrete entity has neither.
    """
    if (entry := er.async_get(hass).async_get(entity_id)) is not None:
        if (entry.capabilities or {}).get(ATTR_STATE_CLASS):
            return True
        if entry.unit_of_measurement:
            return True
    if (state := hass.states.get(entity_id)) is not None:
        attributes = state.attributes
        if attributes.get(ATTR_STATE_CLASS) or attributes.get(ATTR_UNIT_OF_MEASUREMENT):
            return True
    return False


async def _async_options_form(
    flow: ConfigFlow | OptionsFlow,
    step_id: str,
    entity_id: str,
    user_input: dict[str, Any] | None,
    stored: Mapping[str, Any],
    errors: dict[str, str],
) -> ConfigFlowResult:
    """The one form both flows edit an entity's options on.

    Which entity this is about, and what leaving the name blank would give,
    are in the description. The name is NOT prefilled into its box: a
    suggested value comes back on submit, which would freeze the name
    instead of letting it follow the entity.
    """
    mapping = (
        _mapping(user_input.get(CONF_STATES, {}))
        if user_input
        else stored.get(CONF_STATES) or {}
    )
    known = await async_known_states(flow.hass, entity_id, mapping)
    return flow.async_show_form(
        step_id=step_id,
        data_schema=flow.add_suggested_values_to_schema(
            _options_schema(known, bool(mapping)), user_input or _suggested(stored)
        ),
        errors=errors,
        description_placeholders={
            # A markdown link: the dialog description is rendered by
            # ha-markdown, which leaves same-host anchors alone so they
            # navigate in-app (ha-markdown-element.ts:114). The entry
            # row's own menu is fixed by the frontend and cannot be
            # added to, so this is the only place to offer the link.
            "entity": (
                f"[{describe(flow.hass, entity_id)}](/history?entity_id={entity_id})"
            ),
            "default_name": display_name(flow.hass, entity_id),
        },
    )


class DiscreteStatisticsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one tracked entity.

    Two steps: the entity, then the same form the options flow shows. The
    second cannot be the first, since which states get a mapping row is
    only known once an entity is picked.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Hold the picked entity while its options are shown."""
        self._entity_id = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an entity."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

        entity_id = user_input[CONF_ENTITY_ID]
        if _has_continuous_state(self.hass, entity_id):
            # A field error, not an abort: the dialog stays open so another
            # entity can be picked without starting again.
            return self.async_show_form(
                step_id="user",
                data_schema=self.add_suggested_values_to_schema(
                    USER_SCHEMA, user_input
                ),
                errors={CONF_ENTITY_ID: "continuous_state"},
            )

        # entity_id is identity: it builds every statistic ID, so it is the
        # unique id and is never editable afterwards.
        await self.async_set_unique_id(entity_id)
        self._abort_if_unique_id_configured()

        # On a fresh install with no YAML and no existing entries, the
        # component has never run: starting a flow only imports this module,
        # and async_setup runs when the first entry is created - so
        # hass.data[DOMAIN] genuinely does not exist yet. This guard is the
        # only thing standing between that state and a KeyError on the very
        # first entry anyone creates. Skipping the YAML check in that case
        # is also correct: with no async_setup having run, no YAML config
        # can exist to clash with.
        data = self.hass.data.get(DOMAIN)
        if data is not None and is_configured(data["yaml_configs"], entity_id):
            return self.async_abort(reason="yaml_configured")

        self._entity_id = entity_id
        return await self.async_step_options()

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Say how the entity's states are recorded."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _errors(user_input)
            if not errors:
                name = user_input.get(CONF_NAME) or None
                return self.async_create_entry(
                    # Name and ID both: the name is what people recognise,
                    # the ID is what tells two similarly-named entities
                    # apart in a list.
                    title=describe(self.hass, self._entity_id, name),
                    data={CONF_ENTITY_ID: self._entity_id},
                    options=_options(user_input),
                )
        return await _async_options_form(
            self, "options", self._entity_id, user_input, {}, errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow. entity_id is not editable here."""
        return DiscreteStatisticsOptionsFlow()


class DiscreteStatisticsOptionsFlow(OptionsFlow):
    """Edit everything but the entity.

    entity_id is absent by design: it builds every statistic ID, so
    changing it would orphan the entity's whole series.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the editable options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _errors(user_input)
            if not errors:
                return self.async_create_entry(data=_options(user_input))
        return await _async_options_form(
            self,
            "init",
            self.config_entry.data[CONF_ENTITY_ID],
            user_input,
            self.config_entry.options,
            errors,
        )
