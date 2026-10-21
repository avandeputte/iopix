"""Metadata consistency: a scan of the scratch library compares database, sidecar and file,
stores the results, reports progress while it runs and a summary when it is done."""
import time

import pytest

from helpers import need_tool
from app.photos import consistency


def _wait_done(timeout=60):
    end = time.time() + timeout
    while time.time() < end and consistency.progress["running"]:
        time.sleep(0.2)
    return not consistency.progress["running"]


def test_whole_library_scan_reports_and_stores(client, lib):
    r = client.post("/api/consistency/scan", content="rescan_all=1", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and r.json()["queued"] > 0, r.text
    assert _wait_done(), "the scan finishes"
    d = client.get("/api/consistency?field=&offset=0&limit=60").json()
    p, s = d["progress"], d["summary"]
    assert p["error"] is None and p["failed"] == 0 and p["rescan_all"] is True and p["done"] == p["total"] > 0, p
    assert s["scanned"] == p["total"] and s["scanned_at"], s
    assert d["total"] == s["mismatched"] == len(d["items"][:60]) or d["total"] > 60


def test_a_failing_batch_is_counted_and_reported(client, monkeypatch):
    def boom(batch):
        raise RuntimeError("exiftool exploded")
    monkeypatch.setattr(consistency, "compare", boom)
    r = client.post("/api/consistency/scan", content="rescan_all=1", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    assert _wait_done()
    p = client.get("/api/consistency").json()["progress"]
    assert p["failed"] == p["total"] > 0 and "exiftool exploded" in p["error"] and p["done"] == p["total"], p


def test_check_new_only_reports_nothing_to_do(client):
    client.post("/api/consistency/scan", content="rescan_all=1", headers={"content-type": "application/x-www-form-urlencoded"})
    assert _wait_done()
    client.post("/api/consistency/scan", content="rescan_all=0", headers={"content-type": "application/x-www-form-urlencoded"})
    assert _wait_done()
    p = client.get("/api/consistency").json()["progress"]
    assert p["total"] == 0 and p["rescan_all"] is False and p["started"], "nothing changed since the full check"


def test_results_naming_a_dropped_field_do_not_break_the_report(client):
    """Stored results from before pick and colour were dropped may still name them: they are left out
    of the counts and the list (a photo that only disagreed on those is not listed)."""
    import json
    from app.ai import analysis
    rec = lambda pid, mism: (pid, "s", json.dumps({"id": pid, "name": f"p{pid}", "path": f"/x/{pid}.jpg", "has_sidecar": True, "file_xmp": False, "file_editable": False,
                                                    "fields": {f: {"db": 0, "sidecar": 1, "file": None, "ok": f not in mism} for f in ("rating", "pick", "title")}, "mismatch": mism}), len(mism), 1, 0, 1)
    with analysis._writer() as con:
        con.execute("DELETE FROM metadata_check")
        con.executemany("INSERT INTO metadata_check VALUES (?,?,?,?,?,?,?)", [rec(900001, ["pick"]), rec(900002, ["pick", "title"])])
    d = client.get("/api/consistency").json()
    assert d["summary"]["mismatched"] == 1 and "pick" not in d["summary"]["by_field"] and d["summary"]["by_field"]["title"] == 1
    assert d["total"] == 1 and d["items"][0]["mismatch"] == ["title"] and "pick" not in d["items"][0]["fields"]
    with analysis._writer() as con:
        con.execute("DELETE FROM metadata_check")


def test_photos_without_a_sidecar_are_listed_and_can_get_one(client, lib):
    import time
    from app.photos import consistency, edits
    need_tool("exiftool", edits.available())
    from pathlib import Path
    pid = next((i for i in lib["ids"] if not Path(client.get(f"/api/photo/{i}").json()["path"] + ".xmp").exists()), None)
    if pid is None:
        pytest.skip("every scratch photo has a sidecar")
    row = client.get(f"/api/photo/{pid}").json()
    consistency.scan([{"id": pid, "path": row["path"], "name": row["name"], "hash": row.get("hash") or "", "size": row["size"], "modified": row["modified"], "video": row["video"]}])
    for _ in range(100):
        if not consistency.progress["running"]:
            break
        time.sleep(0.1)
    r = client.get("/api/consistency", params={"field": "nosidecar", "limit": 5000}).json()
    assert any(x["id"] == pid for x in r["items"])
    assert isinstance(r["summary"]["no_sidecar"], int)
    s = client.post("/api/consistency/sync", json={"ids": [pid], "source": "db", "targets": ["sidecar"], "fields": ["rating", "title", "caption", "tags"]})
    assert s.status_code == 200 and s.json()["synced"] == 1, s.text
    assert Path(row["path"] + ".xmp").exists()
    client.post("/api/edit/revert", json={"ids": [pid]})  # put the library back: the created sidecar is removed


def test_videos_are_checked_against_their_sidecar(client, lib):
    """A video's sidecar is compared with digiKam too (its date was digiKam's import day for some old videos)."""
    from app.data import index
    con = index.open_index()
    try:
        v = con.execute("SELECT id, path, name, hash, size, modified, video FROM photos WHERE video = 1 LIMIT 1").fetchone()
    finally:
        con.close()
    if not v:
        pytest.skip("no video in the scratch library")
    called = []
    orig = consistency.compare
    consistency.compare = lambda batch: called.extend(p["id"] for p in batch) or orig(batch)
    try:
        consistency.scan([dict(zip(("id", "path", "name", "hash", "size", "modified", "video"), v))])
        assert _wait_done()
    finally:
        consistency.compare = orig
    assert called == [v[0]], "the video is compared"
