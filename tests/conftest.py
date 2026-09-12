"""Fixtures shared by the recorder-backed tests.

`pytest_plugins` stays in the root `conftest.py`; declaring it here is an
error in modern pytest and breaks the whole suite.
"""

import re

import pytest
from homeassistant.components.recorder import get_instance
from sqlalchemy import event as sqlalchemy_event


@pytest.fixture
def statements(hass, recorder_mock):
    """The SELECTs the recorder's engine runs against the statistics table.

    `statistics_meta` is not one of them: `_` is a word character, so the
    boundary after `statistics` does not match it.
    """
    seen: list[str] = []

    def listen(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and re.search(
            r"\bstatistics\b", statement
        ):
            seen.append(statement)

    engine = get_instance(hass).engine
    sqlalchemy_event.listen(engine, "before_cursor_execute", listen)
    yield seen
    sqlalchemy_event.remove(engine, "before_cursor_execute", listen)
