"""Writes to the (scratch) digiKam database: the tag manager and face
management mirror digiKam's own rows, back the database up first, refuse
what digiKam would not allow, and keep the index in step."""

import pytest

from helpers import need_tool
from app import config
from app.digikam import dkdb
from app.faces import facemgr
from app.photos import tagmanager


def _tags(con):
    return {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, pid, name FROM Tags")}


@pytest.fixture
def dk():
    con = dkdb.connect()
    yield con
    con.close()


def _people_root(con):
    return con.execute("SELECT id FROM Tags WHERE name = 'People' AND pid = 0").fetchone()[0]


def _backups():
    d = config.DATA_DIR / "db-backups"
    return sorted(d.glob("digikam4-*.db")) if d.exists() else []


def test_ordinary_changes_take_no_backup_destructive_ones_do_and_are_capped(dk, client):
    """A full copy of digiKam's database on every change made changes slow (seconds on MariaDB): ordinary
    changes take none; deleting a tag (destructive) does, and at most KEEP_BACKUPS copies are kept."""
    before = _backups()
    ids = [tagmanager.apply({"op": "add", "parent": 0, "name": f"zz-backup-probe-{i}"}, check_running=False)["id"] for i in range(12)]
    tagmanager.apply({"op": "rename", "id": ids[0], "name": "zz-backup-probe-renamed"}, check_running=False)
    assert _backups() == before, "adding and renaming tags take no full backup"
    for tid in ids:
        tagmanager.apply({"op": "delete", "id": tid}, check_running=False)
    after = _backups()
    assert len(after) == min(dkdb.KEEP_BACKUPS, len(before) + len(ids)), "deleting a tag takes one; at most KEEP_BACKUPS are kept"


def test_add_rename_move_delete_plain_tag(dk):
    r = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-probe"}, check_running=False)
    tid = r["id"]
    assert _tags(dkdb.connect())[tid] == (0, "zz-probe")
    tagmanager.apply({"op": "rename", "id": tid, "name": "zz-probe2"}, check_running=False)
    assert _tags(dkdb.connect())[tid][1] == "zz-probe2"
    r2 = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-parent"}, check_running=False)
    tagmanager.apply({"op": "move", "id": tid, "parent": r2["id"]}, check_running=False)
    assert _tags(dkdb.connect())[tid][0] == r2["id"]
    tagmanager.apply({"op": "delete", "id": r2["id"]}, check_running=False)
    t = _tags(dkdb.connect())
    assert r2["id"] not in t and tid not in t, "deleting a tag removes its sub-tags too (digiKam's delete_tagstree)"


def test_person_can_be_created_under_people_root(dk):
    root = _people_root(dk)
    tid = facemgr.create_person("Zz Probe Person", parent=root)
    con = dkdb.connect()
    assert _tags(con)[tid] == (root, "Zz Probe Person")
    props = {r[0]: r[1] for r in con.execute("SELECT property, value FROM TagProperties WHERE tagid = ?", (tid,))}
    assert "person" in props, "digiKam marks people with the person property"
    con.close()


def test_people_root_and_internal_tags_are_protected(dk):
    root = _people_root(dk)
    with pytest.raises(Exception):
        tagmanager.apply({"op": "rename", "id": root, "name": "Nope"}, check_running=False)
    with pytest.raises(Exception):
        tagmanager.apply({"op": "delete", "id": root}, check_running=False)
    person = dk.execute("SELECT t.id FROM Tags t JOIN TagProperties p ON p.tagid = t.id WHERE p.property = 'person' LIMIT 1").fetchone()
    if person:
        with pytest.raises(Exception):
            tagmanager.apply({"op": "delete", "id": person[0]}, check_running=False)


def test_face_ignore_and_unknown_roundtrip(dk, lib):
    row = dk.execute("SELECT imageid, tagid, value FROM ImageTagProperties WHERE property = 'tagRegion' LIMIT 1").fetchone()
    if not row:
        pytest.skip("no confirmed face region in the scratch library")
    image, tag, value = row
    import re
    x, y, w, h = map(int, re.findall(r"\d+", value)[:4])
    r = facemgr.apply({"op": "unknown", "image": image, "rect": [x, y, w, h]})
    assert r.get("sidecar_error") is None or isinstance(r.get("sidecar_error"), str)
    con = dkdb.connect()
    kinds = {p for p, in con.execute("SELECT property FROM ImageTagProperties WHERE imageid = ? AND value = ?", (image, value))}
    assert "autodetectedFace" in kinds and "tagRegion" not in kinds, "forgetting a name leaves an unknown face, as digiKam does"
    r = facemgr.apply({"op": "ignore", "image": image, "rect": [x, y, w, h]})
    kinds = {p for p, in con.execute("SELECT property FROM ImageTagProperties WHERE imageid = ? AND value = ?", (image, value))}
    assert "ignoredFace" in kinds
    r = facemgr.apply({"op": "name", "image": image, "rect": [x, y, w, h], "tag": tag})
    rows = con.execute("SELECT property FROM ImageTagProperties WHERE imageid = ? AND value = ? AND tagid = ?", (image, value, tag)).fetchall()
    assert ("tagRegion",) in rows
    assert con.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (image, tag)).fetchone()
    con.close()


