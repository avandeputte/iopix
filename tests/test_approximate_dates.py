"""Approximate dates (app/photos/dateprecision.py): only the year (or the month) is known, or nothing. The photo keeps
a full placeholder date where one is required (digiKam's database, the XMP dates digiKam reads), the known part in
XMP photoshop:DateCreated, and a tag under "Date approximate" that every copy carries; the viewer shows "1992" or
"Date unknown", and the change log undoes it."""
import sqlite3
import time

from app.data import index
from app.photos import dateprecision, edits
from test_metadata_roundtrip import _apply, _diff, _photo, _read


def _detail(client, pid, want):
    for _ in range(40):
        d = client.get(f"/api/photo/{pid}").json()
        if d.get("datePrecision") == want:
            return d
        time.sleep(0.25)
    return d


def _db(lib, pid):
    con = sqlite3.connect(lib["db"])
    try:
        when = con.execute("SELECT creationDate FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()[0]
        tags = [r[0] for r in con.execute("SELECT t.name FROM ImageTags it JOIN Tags t ON t.id = it.tagid WHERE it.imageid = ?", (pid,))]
    finally:
        con.close()
    return when, tags


def test_placeholders():
    assert dateprecision.placeholder("year", "1992") == "1992-01-01T00:00:00"
    assert dateprecision.placeholder("month", "1992-05") == "1992-05-01T00:00:00"
    assert dateprecision.placeholder("month", "1992-05-17T10:11:12") == "1992-05-01T00:00:00"
    assert dateprecision.placeholder("unknown", "1974-01-01T00:00:00") == "1974-01-01T00:00:00", "an unknown date stays where it is"
    assert dateprecision.placeholder("year", "") is None
    assert dateprecision.xmp_date("year", "1992-01-01T00:00:00") == "1992" and dateprecision.xmp_date("unknown", "x") == ""
    assert dateprecision.of_paths(["Where/Belgium", dateprecision.TAGS["year"], dateprecision.TAGS["unknown"]]) == "unknown"
    assert dateprecision.of_paths(["Where/Belgium"]) is None


def test_year_only_month_and_unknown_round_trip_and_undo(client, lib):
    p = _photo(lib)
    before_db, _ = _db(lib, p["id"])
    try:
        # only the year: 1 January in every full date, "1992" in IPTC Core's Date Created, the tag (created on the way)
        r = client.post("/api/edit", json={"ids": [p["id"]], "changes": {"date_precision": "year", "taken": "1992"}})
        assert r.status_code == 200 and r.json()["edited"] == 1, r.text
        side = _read(p["path"] + ".xmp", "-XMP-exif:DateTimeOriginal", "-XMP-photoshop:DateCreated", "-XMP-digiKam:TagsList")
        assert side["DateTimeOriginal"] == "1992:01:01 00:00:00", side
        assert str(side["DateCreated"]) == "1992", side
        assert dateprecision.TAGS["year"] in ([side["TagsList"]] if isinstance(side["TagsList"], str) else side["TagsList"])
        d = _detail(client, p["id"], "year")
        assert d["datePrecision"] == "year" and d["taken"].startswith("1992-01-01"), d
        _apply(p)
        when, tags = _db(lib, p["id"])
        assert str(when).startswith("1992-01-01") and "Year only" in tags, (when, tags)
        assert _diff(p) == {}, _diff(p)
        index.build()
        con = index.open_index()
        row = con.execute("SELECT dprec, md FROM photos WHERE id = ?", (p["id"],)).fetchone()
        con.close()
        assert row == (2, None), "year only, and no day for On this day"
        grid = client.get("/api/photos", params={"album": d["album"]["id"]}).json()
        assert grid["dp"][str(grid["id"].index(p["id"]))] == 2

        # the month becomes known: the 1st of it, the Year only tag replaced
        assert client.post("/api/edit", json={"ids": [p["id"]], "changes": {"date_precision": "month", "taken": "1992-05"}}).json()["edited"] == 1
        side = _read(p["path"] + ".xmp", "-XMP-exif:DateTimeOriginal", "-XMP-photoshop:DateCreated", "-XMP-digiKam:TagsList")
        assert side["DateTimeOriginal"] == "1992:05:01 00:00:00" and str(side["DateCreated"]) == "1992:05", side
        tl = [side["TagsList"]] if isinstance(side["TagsList"], str) else side["TagsList"]
        assert dateprecision.TAGS["month"] in tl and dateprecision.TAGS["year"] not in tl, tl
        _apply(p)
        assert _diff(p) == {}, _diff(p)

        # unknown: the date stays (where digiKam sorts it), Date Created goes, the tag says unknown
        assert client.post("/api/edit", json={"ids": [p["id"]], "changes": {"date_precision": "unknown"}}).json()["edited"] == 1
        side = _read(p["path"] + ".xmp", "-XMP-exif:DateTimeOriginal", "-XMP-photoshop:DateCreated", "-XMP-digiKam:TagsList")
        assert side["DateTimeOriginal"] == "1992:05:01 00:00:00" and "DateCreated" not in side, side
        assert _detail(client, p["id"], "unknown")["datePrecision"] == "unknown"
        _apply(p)
        when, tags = _db(lib, p["id"])
        assert str(when).startswith("1992-05-01") and "Unknown" in tags and "Month and year" not in tags, (when, tags)
        assert _diff(p) == {}, _diff(p)
        index.build()

        # the change log takes it back to month and year
        e = client.get("/api/history", params={"photo": p["id"], "action": "edit"}).json()["entries"][0]
        assert e["action"] == "edit" and "unknown" in e["summary"], e
        assert client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"}).status_code == 200
        side = _read(p["path"] + ".xmp", "-XMP-photoshop:DateCreated", "-XMP-digiKam:TagsList")
        tl = [side["TagsList"]] if isinstance(side["TagsList"], str) else side["TagsList"]
        assert str(side.get("DateCreated")) == "1992:05" and dateprecision.TAGS["month"] in tl and dateprecision.TAGS["unknown"] not in tl, side
        _apply(p)
        assert _diff(p) == {}, _diff(p)

        # an exact date again: every mark comes off
        assert client.post("/api/edit", json={"ids": [p["id"]], "changes": {"date_precision": "exact", "taken": "1992-05-17T10:11:12"}}).json()["edited"] == 1
        side = _read(p["path"] + ".xmp", "-XMP-photoshop:DateCreated", "-XMP-digiKam:TagsList")
        assert str(side["DateCreated"]).startswith("1992:05:17 10:11:12"), side
        assert not any(t.startswith(dateprecision.ROOT) for t in ([side.get("TagsList")] if isinstance(side.get("TagsList"), str) else side.get("TagsList") or []))
        _apply(p)
        assert _diff(p) == {}, _diff(p)
    finally:
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"date_precision": "exact", "taken": str(before_db)[:19]}})
        _apply(p)
        index.build()


