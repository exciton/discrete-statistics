"""Rendering a custom period's edge template to a timestamp."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.discrete_statistics.templates import render_datetime

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_render_datetime_takes_a_datetime_string_or_a_timestamp(hass):
    assert (
        render_datetime(hass, "2026-01-01T01:15:00+00:00")
        == (T0 + timedelta(hours=1, minutes=15)).timestamp()
    )
    # A timestamp, as `as_timestamp(now())` renders one.
    assert render_datetime(hass, "{{ 1767225600.0 }}") == 1767225600.0
    with pytest.raises(ValueError):
        render_datetime(hass, "{{ 'soon' }}")
    with pytest.raises(ValueError):
        render_datetime(hass, "{{ nonsense( }}")
    # A number is not a timestamp merely for being a number: each of these
    # reaches hour arithmetic that raises out of the whole entry's refresh.
    for text in (
        "{{ 'inf' | float }}",
        "{{ 'nan' | float }}",
        "{{ 1e20 }}",
        "{{ 2.6e11 }}",
    ):
        with pytest.raises(ValueError):
            render_datetime(hass, text)