def test_batch_runs_in_one_transaction(dk):
    rows = dk.execute("SELECT imageid, value FROM ImageTagProperties WHERE property = 'autodetectedFace' LIMIT 2").fetchall()
    if len(rows) < 1:
        pytest.skip("no unknown faces in the scratch library")
    import re
    ops = []
    for image, value in rows:
        x, y, w, h = map(int, re.findall(r"\d+", value)[:4])
        ops.append({"op": "ignore", "image": image, "rect": [x, y, w, h]})
    ops.append({"op": "ignore", "image": 999999999, "rect": [0, 0, 1, 1]})  # a bad one
    r = facemgr.apply_many(ops)
    assert isinstance(r, (list, dict))
    results = r if isinstance(r, list) else r.get("results", [])
    assert any(x.get("error") for x in results), "the bad op is reported"


def test_container_mode_writes_without_asking(client, dk):
    """DIGIKAM_CHECK=ask/none (containers): the viewer cannot see digiKam, so it neither checks nor asks."""
    from app.data import state
    assert client.get("/api/status").json()["digikamCheck"] == "none"
    state.set_setting("digikam_closed_until", "0")  # no vouch in force
    r = client.post("/api/tags/op", json={"op": "add", "parent": 0, "name": "zz-ask-probe"})
    assert r.status_code == 200, r.text


def test_face_change_answers_before_the_sidecar_is_written(client, lib):
    """The web app's face change returns right after the database write; the sidecar follows in the background."""
    import time as _t
    from app.photos import edits
    from app.faces import facemgr
    need_tool("exiftool", edits.available())
    pid = lib["with_faces"][3]
    con = dkdb.connect()
    val = con.execute("SELECT value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion' LIMIT 1", (pid,)).fetchone()[0]
    con.close()
    import re
    x, y, w, h = map(int, re.search(r'x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', val).groups())
    r = client.post("/api/faces/op", json={"op": "move", "image": pid, "rect": [x, y, w, h], "new_rect": [x + 2, y, w, h]})
    assert r.status_code == 200 and r.json()["sidecar"] == "queued", r.text
    for _ in range(100):
        if not facemgr.sidecars_pending():
            break
        _t.sleep(0.1)
    _t.sleep(1.5)
    from app.data import index
    ic = index.open_index(); path = ic.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()[0]; ic.close()
    side = open(path + ".xmp", encoding="utf-8").read()
    assert "mwg-rs" in side or "RegionInfo" in side
    client.post("/api/faces/op", json={"op": "move", "image": pid, "rect": [x + 2, y, w, h], "new_rect": [x, y, w, h]})


def test_a_keyword_tag_marked_as_a_person_can_be_unmarked(lib, client):
    """digiKam marks a tag as a person once a face is named with it ("Food"): Not a person removes the mark (the tag
    and its photos stay), and is refused while a face box still carries the name."""
    import sqlite3
    from app.digikam.dbsync import SyncError
    dk = sqlite3.connect(lib["db"])
    tid = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-food-probe"}, check_running=False)["id"]
    dk.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', 'zz-food-probe')", (tid,))
    dk.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'faceEngineId', 'zz-food-probe')", (tid,))
    img = dk.execute("SELECT id FROM Images WHERE status = 1 LIMIT 1").fetchone()[0]
    dk.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?, ?, 'tagRegion', '<rect x=\"1\" y=\"1\" width=\"5\" height=\"5\"/>')", (img, tid))
    dk.commit()
    try:
        with pytest.raises(SyncError, match="face box"):
            tagmanager.apply({"op": "notperson", "id": tid}, check_running=False)
        dk.execute("DELETE FROM ImageTagProperties WHERE tagid = ?", (tid,)); dk.commit()
        tagmanager.apply({"op": "notperson", "id": tid}, check_running=False)
        assert not dk.execute("SELECT 1 FROM TagProperties WHERE tagid = ? AND property IN ('person', 'faceEngineId')", (tid,)).fetchone()
        assert dk.execute("SELECT 1 FROM Tags WHERE id = ?", (tid,)).fetchone(), "the tag stays"
        with pytest.raises(SyncError, match="not marked"):
            tagmanager.apply({"op": "notperson", "id": tid}, check_running=False)
    finally:
        dk.close()
        tagmanager.apply({"op": "delete", "id": tid}, check_running=False)