def test_batch_keeps_each_photos_own_year(client, lib):
    con = index.open_index()
    rows = con.execute("SELECT id, taken FROM photos WHERE video = 0 AND taken IS NOT NULL ORDER BY id DESC LIMIT 2").fetchall()
    con.close()
    ids = [r[0] for r in rows]
    try:
        r = client.post("/api/edit", json={"ids": ids, "changes": {"date_precision": "year"}})
        assert r.status_code == 200 and r.json()["edited"] == 2, r.text
        pend = edits.pending()
        for pid, taken in rows:
            assert pend[pid]["taken"] == f"{taken[:4]}-01-01T00:00:00", (pend[pid], taken)
    finally:
        client.post("/api/edit/revert", json={"ids": ids})
        edits.forget(ids)


def test_bad_precision_is_refused(client, lib):
    assert client.post("/api/edit", json={"ids": [lib["ids"][0]], "changes": {"date_precision": "decade"}}).status_code == 400
    assert client.post("/api/edit", json={"ids": [lib["ids"][0]], "changes": {"date_precision": "year", "taken": "92"}}).status_code == 400


def test_the_mcp_tool_counts_then_marks_by_year(client, lib):
    from app import mcp_server, updates
    tok = updates.admin_token()
    con = index.open_index()
    pid, taken = con.execute("SELECT id, taken FROM photos WHERE video = 0 AND taken IS NOT NULL ORDER BY id LIMIT 1").fetchone()
    n_year = con.execute("SELECT count(*) FROM photos WHERE substr(taken, 1, 4) = ?", (taken[:4],)).fetchone()[0]
    con.close()
    r = mcp_server.set_date_precision(tok, "unknown", years=[int(taken[:4])])
    assert r["photos"] == n_year and r["per_year"] == {taken[:4]: n_year} and "started" not in r, r
    try:
        r = mcp_server.set_date_precision(tok, "unknown", ids=[pid], dry_run=False)
        assert r["started"] == 1, r
        for _ in range(100):
            if not mcp_server._precision_job["running"]:
                break
            time.sleep(0.1)
        assert mcp_server._precision_job["done"] == 1 and not mcp_server._precision_job["error"], mcp_server._precision_job
        assert edits.pending()[pid]["date_precision"] == "unknown" and edits.pending()[pid]["taken"] == taken[:19]
    finally:
        client.post("/api/edit/revert", json={"ids": [pid]})
        edits.forget([pid])
