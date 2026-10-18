"""Every way the viewer changes faces, location, captions, date/time and tags leaves digiKam's database, the sidecar
(and the image file, when the viewer writes into files) saying the same thing: after each change, and after the change
is applied to digiKam's database, the consistency check finds no difference for that photo."""
import sqlite3
import subprocess
import json
import time

import pytest

from app.digikam import dbsync, dkdb
from app.faces import facemgr
from app.data import index
from app.photos import consistency, edits, history, tagmanager


def _photo(lib, ext=(".jpg", ".jpeg")):
    """A photo of the scratch library whose sidecar is first made to say what digiKam's database says."""
    from app.photos import importer
    con = sqlite3.connect(lib["db"])
    try:
        rows = con.execute("SELECT id, name FROM Images WHERE status = 1 AND category = 1 ORDER BY id DESC").fetchall()
    finally:
        con.close()
    pid = next(i for i, n in rows if n.lower().endswith(ext))
    p = {"id": pid, "path": dict(dbsync._photo_paths([pid]))[pid]}
    edits.forget([pid])
    edits._prepare(p["path"])
    importer.sidecars_from_db([pid])
    return p


def _diff(p):
    """The fields on which digiKam's database and the sidecar disagree for this photo (the viewer does not write into
    image files in these tests, so the file keeps what it had)."""
    r = consistency.compare([{**p, "name": ""}])[0]
    return {f: (v["db"], v["sidecar"]) for f, v in r["fields"].items()
            if v["db"] is not None and v["sidecar"] is not None and not consistency.same_value(f, v["db"], v["sidecar"])}


def _apply(p):
    res = dbsync.apply_pending(pending={p["id"]: edits.pending()[p["id"]]}, check_running=False) if p["id"] in edits.pending() else None
    assert res is None or not res["skipped"], res
    edits.forget([p["id"]])


def _read(path, *tags):
    out = subprocess.run([edits.EXIFTOOL, "-j", "-n", *tags, path], capture_output=True, text=True).stdout
    return json.loads(out)[0]


@pytest.fixture
def pid_tag(lib):
    con = sqlite3.connect(lib["db"])
    try:
        person = con.execute("SELECT tagid FROM TagProperties WHERE property = 'person' AND value != '' LIMIT 1").fetchone()[0]
    finally:
        con.close()
    return person


def test_caption_title_rating_tags_date_location_round_trip(client, lib):
    p = _photo(lib)
    assert _diff(p) == {}, _diff(p)
    kw = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-roundtrip-kw"}, check_running=False)["id"]
    index.build()
    try:
        r = client.post("/api/edit", json={"ids": [p["id"]], "changes": {"caption": "Round trip ünïcode", "title": "A title", "rating": 4,
                                                                           "add_tags": [kw], "taken": "2011-02-03T04:05:06",
                                                                           "gps": {"lat": -33.8567844, "lon": 151.213108, "alt": -12.5}}})
        assert r.status_code == 200 and r.json()["edited"] == 1, r.text
        side = _read(p["path"] + ".xmp", "-XMP-exif:GPSAltitudeRef", "-GPSAltitude", "-GPSLatitude", "-DateTimeOriginal")
        assert side["GPSAltitudeRef"] == 1 and side["GPSAltitude"] == -12.5, "below sea level stays below sea level"
        assert abs(side["GPSLatitude"] + 33.8567844) < 1e-6
        _apply(p)
        assert _diff(p) == {}, _diff(p)
        # a time shift: from the latest value, twice in a row
        for _ in range(2):
            assert client.post("/api/edit", json={"ids": [p["id"]], "changes": {"shift": 3600}}).json()["edited"] == 1
        assert edits.pending()[p["id"]]["taken"] == "2011-02-03T06:05:06", "two shifts add up, even before the index catches up"
        _apply(p)
        assert _diff(p) == {}, _diff(p)
        # a caption in another language survives an edit of the caption
        subprocess.run([edits.EXIFTOOL, "-q", "-overwrite_original", "-XMP-dc:Description-de-DE=Deutsch", p["path"] + ".xmp"])
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"caption": "New caption"}})
        out = subprocess.run([edits.EXIFTOOL, "-a", "-G1", "-XMP-dc:Description*", p["path"] + ".xmp"], capture_output=True, text=True).stdout
        assert "Deutsch" in out and "New caption" in out, out
        _apply(p)
        assert _diff(p) == {}, _diff(p)
        # revert: back to digiKam's values; nothing pending, still consistent
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"caption": "to be reverted", "remove_tags": [kw]}})
        assert client.post("/api/edit/revert", json={"ids": [p["id"]]}).status_code == 200
        assert p["id"] not in edits.pending() and _diff(p) == {}, _diff(p)
    finally:
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"remove_tags": [kw], "gps": None}})
        _apply(p)
        tagmanager.apply({"op": "delete", "id": kw}, check_running=False, queue=False)