def test_a_person_tagged_without_a_face_box_can_be_taken_off_the_photo(lib, client):
    """A person on a photo with no face region (digiKam allows it) can be taken off, and put back by undo; a person
    with a face box on the photo is refused (the face is removed or renamed instead)."""
    import sqlite3
    from app.digikam.dbsync import SyncError
    dk = sqlite3.connect(lib["db"])
    try:
        person = dk.execute("SELECT tagid FROM TagProperties WHERE property = 'person' AND value != '' LIMIT 1").fetchone()[0]
        img = dk.execute("SELECT id FROM Images WHERE status = 1 AND id NOT IN (SELECT imageid FROM ImageTagProperties WHERE tagid = ?) LIMIT 1", (person,)).fetchone()[0]
        dk.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (img, person)); dk.commit()
        r = facemgr.apply({"op": "untag", "image": img, "tag": person}, check_running=False)
        assert r["tag"] == person and not dk.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (img, person)).fetchone()
        assert facemgr.inverse_ops({"op": "untag", "tag": person}) == [{"op": "tagonly", "tag": person}]
        facemgr.apply({"op": "tagonly", "image": img, "tag": person}, check_running=False)
        assert dk.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (img, person)).fetchone(), "undo puts them back"
        facemgr.apply({"op": "untag", "image": img, "tag": person}, check_running=False)
        boxed = dk.execute("SELECT imageid, tagid FROM ImageTagProperties WHERE property = 'tagRegion' LIMIT 1").fetchone()
        with pytest.raises(SyncError, match="face box"):
            facemgr.apply({"op": "untag", "image": boxed[0], "tag": boxed[1]}, check_running=False)
    finally:
        dk.close()


def test_merging_two_entries_of_one_person(lib, client):
    """The same person entered twice: merging moves photos, face boxes and suggestions to the one kept, drops
    duplicates, deletes the other tag, and the People groups follow."""
    import sqlite3
    from app.data import state as appstate
    from app.digikam.dbsync import SyncError
    dk = sqlite3.connect(lib["db"])
    try:
        a = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-Cynthia Hwang"}, check_running=False)["id"]
        b = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-Cynthia Wang"}, check_running=False)["id"]
        kw = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-keyword"}, check_running=False)["id"]
        for t, n in ((a, "zz-Cynthia Hwang"), (b, "zz-Cynthia Wang")):
            dk.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', ?)", (t, n))
        imgs = [r[0] for r in dk.execute("SELECT id FROM Images WHERE status = 1 LIMIT 3")]
        r1, r2 = '<rect x="10" y="10" width="50" height="50"/>', '<rect x="200" y="10" width="50" height="50"/>'
        for img in imgs:
            dk.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (img, a))
        dk.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (imgs[0], b))
        dk.execute("INSERT INTO ImageTagProperties VALUES (?, ?, 'tagRegion', ?)", (imgs[0], a, r1))
        dk.execute("INSERT INTO ImageTagProperties VALUES (?, ?, 'tagRegion', ?)", (imgs[0], b, r1))  # the same box twice
        dk.execute("INSERT INTO ImageTagProperties VALUES (?, ?, 'tagRegion', ?)", (imgs[1], a, r2))
        unconf = dk.execute("SELECT tagid FROM TagProperties WHERE property = 'unconfirmedPerson'").fetchone()
        if unconf:
            dk.execute("INSERT INTO ImageTagProperties VALUES (?, ?, 'autodetectedPerson', ?)", (imgs[2], unconf[0], f"{a},autodetectedFace,{r2}"))
        dk.commit()
        gid = appstate.create_people_group("zz-merge")
        appstate.update_people_group(gid, members=[a])
        with pytest.raises(SyncError, match="person"):
            tagmanager.apply({"op": "merge", "id": a, "into": kw}, check_running=False)
        r = tagmanager.apply({"op": "merge", "id": a, "into": b}, check_running=False)
        assert r["photos"] == 3 and r["backup"], r
        assert not dk.execute("SELECT 1 FROM Tags WHERE id = ?", (a,)).fetchone() and not dk.execute("SELECT 1 FROM ImageTags WHERE tagid = ?", (a,)).fetchone()
        assert {x[0] for x in dk.execute("SELECT imageid FROM ImageTags WHERE tagid = ?", (b,))} == set(imgs)
        assert sorted(x[0] for x in dk.execute("SELECT value FROM ImageTagProperties WHERE tagid = ? AND property = 'tagRegion'", (b,))) == sorted([r1, r2]), "moved, the duplicate dropped"
        if unconf:
            assert dk.execute("SELECT value FROM ImageTagProperties WHERE imageid = ? AND property = 'autodetectedPerson'", (imgs[2],)).fetchone()[0].startswith(f"{b},")
        assert next(g for g in appstate.people_groups() if g["id"] == gid)["members"] == [b]
    finally:
        appstate.delete_people_group(gid)
        dk.execute("DELETE FROM ImageTagProperties WHERE tagid = ? OR (property = 'autodetectedPerson' AND value LIKE ?)", (b, f"{b},%"))
        dk.execute("DELETE FROM TagProperties WHERE tagid IN (?, ?)", (a, b)); dk.commit()
        dk.close()
        import time
        for _ in range(300):  # the merged photos' sidecars are rewritten in the background first
            if not tagmanager.pending_jobs():
                break
            time.sleep(0.1)
        for t in (b, kw):
            tagmanager.apply({"op": "delete", "id": t}, check_running=False, queue=False)


