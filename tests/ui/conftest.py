"""Browser tests (Playwright, headless Chromium) for the behaviours a user sees, split by subject:
test_viewer (the lightbox, faces, the info panel), test_pages (Explore, People, folders, albums, the
suggestion and quality pages, Settings and Maintenance), test_accounts (users, proxy sign-in, Require
sign-in, CSP, PWA, offline) and test_layout (phone, sidebar, breadcrumbs, filters, units, theme).

A real uvicorn serves the scratch library on a free port, started once per session from the same
environment the API tests use (tests/conftest.py), but with its own copy of the library and its own data
folder, so the two processes never reindex each other's edits. The fixtures and the helpers every module
uses live here; a test module imports them from ui.conftest.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from helpers import clear_accounts

APP = Path(__file__).resolve().parent.parent.parent
LIGHTBOX_MEDIA = "#lightbox .lb-frame img, #lightbox .lb-frame video"  # the viewer shows its picture (or video)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def lib(lib):
    """The browser tests' server gets its own copy of the scratch library (digiKam database and photos), so
    the two processes stop reindexing each other's edits. Same photo ids, so every fact in the session
    `lib` still holds; the paths are the copy's, and every test under tests/ui sees this one."""
    import shutil
    src = Path(lib["db"]).parent.parent
    dst = src.parent / "lib-ui"
    if not dst.exists():
        shutil.copytree(src, dst)
    return dict(lib, db=dst / "db" / "digikam4.db", root=dst / "photos")


@pytest.fixture(scope="session")
def server(lib):
    """uvicorn on a free port with the API tests' env overrides (set by conftest), its own data folder and
    its own copy of the library (the `lib` above). Its stderr goes to a file (a pipe nobody reads would stall
    the server once full), so a server that dies can say why."""
    port = _free_port()
    import mariadb  # tests/mariadb.py: the browser tests' server gets a database of its own on the test MariaDB
    UI_DB.update(mariadb.fresh_database(mariadb.UI_DB))
    env = dict(os.environ, DATA_DIR=str(Path(os.environ["DATA_DIR"]).parent / "data-ui"),
               DIGIKAM_DB_DIR=str(Path(lib["db"]).parent), PHOTOS_ROOT=str(lib["root"]), **mariadb.env_for(UI_DB))
    log = tempfile.NamedTemporaryFile(prefix="iopix-ui-server-", suffix=".log", delete=False)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=str(APP), env=env, stdout=subprocess.DEVNULL, stderr=log)
    url = f"http://127.0.0.1:{port}"

    def died(why):
        log.flush()
        return RuntimeError(f"{why}: " + Path(log.name).read_text(errors="replace")[-800:])
    try:
        for _ in range(300):
            try:
                with urllib.request.urlopen(url + "/api/status", timeout=1) as r:
                    if json.load(r).get("version"):
                        break
            except Exception:
                pass
            if proc.poll() is not None:
                raise died("server died")
            time.sleep(0.1)
        else:
            raise died("server did not come up")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)
        log.close()
        if not os.environ.get("IOPIX_UI_KEEP_LOG"): Path(log.name).unlink(missing_ok=True)  # keep it to read a failing run


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture(autouse=True)
def _ui_no_users(server):
    """The browser tests' own server keeps its users in a database of its own: start each test in open mode."""
    clear_accounts(UI_DB)
    yield


@pytest.fixture
def page(browser, server):
    # bypass_csp: Playwright polls wait_for_function by eval() inside the page, which the app's Content Security Policy
    # refuses; the policy itself is checked by test_csp_lets_the_app_and_the_vendored_map_run in a strict context
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, bypass_csp=True)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.base = server
    pg.errors = errors
    yield pg
    pg.unroute_all(behavior="ignoreErrors")  # a mocked route still answering at close would fail the next test's context
    ctx.close()
    assert not errors, "uncaught JS errors: " + "; ".join(errors)[:500]


UI_DB: dict = {}  # the browser tests' server's own viewer database (vdb-style settings), filled by the server fixture


def ui_db():
    """A connection (autocommit) to the browser tests' server's own viewer database, for a test that reads or seeds
    a row behind the server's back (dismissed suggestions, the digiKam-closed stamp)."""
    import pymysql
    return pymysql.connect(host=UI_DB["host"], port=int(UI_DB["port"]), database=UI_DB["database"], user=UI_DB["user"], password=UI_DB["password"], autocommit=True, connect_timeout=5)


# --- talking to the server from the test -------------------------------------------------------------

def _get(server, path, timeout=10):
    """GET a JSON answer from the browser tests' server."""
    with urllib.request.urlopen(server + path, timeout=timeout) as r:
        return json.load(r)


def _post(server, path, body=None, timeout=10):
    """POST `body` as JSON to the browser tests' server; the decoded answer (None when the server sends nothing back).
    An error status raises urllib.error.HTTPError, as the hand-written requests did."""
    req = urllib.request.Request(server + path, data=json.dumps(body if body is not None else {}).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _set_read_only(server, on: bool):
    _post(server, "/api/settings", {"read_only": on}, timeout=5)


def _eventually(fn, timeout=10.0, step=0.2):
    """Poll the server (another process, whose index is rebuilt after a write) until fn() is truthy; the last value."""
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() > end:
            return v
        time.sleep(step)


# --- waiting on the page --------------------------------------------------------------------------------

def _wait_flags(page, timeout=10000):
    """The status poll (pollStatus in main.js) has answered: S.EDITING, S.CAN_MANAGE, S.IS_ADMIN and the other flags that
    show or hide the editing controls are the server's, not the defaults the page started with. boot() awaits the first
    poll before drawing a page, so after a load this is already so; after a sign-in, a settings change or a reload it is
    what a test has to wait for. APP_VERSION is '' until the first answer and never after."""
    page.wait_for_function("import('/js/state.js').then((m) => !!m.S.APP_VERSION)", timeout=timeout)


def _viewer_ready(page):
    """The viewer shows its picture (or video) and has the photo's details: /api/photo/{id} has arrived, which the title
    line shows by carrying the date. The mouse then rests on the toolbar, which hides 2.8 s after the last mouse move: a
    slow load must not hide it before the test clicks."""
    page.wait_for_selector(LIGHTBOX_MEDIA, timeout=30000)  # 30 s: in a full run the server is still indexing what earlier tests changed
    page.wait_for_function("!!document.querySelector('#lightbox .lb-top .ttl small')", timeout=30000)
    page.mouse.move(420, 40)


def _open_photo(page, server, pid):
    """Open photo `pid` in the viewer from a fresh page and wait until it is ready (`_viewer_ready`)."""
    page.goto(f"{server}/#/photos?p={pid}")
    _viewer_ready(page)
