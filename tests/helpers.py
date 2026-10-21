"""Helpers shared by the test modules: accounts, the external tools a test needs, and the
library probes several modules use (a JPEG with a face, a dated copy, forgetting a photo).

IOPIX_TEST_STRICT=1 turns the skips for a missing exiftool, jpegtran, ffmpeg or Playwright into
failures, for a machine that is supposed to have them all (CI with the tools installed)."""
import importlib.util
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

STRICT = os.environ.get("IOPIX_TEST_STRICT", "").strip() == "1"


def need_tool(name: str, available=None, module_level: bool = False):
    """Skip the test (or the module, with module_level) when the tool `name` is missing: an executable
    on PATH or an importable module, or the caller's own check in `available`. IOPIX_TEST_STRICT=1
    makes it a failure instead, so a test machine that lacks a tool cannot pass quietly."""
    if available is None:
        try:
            available = shutil.which(name) is not None or importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            available = False
    if available:
        return
    msg = f"{name} is not installed here"
    if STRICT:
        pytest.fail(f"{msg} (IOPIX_TEST_STRICT=1: every tool must be present)", pytrace=False)
    pytest.skip(msg, allow_module_level=module_level)


def clear_accounts(db_path=None):
    """Back to the open mode the app has before the first admin exists. Without a path, the app's own
    viewer database (SQLite or MySQL, through app.data.state); with one, that SQLite file (the browser
    tests' server keeps its users in its own viewer.db)."""
    if db_path is None:
        from app.data import state
        from app import users
        state.init(); users.init()
        con = state.connect()
    else:
        db_path = Path(db_path)
        if not db_path.exists():
            return
        con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM user_accounts"); con.commit()
    finally:
        con.close()


def make_admin(target, name: str = "alex", password: str = "correct horse"):
    """Sign `target` in as the admin `name`, creating the account when no users exist yet (POST
    /api/auth/setup; 409 once users exist, then a plain login). `target` is a FastAPI TestClient, a
    Playwright Page (its browser context then carries the session cookie) or a server URL string."""
    body = {"name": name, "password": password}
    if isinstance(target, str):
        import json
        import urllib.error
        import urllib.request
        for path in ("/api/auth/setup", "/api/auth/login"):
            req = urllib.request.Request(target + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
        raise AssertionError("neither setup nor login worked")
    if hasattr(target, "request") and not hasattr(target, "post"):  # a Playwright Page
        base = getattr(target, "base", None) or target.url.split("/#")[0].rstrip("/")
        r = target.request.post(base + "/api/auth/setup", data=body)
        if r.status == 409:
            r = target.request.post(base + "/api/auth/login", data=body)
        assert r.ok, f"sign-in as {name} failed: {r.status} {r.text()[:200]}"
        return r.json()
    r = target.post("/api/auth/setup", json=body)  # a TestClient
    if r.status_code == 409:
        r = target.post("/api/auth/login", json=body)
    assert r.status_code == 200, f"sign-in as {name} failed: {r.status_code} {r.text[:200]}"
    return r.json()


# --- library probes used by more than one module --------------------------------------------------

def forget_identity(ids):
    """Test photos removed behind the viewer's back lose their viewer ids too (the mirror test counts them)."""
    from app.data import vdb
    con = vdb.connect()
    try:
        for i in ids:
            con.execute("DELETE FROM photo_identity WHERE dk_id = ?", (int(i),))
        con.commit()
    finally:
        con.close()


def _src(lib):
    """A JPEG the library knows: (path, photo id), read from digiKam's database so the UI test (another process) can use it too."""
    con = sqlite3.connect(lib["db"])
    try:
        for pid, rel, name in con.execute("SELECT i.id, a.relativePath, i.name FROM Images i JOIN Albums a ON a.id = i.album "
                                          "WHERE i.status = 1 AND i.fileSize > 20000 AND (i.name LIKE '%.jpg' OR i.name LIKE '%.JPG') ORDER BY i.id"):
            path = Path(lib["root"]) / rel.strip("/") / name
            if path.exists() and "zz-" not in str(path):
                return path, pid
    finally:
        con.close()
    raise AssertionError("no JPEG in the fixture library")


def _dated(lib, tmp_path, stamp="1999:12:31 10:00:00"):
    """A copy of a library photo, taken on another day (a new file: a different size and contents)."""
    from app.photos import edits
    p = tmp_path / "trip.jpg"
    shutil.copy(_src(lib)[0], p)
    subprocess.run([*edits.exiftool_cmd(), "-q", "-overwrite_original", f"-exif:DateTimeOriginal={stamp}", f"-exif:CreateDate={stamp}", f"-xmp:DateTimeOriginal={stamp}", f"-xmp:CreateDate={stamp}",
                    "-xmp-dc:Description=import probe", str(p)], check=True)
    return p


def _cleanup(client, lib, rels):
    """Remove the albums `rels` (and their files) the import tests created, then rebuild the index."""
    from app.data import index
    root = Path(lib["root"])
    con = sqlite3.connect(lib["db"])
    for rel in rels:
        for (aid,) in con.execute("SELECT id FROM Albums WHERE relativePath = ? OR relativePath LIKE ?", (rel, rel + "/%")).fetchall():
            forget_identity([r[0] for r in con.execute("SELECT id FROM Images WHERE album = ?", (aid,)).fetchall()])
            con.execute("DELETE FROM ImageInformation WHERE imageid IN (SELECT id FROM Images WHERE album = ?)", (aid,))
            con.execute("DELETE FROM Images WHERE album = ?", (aid,))
            con.execute("DELETE FROM Albums WHERE id = ?", (aid,))
        shutil.rmtree(root / rel.strip("/"), ignore_errors=True)
    con.commit(); con.close()
    try:
        index.build()
    except Exception:  # the UI tests' server rebuilds its own index when it notices
        pass


def _face_jpeg(lib):
    """(photo id, path, (tagid, rect), (w, h, orientation)) of a JPEG with a confirmed face region."""
    from app.data import index
    con = sqlite3.connect(lib["db"])
    try:
        for pid in lib["with_faces"]:
            row = con.execute("SELECT i.name, a.relativePath, ii.width, ii.height, ii.orientation FROM Images i JOIN Albums a ON a.id = i.album "
                              "LEFT JOIN ImageInformation ii ON ii.imageid = i.id WHERE i.id = ? AND i.status = 1", (pid,)).fetchone()
            if not row or not row[0].lower().endswith((".jpg", ".jpeg")) or not row[2]:
                continue
            reg = con.execute("SELECT tagid, value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion'", (pid,)).fetchone()
            if not reg:
                continue
            path = Path(lib["root"]) / row[1].strip("/") / row[0]
            if path.exists():
                m = index.RECT_RE.search(reg[1])
                return pid, path, (reg[0], tuple(map(int, m.groups()))), (row[2], row[3], row[4] or 1)
    finally:
        con.close()
    pytest.skip("no JPEG with a face region in the fixture")


def _info(db, pid):
    """(width/height/orientation, fileSize/modificationDate, {tagid: rect}) of a photo in digiKam's database."""
    from app.data import index
    con = sqlite3.connect(db)
    try:
        ii = con.execute("SELECT width, height, orientation FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()
        im = con.execute("SELECT fileSize, modificationDate FROM Images WHERE id = ?", (pid,)).fetchone()
        regs = con.execute("SELECT tagid, value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion'", (pid,)).fetchall()
        return ii, im, {t: tuple(map(int, index.RECT_RE.search(v).groups())) for t, v in regs}
    finally:
        con.close()