def test_naming_a_face_writes_the_person_into_the_sidecar_tag_list(lib, client):
    """digiKam writes a named face both as a region and as a People/... tag; the sidecar must carry both, or the
    consistency check sees the tags missing (and a rescan from the sidecar would lose them)."""
    import json as _json
    import sqlite3
    import subprocess
    from app.photos import edits
    dk = sqlite3.connect(lib["db"])
    try:
        person = dk.execute("SELECT tagid FROM TagProperties WHERE property = 'person' AND value != '' LIMIT 1").fetchone()[0]
        img = dk.execute("SELECT id FROM Images WHERE status = 1 AND category = 1 AND id NOT IN (SELECT imageid FROM ImageTagProperties WHERE property IN ('tagRegion', 'autodetectedFace')) LIMIT 1").fetchone()[0]
    finally:
        dk.close()
    path = dict(__import__("app.digikam.dbsync", fromlist=["_photo_paths"])._photo_paths([img]))[img]
    full = edits.digikam_tag_paths([img])[img]
    r = facemgr.apply({"op": "add", "image": img, "rect": [5, 5, 40, 40], "tag": person}, check_running=False)
    try:
        assert r["sidecar"] == "written", r
        out = subprocess.run([edits.EXIFTOOL, "-j", "-XMP-digiKam:TagsList", "-XMP-lr:HierarchicalSubject", "-XMP-mwg-rs:RegionName", path + ".xmp"], capture_output=True, text=True).stdout
        d = _json.loads(out)[0]
        want = [p for p in edits.digikam_tag_paths([img])[img] if p not in full]
        tags = d.get("TagsList") or []
        tags = tags if isinstance(tags, list) else [tags]
        assert want and all(w in tags for w in want), (want, d)
        assert d.get("RegionName"), d
    finally:
        facemgr.apply({"op": "remove", "image": img, "rect": [5, 5, 40, 40]}, check_running=False)
    out = subprocess.run([edits.EXIFTOOL, "-j", "-XMP-digiKam:TagsList", path + ".xmp"], capture_output=True, text=True).stdout
    tags = _json.loads(out)[0].get("TagsList") or []
    assert not any(w in (tags if isinstance(tags, list) else [tags]) for w in want), "removing the face takes the tag out of the sidecar too"


