"""digiKam's file dates: after the viewer rewrites a sidecar with what digiKam's database holds, Images takes
the file's new time and size (as digiKam does), so digiKam does not rescan it and re-import its face regions.
And MySQL date-times are read and written in the convention the library really uses."""
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from app.digikam import dkdb
from app.photos import edits
from app.faces import facemgr


def test_face_change_updates_the_file_date_digikam_compares(client, lib):
    if not edits.available():
        pytest.skip("exiftool is not available here")
    pid = lib["with_faces"][0]
    con = sqlite3.connect(lib["db"])
    rect = con.execute("SELECT value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion' LIMIT 1", (pid,)).fetchone()[0]
    con.close()
    import re
    x, y, w, h = map(int, re.search(r'x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', rect).groups())
    r = facemgr.apply({"op": "move", "image": pid, "rect": [x, y, w, h], "new_rect": [x + 1, y, w, h]}, check_running=False)
    assert "error" not in r and r.get("sidecar") == "written", r
    con = sqlite3.connect(lib["db"])
    md, size = con.execute("SELECT modificationDate, fileSize FROM Images WHERE id = ?", (pid,)).fetchone()
    path = con.execute("SELECT r.specificPath || a.relativePath || '/' || i.name FROM Images i JOIN Albums a ON a.id = i.album "
                       "JOIN AlbumRoots r ON r.id = a.albumRoot WHERE i.id = ?", (pid,)).fetchone()[0]
    con.close()
    from app.data import index
    ic = index.open_index()
    path = ic.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()[0]
    ic.close()
    stamp = dkdb.file_stamp(path)
    assert md == stamp[0] and size == stamp[1], (md, size, stamp)
    side = Path(path + ".xmp")
    assert side.exists() and abs(dt.datetime.fromisoformat(md).timestamp() - side.stat().st_mtime) < 1.5, "the sidecar's time, as digiKam's scanner takes it"
    facemgr.apply({"op": "move", "image": pid, "rect": [x + 1, y, w, h], "new_rect": [x, y, w, h]}, check_running=False)


def test_mysql_dates_follow_the_library_convention(monkeypatch):
    monkeypatch.setattr(dkdb, "is_mysql", lambda: True)
    v = dt.datetime(2006, 10, 16, 19, 22, 47)
    monkeypatch.setenv("DIGIKAM_MYSQL_DATES", "local")
    assert dkdb._norm_value(v) == "2006-10-16T19:22:47.000", "a local-time library is read as it is"
    assert dkdb.db_datetime("2006-10-16T19:22:47.000") == v, "and written as it is"
    monkeypatch.setenv("DIGIKAM_MYSQL_DATES", "utc")
    as_local = v.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
    assert dkdb._norm_value(v) == as_local.strftime("%Y-%m-%dT%H:%M:%S.000")
    assert dkdb.db_datetime(as_local.strftime("%Y-%m-%dT%H:%M:%S.000")) == v


def test_sqlite_library_never_probes_mysql(monkeypatch):
    monkeypatch.delenv("DIGIKAM_MYSQL_DATES", raising=False)
    monkeypatch.setattr(dkdb, "is_mysql", lambda: False)
    monkeypatch.setattr(dkdb, "_detect_mysql_dates", lambda: (_ for _ in ()).throw(AssertionError("probed")))
    assert dkdb.mysql_dates_utc() is False
