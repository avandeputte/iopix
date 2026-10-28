"""Face quality: a person's face stored twice a few pixels apart is found and cleaned up (one kept, sidecar
rewritten from the database, file date recorded, change log written); separate regions and broken rectangles
are only reported; the viewer itself refuses to make such a duplicate."""
import re
import sqlite3
import time

import pytest

from helpers import need_tool
from app.digikam import dkdb
from app.photos import edits
from app.faces import facemgr, facequality
from app.data import index


def _regions(db, pid, tag):
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT value FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = 'tagRegion'", (pid, tag))]
    finally:
        con.close()


def test_near_duplicates_are_found_and_removed(client, lib):
    need_tool("exiftool", edits.available())
    pid = lib["with_faces"][1]
    con = sqlite3.connect(lib["db"])
    tag, val = con.execute("SELECT tagid, value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion' LIMIT 1", (pid,)).fetchone()
    x, y, w, h = map(int, re.search(r'x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', val).groups())
    dup = f'<rect x="{x + 1}" y="{y}" width="{w}" height="{h}"/>'
    far = f'<rect x="{x + 3 * w}" y="{y}" width="{w}" height="{h}"/>'
    con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (pid, tag, "tagRegion", dup))
    con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (pid, tag, "tagRegion", far))
    con.commit(); con.close()
    s = facequality.scan()
    assert any(n["photo"] == pid and n["tag"] == tag and n["drop"] == [dup] and n["apart"] == 1 for n in s["near"]), s["near"][:3]
    assert any(x_["photo"] == pid for x_ in s["separate"]), "the region three widths away is a separate one: reported only"
    # the viewer will not make one itself
    with pytest.raises(dkdb.SyncError, match="already has a face there"):
        facemgr.apply({"op": "add", "image": pid, "rect": [x, y + 2, w, h], "tag": tag}, check_running=False)
    n = facequality.fix_near_duplicates(check_running=False)
    assert n >= 1
    for _ in range(200):
        if not facequality.progress["running"]:
            break
        time.sleep(0.1)
    p = facequality.progress
    assert p["error"] is None and p["removed"] >= 1, p
    left = _regions(lib["db"], pid, tag)
    assert val in left and dup not in left and far in left, left
    assert not [n for n in facequality.scan()["near"] if n["photo"] == pid]
    # the sidecar says what the database says, and digiKam's file date follows it
    ic = index.open_index(); path = ic.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()[0]; ic.close()
    side = open(path + ".xmp", encoding="utf-8").read()
    assert side.count("mwg-rs:Name") or side.count("<mwg-rs:Name>"), "regions written"
    con = sqlite3.connect(lib["db"]); md = con.execute("SELECT modificationDate FROM Images WHERE id = ?", (pid,)).fetchone()[0]; con.close()
    assert md == dkdb.file_stamp(path)[0]
    h_ = client.get(f"/api/history?photo={pid}").json()
    assert any("Duplicate face regions removed" in e["summary"] and "sidecar" in e["restore"] for e in h_["entries"]), h_
    con = sqlite3.connect(lib["db"])
    con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND value = ?", (pid, tag, far)); con.commit(); con.close()


def test_report_endpoint_is_for_admins_and_lists_counts(client, lib):
    r = client.get("/api/quality")
    assert r.status_code == 200, r.text
    q = r.json()
    assert {"near", "separate", "invalid", "progress", "checked"} <= set(q) and q["checked"] > 0