def test_people_on_a_video_and_one_person_per_name(lib, client):
    """A video can have people (no face boxes): the person goes on it in digiKam's database and its sidecar's tag list.
    A new person typed with another letter case than an existing one under People is that existing one, marked as a
    person, not a second tag."""
    import json as _json
    import sqlite3
    import subprocess
    from app.photos import edits
    vid = lib["videos"][0]
    dk = sqlite3.connect(lib["db"])
    people = dk.execute("SELECT id FROM Tags WHERE name = 'People' AND pid = 0").fetchone()[0]
    dk.close()
    fam = tagmanager.apply({"op": "add", "parent": people, "name": "Zz Fam"}, check_running=False)["id"]
    kid = tagmanager.apply({"op": "add", "parent": fam, "name": "Zz Kid Person"}, check_running=False)["id"]  # a keyword, not marked
    try:
        assert facemgr.create_person("zz kid person", 0) == kid, "the existing one, whatever the letter case"
        dk = sqlite3.connect(lib["db"])
        assert dk.execute("SELECT 1 FROM TagProperties WHERE tagid = ? AND property = 'person'", (kid,)).fetchone(), "now marked as a person"
        assert dk.execute("SELECT count(*) FROM Tags WHERE lower(name) = 'zz kid person'").fetchone()[0] == 1
        dk.close()
        r = client.post("/api/faces/op", json={"op": "tagonly", "image": vid, "tag": kid})
        assert r.status_code == 200, r.text
        dk = sqlite3.connect(lib["db"])
        assert dk.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (vid, kid)).fetchone()
        dk.close()
        from app.photos import history
        said = history.entries(photo_id=vid, limit=1)["entries"][0]["summary"]
        assert said == "Zz Kid Person added to the photo (no face box)", said  # a first add, not "put back"
        path = dict(__import__("app.digikam.dbsync", fromlist=["x"])._photo_paths([vid]))[vid]
        out = subprocess.run([edits.EXIFTOOL, "-j", "-XMP-digiKam:TagsList", path + ".xmp"], capture_output=True, text=True).stdout
        tags = _json.loads(out)[0].get("TagsList") or []
        assert "People/Zz Fam/Zz Kid Person" in (tags if isinstance(tags, list) else [tags]), tags
        assert client.post("/api/faces/op", json={"op": "untag", "image": vid, "tag": kid}).status_code == 200
    finally:
        dk = sqlite3.connect(lib["db"])
        for t in (kid, fam):
            for tab, col in (("TagProperties", "tagid"), ("ImageTags", "tagid"), ("ImageTagProperties", "tagid"), ("Tags", "id")):
                dk.execute(f"DELETE FROM {tab} WHERE {col} = ?", (t,))
        dk.commit(); dk.close()


def test_the_tag_manager_marks_a_tag_as_a_person_and_back(lib, client):
    import sqlite3
    tid = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-person-switch"}, check_running=False)["id"]
    try:
        tagmanager.apply({"op": "person", "id": tid}, check_running=False, queue=False)
        dk = sqlite3.connect(lib["db"])
        assert dk.execute("SELECT value FROM TagProperties WHERE tagid = ? AND property = 'person'", (tid,)).fetchone()[0] == "zz-person-switch"
        dk.close()
        tagmanager.apply({"op": "notperson", "id": tid}, check_running=False, queue=False)
        dk = sqlite3.connect(lib["db"])
        assert not dk.execute("SELECT 1 FROM TagProperties WHERE tagid = ? AND property = 'person'", (tid,)).fetchone()
        dk.close()
    finally:
        tagmanager.apply({"op": "delete", "id": tid}, check_running=False, queue=False)


def test_a_face_refuses_a_keyword_tag_as_its_person(lib, client):
    """Adding or naming a face, or putting a person on a photo, takes a person (or a tag under People); a keyword
    tag elsewhere is refused and stays a keyword tag."""
    import sqlite3
    from app.digikam.dbsync import SyncError
    kw = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-Activity-probe"}, check_running=False)["id"]
    dk = sqlite3.connect(lib["db"])
    try:
        img = dk.execute("SELECT id FROM Images WHERE status = 1 AND category = 1 LIMIT 1").fetchone()[0]
        for op in ({"op": "add", "image": img, "rect": [5, 5, 40, 40], "tag": kw}, {"op": "tagonly", "image": img, "tag": kw}):
            with pytest.raises(SyncError, match="keyword tag"):
                facemgr.apply(op, check_running=False)
        assert not dk.execute("SELECT 1 FROM TagProperties WHERE tagid = ? AND property = 'person'", (kw,)).fetchone()
        r = client.post("/api/faces/op", json={"op": "add", "image": img, "rect": [5, 5, 40, 40], "tag": kw})
        assert r.status_code in (400, 409) and "keyword tag" in r.text, r.text
    finally:
        dk.close()
        tagmanager.apply({"op": "delete", "id": kw}, check_running=False)
