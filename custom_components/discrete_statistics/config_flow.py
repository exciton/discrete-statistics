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
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ENTITY_ID,
    CONF_NAME,
    STATE_UNAVAILABLE,
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
    EntityConfig,
    blank_error,
    entity_config_from_entry,
    is_configured,
    min_duration_error,
    uses_ignore_short,
)
from .const import (
    CONF_CUSTOM,
    CONF_LIVE,
    CONF_METRIC,
    CONF_PERIOD,
    CONF_WINDOW_DURATION,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DEFAULT_IGNORE_SHORT,
    DEFAULT_IGNORE_SHORT_UNKNOWN,
    DEFAULT_MIN_DURATION,
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DISPOSITION_IGNORE,
    DISPOSITION_IGNORE_SHORT,
    DISPOSITION_RECORD,
    DOMAIN,
    METRIC_COUNT,
    METRIC_DURATION,
    SENSOR_METRICS,
    SUBENTRY_SENSOR,
)
from .coordinator import render_datetime
from .naming import (
    async_warm_state_translations,
    describe,
    display_name,
    sensor_title,
    state_translator,
)
from .payload import readable_state
from .periods import PERIODS, is_custom
from .reading import Custom, Spec, spec_from
from .statistic_ids import build, is_blank, parse, state_token

# `ignore` is deliberately absent. With no per-state mapping to supply
# exceptions it makes resolve() return None for every state, so nothing is
# ever recordable and the entity never compiles an hour.
# It stays valid in YAML, where `states:` supplies those exceptions.
UI_DEFAULTS = [
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DEFAULT_IGNORE_SHORT,
    DEFAULT_IGNORE_SHORT_UNKNOWN,
]

# Offered, not exhaustive: `blank` takes any state name, and mapping to a
# real one is the point for a text sensor whose blank means "no error".
# custom_value lets the dropdown be typed into.
BLANK_SUGGESTIONS = [STATE_UNKNOWN, DISPOSITION_IGNORE]
# The rows every entity gets, after the ones it has actually reported.
ALWAYS_KNOWN = (STATE_UNAVAILABLE, STATE_UNKNOWN)

# The name is not here: its box shows the entity's own name greyed out,
# so the field is built per request by `_name_field`.
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_DEFAULT, default=DEFAULT_RECORD_KNOWN
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=UI_DEFAULTS,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=CONF_DEFAULT,
            )
        ),
        # Read only under `ignore_short`; always shown, because a form is
        # one fixed schema and cannot grow a field on a dropdown choice.
        vol.Optional(CONF_MIN_DURATION): selector.DurationSelector(
            selector.DurationSelectorConfig(enable_day=False)
        ),
    }
)

