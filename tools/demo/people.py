"""The invented names of the demo library's recurring people, kept so a rebuilt library gets them back.

    .venv/bin/python tools/demo/people.py export   # the demo server's named faces -> tools/demo/people.json
    .venv/bin/python tools/demo/people.py apply    # people.json -> person tags and named faces on the demo server

Faces are stored by file (relative to the library) and rectangle, so they survive a rebuild that produces the same
files; a face the detector no longer finds at that spot is added as a named face.
"""
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def call(base, path, body=None):
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json"} if body is not None else {})
    return json.load(urllib.request.urlopen(req))


def export(base, out):
    people = sorted((t for t in call(base, "/api/meta")["tags"] if t.get("person") and t.get("nAll")), key=lambda t: t["name"])
    data = {}
    for t in people:
        ids = call(base, "/api/photos?q=" + urllib.parse.quote(t["name"]))["id"]
        faces = []
        for i in ids:
            d = call(base, f"/api/photo/{i}")
            rel = f"{d['album']['path'].strip('/')}/{d['name']}"
            faces += [[rel, f["x"], f["y"], f["w"], f["h"]] for f in d.get("faces") or [] if f.get("tag") == t["id"]]
        if faces:
            data[t["name"]] = faces
    out.write_text("{\n" + ",\n".join(f" {json.dumps(n)}: [\n" + ",\n".join("  " + json.dumps(f, ensure_ascii=False) for f in fs) + "\n ]"
                                       for n, fs in data.items()) + "\n}\n")  # a face per line
    print(f"{len(data)} people, {sum(map(len, data.values()))} faces -> {out}")


def apply(base, src):
    data = json.loads(src.read_text())
    by_path = {}
    ids = call(base, "/api/photos")["id"]
    for i in ids:
        d = call(base, f"/api/photo/{i}")
        by_path[f"{d['album']['path'].strip('/')}/{d['name']}"] = i
    existing = {t["name"]: t["id"] for t in call(base, "/api/meta")["tags"] if t.get("person")}
    ops, missing = [], 0
    for name, faces in data.items():
        tid = existing.get(name) or call(base, "/api/faces/person", {"name": name, "parent": 0})["id"]
        for rel, x, y, w, h in faces:
            if rel not in by_path:
                missing += 1
                continue
            ops.append({"op": "name", "image": by_path[rel], "rect": [x, y, w, h], "tag": tid})
    r = call(base, "/api/faces/batch", {"ops": ops}) if ops else {"done": 0, "failed": []}
    print(f"{r['done']} faces named, {len(r['failed'])} failed, {missing} files not in this library")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["export", "apply"])
    ap.add_argument("--base", default="http://127.0.0.1:8610")
    ap.add_argument("--file", default=str(HERE / "people.json"))
    a = ap.parse_args()
    (export if a.action == "export" else apply)(a.base, Path(a.file))


if __name__ == "__main__":
    main()
