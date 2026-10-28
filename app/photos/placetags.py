"""Reverse-geocoded place tags: turn each photo's place name (worked out offline
from its GPS position) into real digiKam tags, Places/<Country>/<Region>/<Town>,
so they are searchable in digiKam too.

Creating tags writes digiKam's database (digiKam must be closed; backup first,
via the tag manager). Assigning them goes through the normal edit path, so the
sidecars (and files, if enabled) are written and the assignment waits for
"Apply to digiKam database" like any other edit.
"""
import threading

from app import jobs
from app.digikam import dbsync
from app.photos import edits, tagmanager
from app.data import index
from app.digikam.dbsync import SyncError

ROOT = "Places"
progress = jobs.Progress("placetags", created=0)


def _part(name: str) -> str:
    """A place name as a tag name: one line, no "/" or "|" (Biel/Bienne becomes Biel-Bienne), at most 120 characters."""
    return " ".join(str(name).replace("/", "-").replace("|", "-").split())[:120]


def plan(photo_ids: list = None) -> dict:
    """What would happen: how many photos get a place tag, and which tags are missing."""
    con = index.open_index()
    try:
        where = "place IS NOT NULL AND video = 0"
        if photo_ids:
            where += f" AND id IN ({','.join(str(int(i)) for i in photo_ids)})"
        rows = con.execute(f"SELECT id, place, region, country FROM photos WHERE {where}").fetchall()
        existing = {r[0] for r in con.execute("SELECT full FROM tags")}
        have = {}
        for pid, tid in con.execute("SELECT pt.photo_id, t.full FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE t.full LIKE ?", (ROOT + "/%",)):
            have.setdefault(pid, set()).add(tid)
    finally:
        con.close()
    wanted, todo, untag = set(), [], []
    # the paths this feature makes (Places/Country/Region/Town for some geotagged photo): only those can be stale
    shaped = {"/".join([ROOT] + [_part(x) for x in (c, r, t) if x and _part(x)]) for _, t, r, c in rows}
    for pid, place, region, country in rows:
        parts = [ROOT] + [_part(p) for p in (country, region, place) if p and _part(p)]
        full = "/".join(parts)
        for i in range(1, len(parts) + 1):  # the root "Places" tag included
            wanted.add("/".join(parts[:i]))
        if full not in have.get(pid, set()):
            todo.append((pid, full))
        # a place tag from an earlier position (the photo was moved since): it goes. Only tags of this feature's
        # shape (country, region, town); deeper ones someone added by hand (Places/France/Paris/Louvre) stay
        stale = sorted(t for t in have.get(pid, set()) if t != full and t in shaped and not full.startswith(t + "/") and not t.startswith(full + "/"))
        if stale:
            untag.append((pid, stale))
    missing = sorted(t for t in wanted if t not in existing)
    return {"photos": len(rows), "to_tag": len(todo), "to_untag": len(untag), "missing_tags": missing, "existing_tags": len(wanted) - len(missing),
            "_todo": todo, "_untag": untag}


def run(photo_ids: list = None):
    """Create the missing tags, then assign each photo its place tag (background)."""
    p = plan(photo_ids)
    progress.start(p["to_tag"], created=0)

    def work():
        try:
            if p["missing_tags"]:
                if dbsync.digikam_running():
                    raise SyncError("digiKam is running. Quit digiKam first; new place tags are created in its database.")
                # one transaction for every missing tag, parents before children
                made = tagmanager.add_tags(p["missing_tags"], check_running=True)
                progress["created"] = len([f for f in p["missing_tags"] if f in made])
                # the index must know the new tags before edits can resolve them
                index.build()
            con = index.open_index()
            try:
                by_full = {r[1]: r[0] for r in con.execute("SELECT id, full FROM tags")}
                paths = {r[0]: r[1] for r in con.execute(f"SELECT id, path FROM photos WHERE id IN ({','.join(str(i) for i, _ in p['_todo']) or 'NULL'})")}
            finally:
                con.close()
            groups = {}
            for pid, full in p["_todo"]:
                if full in by_full and pid in paths:
                    groups.setdefault(full, []).append({"id": pid, "path": paths[pid]})
            for full, photos in groups.items():
                edits.apply(photos, {"add_tags": [full]})
                progress.step(len(photos))
            stale = {}
            for pid, tags in p["_untag"]:
                stale.setdefault(tuple(tags), []).append(pid)
            if stale:
                con = index.open_index()
                try:
                    allp = {r[0]: r[1] for r in con.execute(f"SELECT id, path FROM photos WHERE id IN ({','.join(str(i) for v in stale.values() for i in v)})")}
                finally:
                    con.close()
                for tags, ids in stale.items():
                    photos = [{"id": i, "path": allp[i]} for i in ids if i in allp]
                    if photos:
                        edits.apply(photos, {"remove_tags": list(tags)})
        except Exception as e:
            progress.fail(e)
        finally:
            progress.finish()

    threading.Thread(target=work, name="placetags", daemon=True).start()
