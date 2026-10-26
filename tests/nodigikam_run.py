"""Run by test_no_digikam.py in a process of its own (the app reads its environment once, at import): iopix with no
digiKam database at all (DIGIKAM_DB_TYPE=none) on a folder of photos nobody catalogued. Prints one JSON object with
what it saw. Usage: nodigikam_run.py <empty scratch folder>"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

tmp = Path(sys.argv[1])
photos, data = tmp / "photos", tmp / "data"
(photos / "2024" / "Summer").mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
from PIL import Image  # noqa: E402
for i, name in enumerate(("beach.jpg", "dunes.jpg")):
    Image.new("RGB", (320, 240), (40 + 80 * i, 120, 200)).save(photos / "2024" / "Summer" / name, quality=90)
beach_file = photos / "2024" / "Summer" / "beach.jpg"
subprocess.run(["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2024:07:14 10:30:00", "-XMP-digiKam:TagsList=Trips/Coast",
                "-XMP-dc:Description=At the beach", str(beach_file)], check=True)
os.environ.update({"DATA_DIR": str(data), "PHOTOS_ROOT": str(photos), "DIGIKAM_DB_TYPE": "none", "DIGIKAM_DB_DIR": str(tmp / "no-digikam"),
                   "PREWARM": "0", "SEMANTIC": "0", "ANALYSIS": "0", "CAPTIONS": "0", "REINDEX_POLL": "3600", "AUTO_MODELS": "",
                   "DIGIKAM_CHECK": "ask", "BG_THREADS": "1", "HF_HUB_OFFLINE": "1", "SCANNER_LOOP": "0", "UPDATE_CHECK": "0"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.core import _refresh, state  # noqa: E402
from app.data import index  # noqa: E402
from app.digikam import library  # noqa: E402
from app.photos import scanner  # noqa: E402


def rebuild():
    index.build()
    state["version"] = None
    _refresh()


out = {}
with TestClient(app) as c:
    s = c.get("/api/status").json()
    out["error"] = s.get("error")
    out["status"] = {k: s["digikam"][k] for k in ("sync", "none")} if s.get("digikam") else None
    out["scanner_on"] = scanner.enabled()
    res = scanner.scan(check_running=False)
    out["scan"] = {k: res[k] for k in ("added", "error")}
    rebuild()
    p = c.get("/api/photos").json()
    out["photos"] = p["count"]
    out["tags"] = sorted(t["full"] for t in c.get("/api/meta").json()["tags"] if not t.get("person"))
    beach = next(i for i in p["id"] if c.get(f"/api/metadata/{i}").json()["path"] == str(beach_file))
    con = library.connect()
    people = con.execute("SELECT id FROM Tags WHERE name = 'People' AND pid = 0").fetchone()[0]
    con.close()
    r = c.post("/api/tags/op", json={"op": "add", "parent": people, "name": "Sam Lee"})
    out["person"] = r.status_code
    r = c.post("/api/faces/op", json={"op": "add", "image": beach, "rect": [20, 20, 60, 60], "tag": r.json().get("id")})
    out["face"] = r.status_code
    out["edit"] = c.post("/api/edit", json={"ids": [beach], "changes": {"rating": 4, "caption": "Low tide"}}).status_code
    # only the year is known: its tag is created in the library, the date becomes 1 January
    out["approx"] = c.post("/api/edit", json={"ids": [beach], "changes": {"date_precision": "year", "taken": "1992"}}).status_code
    out["apply"] = c.post("/api/edits/db", json={}).status_code
    for _ in range(50):  # the face's sidecar is rewritten in the background
        side = beach_file.with_name(beach_file.name + ".xmp")
        if side.exists() and "Sam Lee" in side.read_text():
            break
        time.sleep(0.1)
    rebuild()
    con = library.connect()
    out["library"] = {
        "rating": con.execute("SELECT rating FROM ImageInformation WHERE imageid = ?", (beach,)).fetchone()[0],
        "caption": [r[0] for r in con.execute("SELECT comment FROM ImageComments WHERE imageid = ? AND type = 1", (beach,))],
        "faces": con.execute("SELECT count(*) FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion'", (beach,)).fetchone()[0],
    }
    out["approx_library"] = [str(con.execute("SELECT creationDate FROM ImageInformation WHERE imageid = ?", (beach,)).fetchone()[0])[:10],
                             [r[0] for r in con.execute("SELECT t.name FROM ImageTags it JOIN Tags t ON t.id = it.tagid WHERE it.imageid = ?", (beach,))]]
    con.close()
    out["pending"] = c.get("/api/edits/db").json()["pending"]
    out["faces_in_index"] = c.get("/api/photos").json()["faces"]
    side = beach_file.with_name(beach_file.name + ".xmp").read_text()
    out["sidecar"] = {"person": "People/Sam Lee" in side, "caption": "Low tide" in side, "keyword": "Trips/Coast" in side}
    dunes = next(i for i in p["id"] if i != beach)
    r = c.post("/api/photos/trash", json={"ids": [dunes]})
    out["trash"] = [r.status_code, r.json().get("trashed") == [dunes]]
    for _ in range(100):
        items = c.get("/api/trash").json()["items"]
        if items:
            break
        time.sleep(0.1)
    r = c.post("/api/trash/restore", json={"infos": [items[0]["info"]]})
    out["restore"] = [r.status_code, r.json().get("restored") == [dunes], (photos / "2024" / "Summer" / "dunes.jpg").exists()]
    out["digikam_files"] = sorted(str(p.relative_to(tmp)) for p in tmp.rglob("digikam4.db*"))
print("RESULT " + json.dumps(out), flush=True)
