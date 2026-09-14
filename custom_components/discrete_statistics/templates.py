"""Render a template to a timestamp; needs `hass`, never the recorder.

A custom period's edges are templates, and the two places that read them
- the sensor dialog validating a submit, and the coordinator rendering
the window on every refresh - must agree on what a valid one is. The
rendering lives here rather than in either of them so the dialog does
not import the coordinator, and with it the compiler and the recorder.
"""

from __future__ import annotations

import math

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import template
from homeassistant.util import dt as dt_util

# `datetime`'s own range, with room: past it a rendering raises rather
# than answering.
_TIMESTAMP_LIMIT = 2.5e11


def render_datetime(hass: HomeAssistant, text: str) -> float:
    """A template's rendering as a timestamp, or ValueError saying why not.

    What `history_stats` accepts: a date and time, or a timestamp. The
    flow renders once on submit with this same call, so a template that
    saves is one that renders - as long as what it reads still exists.
    """
    try:
        rendered = template.Template(text, hass).async_render(parse_result=False)
    except TemplateError as err:
        raise ValueError(str(err)) from err
    if (parsed := dt_util.parse_datetime(rendered)) is not None:
        return dt_util.as_utc(parsed).timestamp()
    try:
        value = float(rendered)
    except ValueError:
        raise ValueError(f"{rendered!r} is not a date and time") from None
    # `nan`, `inf` and a number far outside any date reach hour arithmetic
    # that raises rather than answers, so they are refused here, where the
    # dialog sees them too.
    if not math.isfinite(value) or abs(value) > _TIMESTAMP_LIMIT:
        raise ValueError(f"{rendered!r} is not a date and time")
    return value
