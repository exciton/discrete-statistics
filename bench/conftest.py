"""Fixtures for the bench. The root `conftest.py` keeps `pytest_plugins`;
declaring it in a non-rootdir conftest is an error in modern pytest.

`test_bench.py` only means anything against a prepared database - a
SQLite file, or a database in the MariaDB or Postgres container
`script/bench-db` runs - which `script/bench` names with `--dburl`.
Collected with the default in-memory URL it would measure nothing and
fail, so it is ignored unless the URL is one of those. `test_selftest.py`
builds its own database and is always collected.

`recorder_db_url` is overridden for the two server engines because the
plugin's own fixture *creates* the database on the way in and *drops* it
on the way out. A bench database is loaded once and measured many times,
so this yields the URL untouched.
"""

import pytest

SERVER_PREFIXES = ("mysql://", "mysql+pymysql://", "postgresql://")


def _is_bench_url(url: str) -> bool:
    return url.endswith(".db") or url.startswith(SERVER_PREFIXES)


def pytest_ignore_collect(collection_path, config):
    if collection_path.name != "test_bench.py":
        return None
    return not _is_bench_url(config.getoption("dburl"))


@pytest.fixture
def recorder_db_url(pytestconfig, hass_fixture_setup):
    """The URL as given: no create, no drop.

    `hass_fixture_setup` is requested for the same reason the plugin's
    own fixture asserts on it - the database has to be settled before
    `hass` starts.
    """
    url = pytestconfig.getoption("dburl")
    assert not hass_fixture_setup
    yield url


@pytest.fixture(autouse=True)
def bench_sockets(request, pytestconfig):
    """MariaDB and Postgres are reached over TCP, which the HA plugin
    blocks by default; `socket_enabled` is pytest-socket's own escape."""
    if pytestconfig.getoption("dburl").startswith(SERVER_PREFIXES):
        request.getfixturevalue("socket_enabled")
    yield
