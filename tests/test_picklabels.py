"""Clearing digiKam's pick labels (picklabels.py): the chosen labels leave digiKam's database and the sidecars that
carry them, every photo's change is in the edit log with undo, and nothing else is touched."""
import sqlite3
import time

import pytest

from app.photos import edits, picklabels


def test_clearing_the_rejected_label(client, lib):
    db = sqlite3.connect(lib["db"])
    tag = {n: i for i, n in db.execute("SELECT id, name FROM Tags WHERE name LIKE 'Pick Label %'")}
    if "Pick Label Rejected" not in tag:
        db.close()
        pytest.skip("the scratch library has no pick labels")
    from app.data import index
    ids = [r[0] for r in db.execute("SELECT id FROM Images WHERE status = 1 AND category = 1 ORDER BY id LIMIT 4")]
    rejected, accepted = ids[:3], ids[3:]
    existing = db.execute("SELECT imageid, tagid FROM ImageTags WHERE tagid IN (?, ?)", (tag["Pick Label Rejected"], tag.get("Pick Label Accepted", -1))).fetchall()
    db.executemany("DELETE FROM ImageTags WHERE imageid = ? AND tagid IN (SELECT id FROM Tags WHERE name LIKE 'Pick Label %')", [(i,) for i in ids])
    db.executemany("INSERT INTO ImageTags (imageid, tagid) VALUES (?, ?)", [(i, tag["Pick Label Rejected"]) for i in rejected] + [(i, tag["Pick Label Accepted"]) for i in accepted])
    db.commit()
    rejected = sorted({*rejected, *(i for i, t in existing if t == tag["Pick Label Rejected"])})
    accepted_before = sorted({*accepted, *(i for i, t in existing if t == tag.get("Pick Label Accepted"))})
    db.close()
    index.build()
    con = index.open_index()
    paths = dict(con.execute(f"SELECT id, path FROM photos WHERE id IN ({','.join(map(str, rejected))})").fetchall())
    con.close()
    saved = {}
    for p in paths.values():
        side = edits.sidecar_path(p)
        try:
            saved[side] = open(side, "rb").read()
        except OSError:
            pass
    try:
        one = edits.sidecar_path(paths[rejected[0]])  # a sidecar that says Rejected, as digiKam writes it when told to
        edits.run_exiftool([(one, edits.write_args({"pick": 1}, None, False))])
        assert picklabels._SIDECAR_PICK.search(open(one, encoding="utf-8", errors="replace").read())
        assert client.get("/api/picklabels").json()["counts"]["1"] == len(rejected)  # (json keys are strings)
        assert client.post("/api/picklabels/clear", json={"levels": []}).status_code == 400
        r = client.post("/api/picklabels/clear", json={"levels": [1]}).json()
        assert r["cleared"] == len(rejected)
        for _ in range(300):
            if not picklabels.progress["running"]:
                break
            time.sleep(0.1)
        db = sqlite3.connect(lib["db"])
        assert not db.execute("SELECT 1 FROM ImageTags WHERE tagid = ?", (tag["Pick Label Rejected"],)).fetchone()
        assert sorted(i for (i,) in db.execute("SELECT imageid FROM ImageTags WHERE tagid = ?", (tag.get("Pick Label Accepted", -1),))) == accepted_before
        db.close()
        assert client.get("/api/picklabels").json()["counts"]["1"] == 0
        for side in saved:  # no sidecar still says Rejected
            text = open(side, encoding="utf-8", errors="replace").read()
            assert 'PickLabel="1"' not in text and "<digiKam:PickLabel>1<" not in text
        assert not picklabels._SIDECAR_PICK.search(open(one, encoding="utf-8", errors="replace").read())
        entry = client.get(f"/api/history?photo={rejected[0]}").json()["entries"][0]
        assert "Pick label Rejected cleared" in entry["summary"] and "undo" in entry["restore"]
    finally:
        db = sqlite3.connect(lib["db"])
        db.executemany("DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?", [(i, tag["Pick Label Rejected"]) for i in ids] + [(i, tag["Pick Label Accepted"]) for i in accepted])
        db.executemany("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?, ?)", existing)
        db.commit()
        db.close()
        for side, data in saved.items():
            with open(side, "wb") as f:
                f.write(data)
        index.build()