# The blank state's row, last in the States section: it is one more state
# the entity may report, and the section is where each state's treatment
# goes. A state literally named `blank` would share its key, so `_known`
# leaves that one off the form.
BLANK_FIELD = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=BLANK_SUGGESTIONS,
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key=CONF_BLANK,
        custom_value=True,
    )
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
    DISPOSITION_IGNORE_SHORT,
    DISPOSITION_IGNORE,
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
    are left out: `blank` is their setting. `unavailable` and `unknown`
    close the list whether or not the entity has reported them, since
    every entity can, and they are the two a person most often wants
    treated differently from the rest.
    """
    known = set(states) | {v for v in states.values() if v not in DISPOSITIONS}
    if (state := hass.states.get(entity_id)) is not None:
        known.add(state.state)
        known.update(state.attributes.get(ATTR_OPTIONS) or [])
    known.update(
        await get_instance(hass).async_add_executor_job(_stored_states, hass, entity_id)
    )
    known -= set(ALWAYS_KNOWN)
    return sorted((s for s in known if not is_blank(s)), key=str.casefold) + list(
        ALWAYS_KNOWN
    )


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
    """A mapping row for each known state, then the blank state's row.

    Built per request: the rows are the entity's states, which a fixed
    schema cannot know.
    """
    rows: dict[Any, Any] = {
        vol.Required(state, default=DISPOSITION_DEFAULT): _disposition_field(
            state, known
        )
        for state in known
    }
    rows[vol.Required(CONF_BLANK, default=STATE_UNKNOWN)] = BLANK_FIELD
    return vol.Schema(rows)


def _name_field(default_name: str) -> selector.TextSelector:
    """A name box with the entity's own name greyed out in it.

    A placeholder, not a value: the box stays empty, so the name keeps
    following the entity until one is typed. The frontend's text selector
    draws `placeholder` (ha-selector-text.ts:85), but core's config schema
    for the selector does not list the key and would refuse it, so it is
    added after validation; `serialize` passes the config on verbatim.
    """
    field = selector.TextSelector()
    field.config["placeholder"] = default_name  # type: ignore[typeddict-unknown-key]
    return field


def _options_schema(known: list[str], open_: bool, default_name: str) -> vol.Schema:
    """The options form: the name first, then the rows in a section.

    The section opens when something in it is set, and stays folded away
    otherwise, since most entities need no mapping.
    """
    return vol.Schema(
        {
            vol.Optional(CONF_NAME): _name_field(default_name),
            **OPTIONS_SCHEMA.schema,
            vol.Required(CONF_STATES): section(
                _rows_schema(known), {"collapsed": not open_}
            ),
        }
    )


def _mapping(rows: Mapping[str, str]) -> dict[str, str]:
    """The `states` map the submitted rows describe."""
    return {
        state: disposition
        for state, disposition in rows.items()
        if state != CONF_BLANK and disposition and disposition != DISPOSITION_DEFAULT
    }


def _blank(user_input: Mapping[str, Any]) -> str:
    """The blank state's treatment, from its row in the section."""
    return user_input.get(CONF_STATES, {}).get(CONF_BLANK, STATE_UNKNOWN)


def _known(states: list[str]) -> list[str]:
    """The states that get a row: all but one sharing the blank row's key."""
    return [state for state in states if state != CONF_BLANK]


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
        CONF_BLANK: _blank(user_input),
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
    suggested[CONF_STATES] = {
        **(options.get(CONF_STATES) or {}),
        CONF_BLANK: options.get(CONF_BLANK, STATE_UNKNOWN),
    }
    return suggested


def _filled(user_input: dict[str, Any]) -> dict[str, Any]:
    """The submitted form, with a blank duration filled in where one is needed.

    The box cannot be made required by the choice beside it - a form is one
    fixed schema - so a choice that needs a duration and a box left blank,
    or at zero, gets `DEFAULT_MIN_DURATION` rather than an error; the form
    shows what it chose if something else sends it back.
    """
    if _seconds(user_input.get(CONF_MIN_DURATION)) or not uses_ignore_short(
        user_input[CONF_DEFAULT], _mapping(user_input.get(CONF_STATES, {}))
    ):
        return user_input
    return {**user_input, CONF_MIN_DURATION: _duration(DEFAULT_MIN_DURATION)}