def test_faces_and_person_tags_round_trip(client, lib, pid_tag):
    p = _photo(lib)
    assert _diff(p) == {}, _diff(p)
    rect = [20, 20, 80, 80]
    assert client.post("/api/faces/op", json={"op": "add", "image": p["id"], "rect": rect, "tag": pid_tag}).status_code == 200
    facemgr.write_regions_many([p["id"]])  # what the background queue does
    assert _diff(p) == {}, _diff(p)
    # renamed person: the sidecar's region name and tag list follow, and digiKam's face name too
    con = sqlite3.connect(lib["db"])
    old = con.execute("SELECT name FROM Tags WHERE id = ?", (pid_tag,)).fetchone()[0]
    con.close()
    tagmanager.apply({"op": "rename", "id": pid_tag, "name": old + " Zz"}, check_running=False, queue=False)
    try:
        facemgr.write_regions_many([p["id"]])
        side = _read(p["path"] + ".xmp", "-RegionName", "-TagsList")
        names = side.get("RegionName") if isinstance(side.get("RegionName"), list) else [side.get("RegionName")]
        assert old + " Zz" in names, side
        con = sqlite3.connect(lib["db"])
        assert con.execute("SELECT value FROM TagProperties WHERE tagid = ? AND property = 'person'", (pid_tag,)).fetchone()[0] == old + " Zz"
        con.close()
        assert _diff(p) == {}, _diff(p)
    finally:
        tagmanager.apply({"op": "rename", "id": pid_tag, "name": old}, check_running=False, queue=False)
        facemgr.apply({"op": "remove", "image": p["id"], "rect": rect}, check_running=False)
    assert _diff(p) == {}, _diff(p)


def test_a_sync_or_an_old_sidecar_never_takes_a_person_off(client, lib, pid_tag):
    """The bug the audit found: a sidecar that lacks a person's tag (written before the tag list was kept with the
    regions) synced to the database, or put back from the change log, removed the person and their face box."""
    p = _photo(lib)
    assert _diff(p) == {}, _diff(p)
    rect = [30, 30, 90, 90]
    facemgr.apply({"op": "add", "image": p["id"], "rect": rect, "tag": pid_tag}, check_running=False)
    try:
        # the stale sidecar: regions but no people in the tag list
        subprocess.run([edits.EXIFTOOL, "-q", "-overwrite_original", "-XMP-digiKam:TagsList=", "-XMP-lr:HierarchicalSubject=",
                        "-XMP-microsoft:LastKeywordXMP=", "-XMP-mediapro:CatalogSets=", "-XMP-dc:Subject=", p["path"] + ".xmp"])
        # store a report row so sync finds the photo
        rec = consistency.compare([{**p, "name": "x"}])[0]
        from app.data import vdb
        c = vdb.connect()
        c.execute("INSERT OR REPLACE INTO metadata_check (photo_id, stamp, data, mismatch, has_sidecar, file_xmp, checked) VALUES (?,?,?,?,?,?,?)",
                  (p["id"], "x", json.dumps(rec), len(rec["mismatch"]), 1, 0, int(time.time())))
        c.commit(); c.close()
        consistency.sync([p["id"]], "sidecar", ["db"], ["tags"])
        con = sqlite3.connect(lib["db"])
        assert con.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (p["id"], pid_tag)).fetchone(), "the person stays"
        assert con.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = 'tagRegion'", (p["id"], pid_tag)).fetchone(), "and the face box"
        con.close()
        _ = con
    finally:
        facemgr.apply({"op": "remove", "image": p["id"], "rect": rect}, check_running=False)
    _ = history


