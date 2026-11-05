"""The MariaDB the tests run iopix's own database on.

iopix's own database (settings, accounts, favorites, albums, the edit log, AI results, the library copy) is
MariaDB-only since v1.113, so the suite needs a server. Two ways:

- IOSUITE_TEST_DB=host:port:user:password names one (CI uses a MariaDB service this way). The user must be
  allowed to CREATE and DROP databases: every run starts from empty databases of its own.
- Otherwise a container is started with Docker, `iosuite-test-mariadb` on a free local port, and left running
  for the next run (a start costs ten seconds; `docker rm -f iosuite-test-mariadb` throws it away). Without
  Docker and without IOSUITE_TEST_DB the whole suite is skipped with a message that says so.

A session uses two databases: `iopix_test` for the API tests (the app imported in-process) and
`iopix_test_ui` for the browser tests' own server (a second process, tests/ui/conftest.py). Both are
dropped and created again at the start of every session, never shared with anything real.
"""
import os
import socket
import subprocess
import time

CONTAINER = "iosuite-test-mariadb"
IMAGE = "mariadb:11"
ROOT_PASSWORD = "iosuite-test"  # the same container as iosuite_shared.testing
API_DB, UI_DB = "iopix_test", "iopix_test_ui"
_server = None


def server() -> dict:
    """{host, port, user, password} of a reachable MariaDB, or None (with the reason in `server.reason`)."""
    global _server
    if _server is not None:
        return _server or None
    given = os.environ.get("IOSUITE_TEST_DB", "")
    if given:
        host, port, user, password = (given.split(":", 3) + ["", "", ""])[:4]
        c = {"host": host, "port": int(port or 3306), "user": user or "root", "password": password}
    else:
        c = _docker()
    if c and _wait(c):
        _server = c
        return c
    _server = {}
    return None


server.reason = ""


def _docker() -> dict:
    """The test container, started when it is not running; None when Docker is not there."""
    try:
        out = subprocess.run(["docker", "port", CONTAINER, "3306/tcp"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        server.reason = f"no Docker ({e}) and no IOSUITE_TEST_DB"
        return None
    if out.returncode != 0:  # not running: start it (a stopped one of the same name is removed first)
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, timeout=30)
        run = subprocess.run(["docker", "run", "-d", "--name", CONTAINER, "-p", "127.0.0.1:0:3306",
                              "-e", f"MARIADB_ROOT_PASSWORD={ROOT_PASSWORD}", IMAGE], capture_output=True, text=True, timeout=600)
        if run.returncode != 0:
            server.reason = "docker run failed: " + run.stderr.strip()[-300:]
            return None
        out = subprocess.run(["docker", "port", CONTAINER, "3306/tcp"], capture_output=True, text=True, timeout=20)
    port = int(out.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
    return {"host": "127.0.0.1", "port": port, "user": "root", "password": ROOT_PASSWORD}


def _wait(c: dict, seconds: float = 90) -> bool:
    """The server answers a query (a fresh container takes a few seconds to initialise)."""
    import pymysql
    end = time.time() + seconds
    while time.time() < end:
        try:
            con = pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"], connect_timeout=3)
            con.close()
            return True
        except (pymysql.err.Error, OSError, socket.error) as e:
            server.reason = f"MariaDB at {c['host']}:{c['port']} does not answer: {e}"
            time.sleep(0.5)
    return False


def fresh_database(name: str) -> dict:
    """Drop and create `name` on the test server; the connection settings for it (vdb's KEYS)."""
    import pymysql
    c = server()
    con = pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"], connect_timeout=5)
    try:
        with con.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
            cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        con.commit()
    finally:
        con.close()
    return {"type": "mysql", "host": c["host"], "port": c["port"], "database": name, "user": c["user"], "password": c["password"]}


def env_for(settings: dict) -> dict:
    """The DB_* settings that point an app process at `settings` (app.config.DB_ENV's names)."""
    names = {"type": "DB_TYPE", "host": "DB_HOST", "port": "DB_PORT", "database": "DB_NAME", "user": "DB_USER", "password": "DB_PASSWORD"}
    return {names[k]: str(v) for k, v in settings.items()}