def _errors(user_input: dict[str, Any]) -> dict[str, str]:
    """Field errors for a submitted form, shared by both flows."""
    errors: dict[str, str] = {}
    # A form error, like the mapping's: the field is in the section.
    if problem := blank_error(_blank(user_input)):
        errors["base"] = problem
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

    Which entity this is about is in the description, and what leaving the
    name blank would give is greyed out in the name box. The name is NOT
    prefilled into it: a suggested value comes back on submit, which would
    freeze the name instead of letting it follow the entity.
    """
    if user_input:
        mapping = _mapping(user_input.get(CONF_STATES, {}))
        blank = _blank(user_input)
    else:
        mapping = stored.get(CONF_STATES) or {}
        blank = stored.get(CONF_BLANK, STATE_UNKNOWN)
    known = await async_known_states(flow.hass, entity_id, mapping)
    return flow.async_show_form(
        step_id=step_id,
        data_schema=flow.add_suggested_values_to_schema(
            _options_schema(
                _known(known),
                bool(mapping) or blank != STATE_UNKNOWN,
                display_name(flow.hass, entity_id),
            ),
            user_input or _suggested(stored),
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
        },
    )


async def async_offered_states(
    hass: HomeAssistant, cfg: EntityConfig, existing: Mapping[str, str]
) -> list[selector.SelectOptionDict]:
    """The states a sensor can be over, labelled as Home Assistant renders them.

    Two sources, the same as the card's and the entity's own: every
    duration statistic the entity has - the state as its name holds it,
    which catches states no longer reported - and an enum's live `options`
    through `resolve()`, the one case a never-seen state is a real choice.
    One per token, so a state the entry maps onto another is offered once.
    Not the recorder's distinct states: those only add the last hour, and
    the box can be typed into.
    """
    await async_warm_state_translations(hass, cfg.entity_id)
    translate = state_translator(hass, cfg.entity_id)
    offered: dict[str, str] = {}
    for statistic_id, name in existing.items():
        if (parts := parse(statistic_id)) is not None and parts[2] == METRIC_DURATION:
            offered.setdefault(parts[1], readable_state(name, parts[1]))
    if (state := hass.states.get(cfg.entity_id)) is not None:
        for option in state.attributes.get(ATTR_OPTIONS) or []:
            resolved = cfg.resolve(option)
            if resolved is not None and not is_blank(resolved):
                offered.setdefault(state_token(resolved), resolved)
    return sorted(
        (
            selector.SelectOptionDict(value=state, label=translate(state))
            for state in offered.values()
        ),
        key=lambda option: option["label"].casefold(),
    )


def _sensor_states(
    cfg: EntityConfig, existing: Mapping[str, str], raw_states: list[str]
) -> tuple[list[str], dict[str, str]]:
    """The states a sensor reads, as the entry records them, or why not.

    Resolved through the entry's own mapping so the sensor reads the
    statistic the compile writes. A state the entry ignores is refused -
    a sensor over it would sit at zero - unless it already has a duration
    statistic, in which case the settings changed after it was written and
    there is history to read.
    """
    states: list[str] = []
    for raw in (raw.strip() for raw in raw_states):
        if is_blank(raw):
            return [], {CONF_STATES: "state_unusable"}
        resolved = cfg.resolve(raw)
        if (
            resolved is None
            and build(cfg.entity_id, raw, METRIC_DURATION) not in existing
        ):
            return [], {CONF_STATES: "state_ignored"}
        state = resolved or raw
        if state not in states:
            states.append(state)
    return states, {}


def _custom_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_WINDOW_START): selector.TemplateSelector(),
            vol.Optional(CONF_WINDOW_END): selector.TemplateSelector(),
            vol.Optional(CONF_WINDOW_DURATION): selector.DurationSelector(
                selector.DurationSelectorConfig(enable_day=False)
            ),
        }
    )


def _sensor_schema(
    offered: list[selector.SelectOptionDict], default_name: str, open_custom: bool
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_STATES, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=offered,
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_METRIC, default=METRIC_DURATION): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(SENSOR_METRICS),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_METRIC,
                )
            ),
            vol.Required(CONF_PERIOD, default="this_month"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(PERIODS),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_PERIOD,
                )
            ),
            # A form is one fixed schema, so the window cannot appear only
            # on the dropdown's custom choice; it is folded away instead.
            vol.Optional(CONF_CUSTOM): section(
                _custom_schema(), {"collapsed": not open_custom}
            ),
            vol.Optional(CONF_NAME): _name_field(default_name),
            vol.Required(CONF_LIVE, default=True): selector.BooleanSelector(),
        }
    )


def _custom_window(
    hass: HomeAssistant, period: str, window: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The custom window's data from the section, and the error keeping the form open.

    Templates are rendered the same way the coordinator renders them.
    Under any other period the section must be empty, so a stale window
    cannot sit unread behind a calendar choice.
    """
    start = (window.get(CONF_WINDOW_START) or "").strip() or None
    end = (window.get(CONF_WINDOW_END) or "").strip() or None
    duration = _seconds(window.get(CONF_WINDOW_DURATION)) or None
    data = {
        CONF_WINDOW_START: start,
        CONF_WINDOW_END: end,
        CONF_WINDOW_DURATION: duration,
    }
    given = sum(value is not None for value in data.values())
    if not is_custom(period):
        return dict.fromkeys(data), {"base": "custom_only"} if given else {}
    if given == 3 or given == 0 or (given == 1 and start is None):
        return data, {"base": "custom_needs_two"}
    rendered: dict[str, float] = {}
    # Start is checked first, so a template broken in both fields is blamed
    # on Start - the field a person reads first, and the one whose error
    # would otherwise be masked by End's.
    for key, text in ((CONF_WINDOW_START, start), (CONF_WINDOW_END, end)):
        if text is None:
            continue
        try:
            rendered[key] = render_datetime(hass, text)
        except ValueError:
            field = "start" if key == CONF_WINDOW_START else "end"
            return data, {"base": f"template_invalid_{field}"}
    if len(rendered) == 2 and rendered[CONF_WINDOW_END] <= rendered[CONF_WINDOW_START]:
        return data, {"base": "custom_empty"}
    return data, {}


