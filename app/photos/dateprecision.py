"""Approximate dates: a photo whose day (or month, or year) nobody knows, like an old print scanned decades later.

digiKam has one full date per photo (ImageInformation.creationDate) and reads only full dates from metadata, so the
photo keeps a full placeholder date where a full date is required (digiKam's database, EXIF, IPTC and the XMP dates
digiKam reads): the 1st of January of the year, or the 1st of the month. How much of it is true is said twice:

- a keyword tag under "Date approximate" (Month and year / Year only / Unknown), which digiKam keeps in its database
  and sidecars like any tag, so the mark survives every path (database, sidecar, file, iopix's own copy) and can be
  searched in digiKam too;
- XMP photoshop:DateCreated (IPTC Core's Date Created, which may be cut short) holds only the known part, "1992" or
  "1992-05", and is left out when nothing is known. digiKam reads that field only as a full date, so a short one is
  passed over there and its placeholder wins; other programs that read IPTC Core see the honest date.

An unknown date keeps the photo's date as it is (where digiKam sorts it), only marked as unknown.
"""
import re

ROOT = "Date approximate"
TAGS = {"month": ROOT + "/Month and year", "year": ROOT + "/Year only", "unknown": ROOT + "/Unknown"}
LEVELS = {"month": 1, "year": 2, "unknown": 3}  # the index's photos.dprec; NULL = an exact date
NAMES = {v: k for k, v in LEVELS.items()}
CHOICES = ("exact", *TAGS)


def of_paths(paths) -> str:
    """The precision a photo's tag paths say: "month", "year", "unknown", or None (exact). The vaguest one wins when
    a photo somehow carries two."""
    found = [k for k, t in TAGS.items() if t in set(paths or ())]
    return max(found, key=LEVELS.get) if found else None


def placeholder(precision: str, taken: str):
    """The full date written for a photo of this precision whose date (or the known part of it) is `taken`
    ("YYYY-MM-DDTHH:MM:SS", or just "YYYY" / "YYYY-MM"). None when there is no date to go on."""
    if not taken or not re.match(r"^\d{4}", taken):
        return None
    y, m = taken[:4], (taken[5:7] if re.match(r"^\d{4}-\d{2}", taken) else "01")
    if precision == "year":
        return f"{y}-01-01T00:00:00"
    if precision == "month":
        return f"{y}-{m}-01T00:00:00"
    return taken[:19] if len(taken) >= 19 else None  # exact or unknown: the date stays as it is


def xmp_date(precision: str, taken: str):
    """photoshop:DateCreated for this precision: the known part only, "" (removed) when nothing is known, None when
    there is nothing to write (an exact date with no date given)."""
    if precision == "unknown":
        return ""
    if not taken:
        return None
    return {"year": taken[:4], "month": taken[:7]}.get(precision, taken[:19])


def changes_for(precision: str) -> dict:
    """The tag edit that marks a photo with this precision: its tag on, the other ones off."""
    add = [TAGS[precision]] if precision in TAGS else []
    return {"add_tags": add, "remove_tags": [t for t in TAGS.values() if t not in add]}


def prepare(photos: list, precision: str, known: str, ch: dict, per_photo: dict, now: dict):
    """Fill an edit (ch, and per_photo {id: {taken}}) that sets this precision on these photos [{id, path}]: its tag
    on and the others off, the precision itself (for the XMP date), and each photo's full placeholder date, from the
    part `known` ("YYYY", "YYYY-MM", a full date, or "") with the rest from the photo's own date in `now` {id: taken}.
    ValueError when a year or month is asked for a photo with no date and none was given."""
    tags = changes_for(precision)
    ch["add_tags"] = list(dict.fromkeys(ch.get("add_tags", []) + tags["add_tags"]))
    ch["remove_tags"] = [t for t in dict.fromkeys(ch.get("remove_tags", []) + tags["remove_tags"]) if t not in ch["add_tags"]]
    ch["date_precision"] = precision
    ch.pop("taken", None)
    for p in photos:
        own = now.get(p["id"]) or ""
        given = known + own[len(known):] if known else own  # the known part given, the rest from the photo's date
        t = placeholder(precision, given if len(given) >= 19 else known)
        if t:
            per_photo[p["id"]] = {"taken": t}
        elif precision in ("year", "month"):
            raise ValueError("Give the year: the photo has no date to take it from")


def ensure_tags(precision: str):
    """Create the precision's tag (and the "Date approximate" parent) in digiKam's database when it is not there yet.
    The tag manager does the write: digiKam closed, as for any new tag."""
    if precision not in TAGS:
        return
    from app.digikam import dkdb
    from app.photos import tagmanager

    def find(con, full):
        tree = dkdb.tag_tree(con)
        return next((tid for tid in tree if tid and dkdb.tag_path(tree, tid) == full), 0)
    src = dkdb.connect(write=False)
    try:
        root, leaf = find(src, ROOT), find(src, TAGS[precision])
    finally:
        src.close()
    if leaf:
        return
    if not root:
        root = tagmanager.apply({"op": "add", "parent": 0, "name": ROOT}, backup=False)["id"]
    tagmanager.apply({"op": "add", "parent": root, "name": TAGS[precision].split("/", 1)[1]}, backup=False)
    from app.data import index
    index.build()  # the edit that follows resolves the new tag by its path