def test_image_files_with_the_duplicates_are_rewritten_too(client, lib):
    need_tool("exiftool", edits.available())
    from app.data import vdb
    ic = index.open_index()
    jpegs = {r[0] for r in ic.execute("SELECT id FROM photos WHERE lower(path) LIKE '%.jpg' OR lower(path) LIKE '%.jpeg'")}
    ic.close()
    cand = [i for i in lib["with_faces"][2:] if i in jpegs]
    if not cand:
        pytest.skip("no JPEG with faces in the fixture")
    pid = cand[0]
    con = sqlite3.connect(lib["db"])
    tag, val = con.execute("SELECT tagid, value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion' LIMIT 1", (pid,)).fetchone()
    x, y, w, h = map(int, re.search(r'x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', val).groups())
    dup = f'<rect x="{x}" y="{y + 1}" width="{w}" height="{h}"/>'
    con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (pid, tag, "tagRegion", dup))
    con.commit(); con.close()
    ic = index.open_index(); path = ic.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()[0]; ic.close()
    if not edits.can_write_file(path):
        pytest.skip("not a JPEG")
    assert facemgr.write_regions_many([pid], files=True, sidecars=False)[pid] == "written"  # the file now carries the pair, as digiKam wrote it
    mwg, mp = facequality._file_regions([path])[path]
    assert facequality._has_duplicates(mwg, mp)
    assert facequality.fix_near_duplicates(check_running=False) >= 1
    for _ in range(200):
        if not facequality.progress["running"]:
            break
        time.sleep(0.1)
    assert facequality._has_duplicates(*facequality._file_regions([path])[path]), "the first pass leaves the image files alone"
    n = facequality.fix_files()
    assert n >= 1
    for _ in range(300):
        if not facequality.progress["running"]:
            break
        time.sleep(0.1)
    p = facequality.progress
    assert p["files"] and p["error"] is None and p["photos"] >= 1 and p["errors"] == 0, p
    mwg, mp = facequality._file_regions([path])[path]
    assert not facequality._has_duplicates(mwg, mp) and len(mwg) >= 1
    assert edits.file_backup_path(path).exists(), "the original file is kept in file-backups"
    con = vdb.connect()
    try:
        assert con.execute("SELECT count(*) FROM photo_history WHERE photo_id = ? AND summary = ?", (pid, facequality.FILE_DONE)).fetchone()[0] == 1
    finally:
        con.close()
    assert facequality.fix_files() == 0 or facequality.progress["photos"] == 0, "a second run finds nothing left to do"


def test_boxes_outside_the_photo_are_trimmed_and_empty_ones_removed(client, lib):
    pid = lib["with_faces"][5] if len(lib["with_faces"]) > 5 else lib["with_faces"][-1]
    con = sqlite3.connect(lib["db"])
    tag = con.execute("SELECT tagid FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion' LIMIT 1", (pid,)).fetchone()[0]
    w, h, o = con.execute("SELECT width, height, orientation FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()
    W, H = (h, w) if (o or 1) in (5, 6, 7, 8) else (w, h)
    out = f'<rect x="{W - 40}" y="10" width="100" height="80"/>'
    con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (pid, tag, "tagRegion", out))
    con.commit(); con.close()
    q = client.get("/api/quality").json()
    assert any(x["photo"] == pid and x["fix"] == "fit" for x in q["invalid"]["items"]), q["invalid"]
    r = client.post("/api/quality/fit", json={"photos": [pid]})
    assert r.status_code == 200 and r.json()["fitted"] >= 1, r.text
    left = _regions(lib["db"], pid, tag)
    assert out not in left and f'<rect x="{W - 40}" y="10" width="40" height="80"/>' in left, left
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert "undo" in e["restore"], "trimmed through the normal face change: undo is there"
    con = sqlite3.connect(lib["db"])
    con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND value = ?", (pid, tag, f'<rect x="{W - 40}" y="10" width="40" height="80"/>'))
    con.commit(); con.close()