class SensorSubentryFlow(ConfigSubentryFlow):
    """Add or edit one period sensor on an entry.

    One form for both: the sensor's states, metric and period, its name,
    and whether it follows the entity live. Creating and reconfiguring
    differ only in what the form is filled with and what saving it does.
    """

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form("user", user_input, None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form(
            "reconfigure", user_input, self._get_reconfigure_subentry()
        )

    async def _async_form(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        subentry: ConfigSubentry | None,
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        cfg = entity_config_from_entry(entry.data, entry.options)
        existing = await self.hass.data[DOMAIN]["compiler"].async_existing(
            cfg.entity_id
        )
        errors: dict[str, str] = {}
        custom_errors: dict[str, str] = {}
        if user_input is not None:
            states, errors = _sensor_states(
                cfg, existing, user_input.get(CONF_STATES, [])
            )
            metric = user_input[CONF_METRIC]
            if not errors and not states and metric != METRIC_COUNT:
                # Time in "any state" is the whole period; only the count
                # of changes means something over every state.
                errors[CONF_METRIC] = "all_states_count_only"
            window, custom_errors = _custom_window(
                self.hass, user_input[CONF_PERIOD], user_input.get(CONF_CUSTOM) or {}
            )
            errors.update(custom_errors)
            if not errors:
                period = user_input[CONF_PERIOD]
                spec = Spec(
                    tuple(states),
                    metric,
                    period,
                    user_input[CONF_LIVE],
                    Custom(**window) if is_custom(period) else None,
                )
                name = user_input.get(CONF_NAME) or None
                data = {
                    CONF_STATES: states,
                    CONF_METRIC: metric,
                    CONF_PERIOD: period,
                    CONF_LIVE: spec.live,
                    CONF_NAME: name,
                    **window,
                }
                title = name or sensor_title(self.hass, cfg, spec)
                if subentry is None:
                    return self.async_create_entry(title=title, data=data)
                return self.async_update_and_abort(
                    entry, subentry, data=data, title=title
                )

        # What is in the box, or was stored, is what the greyed-out name
        # describes; a stored name of None is left out so the box stays
        # empty rather than suggesting "None". The window is a section of
        # the form, so it is nested back under its key.
        if user_input is not None:
            current = user_input
        else:
            stored = subentry.data if subentry else {}
            current = {
                k: v
                for k, v in stored.items()
                if v is not None
                and k not in (CONF_WINDOW_START, CONF_WINDOW_END, CONF_WINDOW_DURATION)
            }
            window = {
                CONF_WINDOW_START: stored.get(CONF_WINDOW_START),
                CONF_WINDOW_END: stored.get(CONF_WINDOW_END),
                CONF_WINDOW_DURATION: _duration(stored.get(CONF_WINDOW_DURATION)),
            }
            current[CONF_CUSTOM] = {k: v for k, v in window.items() if v is not None}
        # Open on anything set, or on the error naming the fields in it -
        # an all-empty section that just failed would otherwise stay
        # collapsed and hide the fields the error is about.
        open_custom = bool(custom_errors) or any(
            (current.get(CONF_CUSTOM) or {}).values()
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _sensor_schema(
                    await async_offered_states(self.hass, cfg, existing),
                    sensor_title(self.hass, cfg, spec_from(current)),
                    open_custom,
                ),
                current,
            ),
            errors=errors,
            description_placeholders={
                "entity": (
                    f"[{describe(self.hass, cfg.entity_id, cfg.name)}]"
                    f"(/history?entity_id={cfg.entity_id})"
                ),
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
            user_input = _filled(user_input)
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

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Period sensors hang off the entry as subentries."""
        return {SUBENTRY_SENSOR: SensorSubentryFlow}


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
            user_input = _filled(user_input)
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