def test_pending_tag_edits_follow_a_renamed_tag(client, lib):
    p = _photo(lib)
    assert _diff(p) == {}, _diff(p)
    kw = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-rt-old"}, check_running=False)["id"]
    index.build()
    try:
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"add_tags": [kw]}})
        assert "zz-rt-old" in edits.pending()[p["id"]]["add_tags"]
        tagmanager.apply({"op": "rename", "id": kw, "name": "zz-rt-new"}, check_running=False, queue=False)
        assert edits.pending()[p["id"]]["add_tags"] == ["zz-rt-new"], "the pending edit follows the rename"
        _apply(p)
        con = sqlite3.connect(lib["db"])
        assert con.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (p["id"], kw)).fetchone()
        con.close()
    finally:
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"remove_tags": [kw]}})
        _apply(p)
        tagmanager.apply({"op": "delete", "id": kw}, check_running=False, queue=False)


def test_batched_face_changes_log_their_own_rectangles(lib, pid_tag):
    p = _photo(lib)
    assert _diff(p) == {}, _diff(p)
    a, b = [10, 10, 40, 40], [100, 100, 40, 40]
    facemgr.apply_many([{"op": "detected", "image": p["id"], "rect": a}, {"op": "detected", "image": p["id"], "rect": b}], check_running=False)
    try:
        facemgr.apply_many([{"op": "ignore", "image": p["id"], "rect": a}, {"op": "ignore", "image": p["id"], "rect": b}], check_running=False)
        rows = [e for e in history.entries(photo_id=p["id"], limit=2)["entries"]]
        rects = sorted(tuple(e["details"]["rect"]) for e in rows)
        assert rects == sorted([tuple(a), tuple(b)]), rows
    finally:
        facemgr.apply_many([{"op": "remove", "image": p["id"], "rect": a}, {"op": "remove", "image": p["id"], "rect": b}], check_running=False)
    _ = dkdb


def test_an_approximate_position_keeps_its_accuracy_everywhere(client, lib):
    """Only the town is known: the position carries how far off it may be, in the sidecar (GPSHPositioningError), in
    digiKam's database (ImagePositions.accuracy) and in what the viewer shows; an exact position afterwards drops it."""
    p = _photo(lib)
    try:
        r = client.post("/api/edit", json={"ids": [p["id"]], "changes": {"gps": {"lat": 40.3834, "lon": -80.0459, "alt": None, "accuracy": 2000}}})
        assert r.status_code == 200 and r.json()["edited"] == 1, r.text
        side = _read(p["path"] + ".xmp", "-XMP-exif:GPSHPositioningError", "-GPSAltitude")
        assert side["GPSHPositioningError"] == 2000 and "GPSAltitude" not in side, side
        for _ in range(40):
            d = client.get(f"/api/photo/{p['id']}").json()
            if d.get("extra", {}).get("gpsAccuracy"):
                break
            time.sleep(0.25)
        assert d["extra"]["gpsAccuracy"] == 2000, "shown as approximate"
        _apply(p)
        con = sqlite3.connect(lib["db"])
        assert con.execute("SELECT accuracy FROM ImagePositions WHERE imageid = ?", (p["id"],)).fetchone()[0] == 2000
        con.close()
        assert _diff(p) == {}, _diff(p)
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"gps": {"lat": 40.3834, "lon": -80.0459, "alt": 300}}})
        assert "GPSHPositioningError" not in _read(p["path"] + ".xmp", "-XMP-exif:GPSHPositioningError"), "an exact position is not approximate"
        _apply(p)
    finally:
        client.post("/api/edit", json={"ids": [p["id"]], "changes": {"gps": None}})
        _apply(p)