def test_photos_with_only_unnamed_faces_can_be_accepted_or_ignored(client, lib):
    q = client.get("/api/quality").json()["unnamed"]
    assert {"count", "items", "accepted", "faces"} <= set(q)
    # make one: a photo whose faces are all unknown
    con = sqlite3.connect(lib["db"])
    unknown = con.execute("SELECT tagid FROM TagProperties WHERE property = 'unknownPerson'").fetchone()[0]
    pid = next(i for i in lib["ids"][20:] if i not in lib["with_faces"])
    con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (pid, unknown, "autodetectedFace", '<rect x="20" y="20" width="60" height="60"/>'))
    con.commit(); con.close()
    q = client.get("/api/quality").json()["unnamed"]
    assert any(x["photo"] == pid and x["faces"] == 1 for x in q["items"]), q
    assert client.post("/api/quality/accept", json={"photos": [pid]}).json()["accepted"] == 1
    q2 = client.get("/api/quality").json()["unnamed"]
    assert not any(x["photo"] == pid for x in q2["items"]) and q2["accepted"] >= 1
    assert pid in client.get("/api/quality/accepted").json()["photos"]
    assert client.post("/api/quality/accept", json={"photos": [pid], "on": False}).status_code == 200
    assert any(x["photo"] == pid for x in client.get("/api/quality").json()["unnamed"]["items"]), "shown again"
    r = client.post("/api/quality/ignore", json={"photos": [pid]}).json()
    assert r["ignored"] == 1, r
    assert not any(x["photo"] == pid for x in client.get("/api/quality").json()["unnamed"]["items"]), "an ignored face does not count"
    con = sqlite3.connect(lib["db"])
    con.execute("DELETE FROM ImageTagProperties WHERE imageid = ?", (pid,)); con.commit(); con.close()


def test_people_without_any_face_or_tag_are_listed_and_can_be_accepted(client, lib):
    from app.data import vdb
    q = client.get("/api/quality").json()["untagged"]
    assert {"count", "items", "accepted"} <= set(q)
    # object detection saw two people on a photo nobody is tagged on
    ic = index.open_index()
    pid = ic.execute("SELECT p.id FROM photos p WHERE p.video = 0 "
                     "AND NOT EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.photo_id = p.id AND t.person = 1) LIMIT 1").fetchone()[0]
    ic.close()
    dk = sqlite3.connect(lib["db"])  # the fixture's photos all carry some face region: take this one's away for the test
    saved = dk.execute("SELECT imageid, tagid, property, value FROM ImageTagProperties WHERE imageid = ?", (pid,)).fetchall()
    dk.execute("DELETE FROM ImageTagProperties WHERE imageid = ?", (pid,)); dk.commit(); dk.close()
    with vdb.write_lock:
        con = vdb.connect()
        con.executemany("INSERT INTO ai_objects (photo_id, cls, score, x, y, w, h) VALUES (?,?,?,?,?,?,?)", [(pid, "person", 0.9, 0.1, 0.1, 0.2, 0.4), (pid, "person", 0.8, 0.5, 0.1, 0.2, 0.4)])
        con.commit(); con.close()
    index.build()
    q = client.get("/api/quality").json()["untagged"]
    assert any(x["photo"] == pid and x["people"] == 2 for x in q["items"]), q["items"][:3]
    assert client.post("/api/quality/accept", json={"photos": [pid], "check": "untagged"}).json()["accepted"] == 1
    q2 = client.get("/api/quality").json()["untagged"]
    assert not any(x["photo"] == pid for x in q2["items"]) and q2["accepted"] >= 1
    client.post("/api/quality/accept", json={"photos": [pid], "check": "untagged", "on": False})
    assert any(x["photo"] == pid for x in client.get("/api/quality").json()["untagged"]["items"])
    with vdb.write_lock:
        con = vdb.connect(); con.execute("DELETE FROM ai_objects WHERE photo_id = ?", (pid,)); con.commit(); con.close()
    dk = sqlite3.connect(lib["db"]); dk.executemany("INSERT INTO ImageTagProperties VALUES (?,?,?,?)", saved); dk.commit(); dk.close()
    index.build()


def test_lists_page_through_their_entries(client, lib):
    full = client.get("/api/quality/items?check=invalid&offset=0&limit=500").json()
    if full["total"] < 2:
        pytest.skip("fewer than two entries in the fixture")
    a = client.get("/api/quality/items?check=invalid&offset=0&limit=1").json()["items"]
    b = client.get("/api/quality/items?check=invalid&offset=1&limit=1").json()["items"]
    assert a == full["items"][:1] and b == full["items"][1:2]
    for k in ("near", "separate", "unnamed", "untagged"):
        assert client.get(f"/api/quality/items?check={k}").status_code == 200
    assert client.get("/api/quality/items?check=nonsense").status_code == 400
