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


class Fetched:
    """How many rows the statistics-table SELECTs have handed back."""

    def __init__(self) -> None:
        self.rows = 0
        self.on = False

    def clear(self) -> None:
        self.rows = 0


@pytest.fixture
def fetched(hass, recorder_mock):
    """Rows returned by the same statements `statements` counts.

    Counted through the sqlite3 connection's `row_factory`, which sees
    every row a cursor hands back; the flag says whether the statement
    running is one of ours. Rows are what a leaner query saves when the
    statement count is already one.
    """
    counter = Fetched()

    def row(cursor, values):
        if counter.on:
            counter.rows += 1
        return values

    def factory(dbapi_connection, *_):
        dbapi_connection.row_factory = row

    def before(conn, cursor, statement, parameters, context, executemany):
        counter.on = statement.lstrip().upper().startswith("SELECT") and bool(
            re.search(r"\bstatistics\b", statement)
        )

    engine = get_instance(hass).engine
    sqlalchemy_event.listen(engine, "connect", factory)
    sqlalchemy_event.listen(engine, "checkout", factory)
    sqlalchemy_event.listen(engine, "before_cursor_execute", before)
    yield counter
    sqlalchemy_event.remove(engine, "connect", factory)
    sqlalchemy_event.remove(engine, "checkout", factory)
    sqlalchemy_event.remove(engine, "before_cursor_execute", before)
