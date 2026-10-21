"""Test harness: a scratch copy of a small digiKam library, the viewer's own
data folder under a temporary directory, and one FastAPI test client.

Everything the app reads is set through the environment BEFORE app.config
is imported, so no test can touch the real library. Background analysis,
content search, thumbnail prewarming and captions are off; what they would
compute is tested on their own with scratch inputs.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

SRC_DB = Path(os.environ.get("DIGIKAM_TEST_DB", str(Path.home() / "Pictures/digikamDB/digikam4.db")))
SRC_ROOT = Path(os.environ.get("DIGIKAM_TEST_ROOT", str(Path.home() / "Pictures/photos")))

_tmp = Path(tempfile.mkdtemp(prefix="iopix-tests-"))
_lib = _tmp / "lib"
_data = _tmp / "data"

from fixture import build  # noqa: E402  (tests/ on sys.path through pytest's rootdir conftest import)

LIB = None
if SRC_DB.exists() and SRC_ROOT.exists():
    LIB = build(SRC_DB, SRC_ROOT, _lib)
else:  # no digiKam library to cut a scratch copy from (CI): the tests that need one skip, the rest run
    (_lib / "db").mkdir(parents=True, exist_ok=True); (_lib / "photos").mkdir(parents=True, exist_ok=True)
    print(f"\nNo test library at {SRC_DB} / {SRC_ROOT} (DIGIKAM_TEST_DB / DIGIKAM_TEST_ROOT): library tests are skipped")

os.environ.update({
    "DATA_DIR": str(_data), "DIGIKAM_DB_DIR": str(_lib / "db"), "PHOTOS_ROOT": str(LIB["root"] if LIB else _lib / "photos"),
    "PREWARM": "0", "SEMANTIC": "0", "ANALYSIS": "0", "CAPTIONS": "0", "REINDEX_POLL": "3600",
    "DIGIKAM_CHECK": "ask", "BG_THREADS": "2", "HF_HUB_OFFLINE": "1", "SCANNER_LOOP": "0",
})
_data.mkdir(parents=True, exist_ok=True)

from app import config  # noqa: E402

assert config.DATA_DIR.resolve() == _data.resolve() and (LIB is None or config.DIGIKAM_DB.resolve() == LIB["db"].resolve()), "environment overrides were not picked up"


@pytest.fixture(scope="session")
def lib():
    """Facts about the scratch library: ids, which have faces/tags/gps, videos."""
    if LIB is None:
        pytest.skip("needs a digiKam library to cut a scratch copy from (DIGIKAM_TEST_DB / DIGIKAM_TEST_ROOT)")
    return LIB


@pytest.fixture(scope="session")
def client(lib):
    """One test client for the session; the app's startup builds the index from the scratch DB."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        # the index is built in a background thread at startup; wait for it
        import time
        for _ in range(600):
            s = c.get("/api/status").json()
            if s.get("version") and not s.get("building"):
                break
            time.sleep(0.1)
        yield c


@pytest.fixture
def photo_id(lib):
    return lib["ids"][0]


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(_tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _digikam_vouched_closed():
    """DIGIKAM_CHECK=ask (Docker mode): the owner vouches that digiKam is closed for a while,
    exactly as the Settings page does; the tests vouch before every test."""
    import time
    from app.data import state
    state.init()  # the app's startup normally creates the tables; a test may run before any client exists
    state.set_setting("digikam_closed_until", str(time.time() + 3600))
    yield


@pytest.fixture(autouse=True)
def _no_users_by_default():
    """Most tests run in the open (no users) mode the app has before the first admin is created."""
    from app.data import state
    from app import users
    state.init(); users.init()
    con = state.connect(); con.execute("DELETE FROM user_accounts"); con.commit(); con.close()
    yield


@pytest.fixture
def fresh(client):
    """No accounts and no session cookie before and after: the open mode the app has before the
    first admin exists, for tests that create their own accounts."""
    from app.data import state
    def clear():
        con = state.connect(); con.execute("DELETE FROM user_accounts"); con.commit(); con.close()
        client.cookies.clear()
    clear(); yield; clear()
