"""The MariaDB the tests run iopix's own database on: the suite's test server (iosuite_shared.testing).

iopix's own database (settings, accounts, favorites, albums, the edit log, AI results, the library copy) is
MariaDB-only since v1.113, so the suite needs a server. Two ways, the same as in every app of the suite:

- IOSUITE_TEST_DB=host:port:user:password names one (CI uses a MariaDB service this way). The user must be
  allowed to CREATE and DROP databases: every run starts from empty databases of its own.
- Otherwise a container is started with Docker, `iosuite-test-mariadb` on a free local port, and left running
  for the next run (`docker rm -f iosuite-test-mariadb` throws it away). Without Docker and without
  IOSUITE_TEST_DB the whole suite is skipped with a message that says so.

A session uses two databases: `iopix_test` for the API tests (the app imported in-process) and
`iopix_test_ui` for the browser tests' own server (a second process, tests/ui/conftest.py). Both are
dropped and created again at the start of every session, never shared with anything real.
"""
from iosuite_shared import testing as _testing

API_DB, UI_DB = "iopix_test", "iopix_test_ui"
server = _testing.server  # {host, port, user, password} of a reachable MariaDB, or None (the reason in server.reason)


def fresh_database(name: str) -> dict:
    """Drop and create `name` on the test server; the connection settings for it (vdb's KEYS)."""
    return {"type": "mysql", **_testing.fresh_database(name)}


def env_for(settings: dict) -> dict:
    """The DB_* settings that point an app process at `settings` (app.config.DB_ENV's names)."""
    names = {"type": "DB_TYPE", "host": "DB_HOST", "port": "DB_PORT", "database": "DB_NAME", "user": "DB_USER", "password": "DB_PASSWORD"}
    return {names[k]: str(v) for k, v in settings.items()}
