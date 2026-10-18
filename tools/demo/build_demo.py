"""Build a demo library for documentation screenshots: freely licensed photos, coherent metadata, an empty
digiKam database of its own. Nothing here touches a real library.

    .venv/bin/python tools/demo/build_demo.py [--out ~/iopix-demo] [--unsplash-key KEY] [--library-picks FILE]

Travel photos come from Wikimedia Commons (Quality images near each place, CC BY / BY-SA / CC0 / public domain),
people photos from a CC0 family series on StockSnap (found through Openverse) and from Unsplash with a free access
key (UNSPLASH_KEY or ~/.unsplash-key). A list of people-free photos from the owner's own library can be added; they
are copied with their captions and keywords stripped. Every file gets dates, location, keywords, a title and a rating
written into it (exiftool); CREDITS.md lists every author and license. Downloads are cached in ~/iopix-demo-sources.
Then run the viewer against it (tools/demo/serve_demo.sh), give the recurring people their invented names
(tools/demo/people.py apply) and take the screenshots (tools/demo/screenshots.py).
"""
import argparse
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "iopix-demo-builder/1.0 (https://github.com/avandeputte/iopix)"
EXIFTOOL = shutil.which("exiftool") or "/Applications/digiKam.org/digikam.app/Contents/bin/exiftool"
LICENSES_OK = ("CC BY", "CC0", "Public domain", "PD")
TRIPS = [  # name, folder date, places (lat, lon, label), tags
    ("Lisbon", "2024-04-12", [(38.7139, -9.1394, "Baixa"), (38.6916, -9.2160, "Belém"), (38.7139, -9.1334, "Alfama"), (38.7979, -9.3906, "Sintra")], ["Travel/Portugal/Lisbon", "Architecture"]),
    ("Kyoto", "2023-11-18", [(34.9671, 135.7727, "Fushimi Inari"), (35.0394, 135.7292, "Kinkaku-ji"), (35.0116, 135.7681, "Gion"), (35.0170, 135.6713, "Arashiyama")], ["Travel/Japan/Kyoto", "Temples"]),
    ("Yosemite", "2023-07-04", [(37.7459, -119.5332, "Yosemite Valley"), (37.7275, -119.6457, "Tunnel View"), (37.7308, -119.5728, "Half Dome"), (37.8651, -119.5383, "Tuolumne")], ["Travel/USA/Yosemite", "Nature", "Mountains"]),
    ("Iceland", "2022-08-09", [(64.1466, -21.9426, "Reykjavík"), (64.2559, -21.1300, "Þingvellir"), (63.5321, -19.5114, "Skógafoss"), (64.0485, -16.1800, "Jökulsárlón")], ["Travel/Iceland", "Nature", "Waterfalls"]),
    ("Rome", "2023-05-06", [(41.8902, 12.4922, "Colosseum"), (41.9022, 12.4539, "Vatican"), (41.8986, 12.4769, "Pantheon"), (41.9009, 12.4833, "Trevi Fountain")], ["Travel/Italy/Rome", "Architecture", "History"]),
    ("Barcelona", "2022-10-14", [(41.4036, 2.1744, "Sagrada Família"), (41.4145, 2.1527, "Park Güell"), (41.3830, 2.1760, "Gothic Quarter"), (41.3758, 2.1490, "Montjuïc")], ["Travel/Spain/Barcelona", "Architecture"]),
    ("New York", "2023-09-22", [(40.7580, -73.9855, "Times Square"), (40.7829, -73.9654, "Central Park"), (40.7061, -73.9969, "Brooklyn Bridge"), (40.6892, -74.0445, "Statue of Liberty")], ["Travel/USA/New York", "City"]),
    ("San Francisco", "2022-06-11", [(37.8199, -122.4783, "Golden Gate Bridge"), (37.8080, -122.4177, "Fisherman's Wharf"), (37.8027, -122.4058, "Telegraph Hill"), (37.7694, -122.4862, "Golden Gate Park")], ["Travel/USA/San Francisco", "City"]),
    ("Scotland", "2023-08-19", [(55.9486, -3.1999, "Edinburgh"), (57.3229, -4.4244, "Loch Ness"), (57.2730, -5.5170, "Eilean Donan"), (56.6812, -5.1015, "Glencoe")], ["Travel/Scotland", "Nature", "Castles"]),
    ("Swiss Alps", "2024-07-27", [(45.9763, 7.6586, "Zermatt"), (46.5577, 7.9068, "Lauterbrunnen"), (46.6863, 7.8632, "Interlaken"), (46.0207, 7.7491, "Gornergrat")], ["Travel/Switzerland", "Mountains", "Nature"]),
    ("Grand Canyon", "2022-04-16", [(36.0544, -112.1401, "South Rim"), (36.0579, -112.1436, "Mather Point"), (36.1987, -112.0525, "North Rim"), (36.8791, -111.5104, "Horseshoe Bend")], ["Travel/USA/Arizona", "Nature", "Desert"]),
    ("Amsterdam", "2024-04-26", [(52.3676, 4.9041, "Canals"), (52.3600, 4.8852, "Rijksmuseum"), (52.3731, 4.8922, "Dam Square"), (52.3080, 4.7620, "Keukenhof road")], ["Travel/Netherlands/Amsterdam", "City", "Canals"]),
    ("Banff", "2023-09-02", [(51.4254, -116.1773, "Lake Louise"), (51.3217, -116.1860, "Moraine Lake"), (51.1784, -115.5708, "Banff"), (52.8734, -117.9543, "Jasper")], ["Travel/Canada/Banff", "Mountains", "Lakes"]),
    ("Cinque Terre", "2024-09-21", [(44.1069, 9.7290, "Manarola"), (44.1279, 9.6830, "Vernazza"), (44.1461, 9.6439, "Monterosso"), (44.0996, 9.7377, "Riomaggiore")], ["Travel/Italy/Cinque Terre", "Sea", "Villages"]),
    ("Santorini", "2023-06-17", [(36.4618, 25.3753, "Oia"), (36.4166, 25.4316, "Fira"), (36.3932, 25.4615, "Akrotiri road"), (36.3486, 25.4058, "Red Beach")], ["Travel/Greece/Santorini", "Sea", "Islands"]),
    ("Tokyo", "2023-11-11", [(35.6595, 139.7005, "Shibuya"), (35.7148, 139.7967, "Asakusa"), (35.6764, 139.6993, "Meiji Shrine"), (35.6586, 139.7454, "Tokyo Tower")], ["Travel/Japan/Tokyo", "City", "Temples"]),
    ("Paris", "2024-12-20", [(48.8584, 2.2945, "Eiffel Tower"), (48.8606, 2.3376, "Louvre"), (48.8530, 2.3499, "Notre-Dame"), (48.8867, 2.3431, "Montmartre")], ["Travel/France/Paris", "Architecture", "City"]),
]


SOURCES = Path(os.environ.get("DEMO_SOURCES", Path.home() / "iopix-demo-sources"))  # kept: downloads and the picks, to rebuild at will
CACHE = SOURCES / "downloads"  # every downloaded file, so a rebuild downloads nothing again (Wikimedia asks bots to go easy)


def get(url, binary=False, headers=None):
    import hashlib
    import urllib.error
    if binary:
        hit = CACHE / hashlib.sha1(url.encode()).hexdigest()
        if hit.exists():
            return hit.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(6):
        try:
            time.sleep(0.25 if binary else 0.5)  # a polite pace per worker (a few workers per source)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if binary:
                CACHE.mkdir(parents=True, exist_ok=True)
                hit.write_bytes(data)
                return data
            return json.loads(data)
        except urllib.error.HTTPError as e:
            if attempt == 5 or e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(int(e.headers.get("Retry-After") or 0) or 15 * (attempt + 1))
        except OSError:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def commons_near(lat, lon, n=12):
    """Freely licensed Quality images within 5 km (Commons search: nearcoord + the Quality images category)."""
    q = urllib.parse.urlencode({"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 50,
                                "gsrsearch": f"nearcoord:5km,{lat},{lon} incategory:Quality_images filetype:bitmap",
                                "prop": "imageinfo|coordinates", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 2400})
    d = get("https://commons.wikimedia.org/w/api.php?" + q)
    out = []
    for p in d.get("query", {}).get("pages", {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        m = ii.get("extmetadata", {})
        lic = strip_html(m.get("LicenseShortName", {}).get("value", ""))
        cats = m.get("Categories", {}).get("value", "").lower()
        if ii.get("mime") != "image/jpeg" or ii.get("width", 0) < 1600 or ii.get("width", 0) < ii.get("height", 1) * 0.6 \
                or not lic.startswith(LICENSES_OK) or "NC" in lic or "ND" in lic or any(w in cats for w in ("people", "portrait", "nude", "insect", "spider")):
            continue
        c = (p.get("coordinates") or [{}])[0]
        out.append({"title": p["title"][5:].rsplit(".", 1)[0], "url": ii.get("thumburl") or ii["url"], "page": ii.get("descriptionurl"), "license": lic,
                    "artist": strip_html(m.get("Artist", {}).get("value", ""))[:80] or "Unknown", "desc": strip_html(m.get("ImageDescription", {}).get("value", ""))[:200],
                    "lat": c.get("lat", lat), "lon": c.get("lon", lon), "w": ii["width"], "h": ii["height"]})
    random.shuffle(out)
    out = [o for o in out if o["url"] not in SEEN and o["w"] <= o["h"] * 2.2]  # no photo twice, no extreme panoramas
    SEEN.update(o["url"] for o in out[:n])
    return out[:n]


SEEN = set()


def unescape(name):
    """Some creator names come URL-escaped, JavaScript style included ("Alex%20Bl%u0103jan")."""
    return urllib.parse.unquote(re.sub(r"%u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name))


def openverse_series(creator, n, source="stocksnap"):
    """Photos of one photographer's series on Openverse (CC0 / CC BY): the same people across many photos, which is
    what face grouping and the People pages need."""
    out, page = [], 1
    while len(out) < n and page <= 6:
        q = urllib.parse.urlencode({"q": "family", "source": source, "creator": creator, "license": "cc0,by,by-sa", "page_size": 20, "page": page})
        d = get("https://api.openverse.org/v1/images/?" + q)
        for r in d.get("results", []):
            if r.get("url"):
                out.append({"title": r.get("title") or "Family", "url": r["url"], "page": r.get("foreign_landing_url") or r["url"],
                            "license": f"{r['license'].upper()} {r.get('license_version') or ''}".strip().replace("CC0 1.0", "CC0"),
                            "artist": unescape(r.get("creator") or "Unknown"), "desc": ""})
        if not d.get("results"):
            break
        page += 1
    return out[:n]


def unsplash(key, query, n):
    """Unsplash search (free developer key). Their API guidelines ask that a download be reported: done per photo."""
    d = get(f"https://api.unsplash.com/search/photos?{urllib.parse.urlencode({'query': query, 'per_page': n, 'orientation': 'landscape', 'content_filter': 'high'})}",
            headers={"Authorization": f"Client-ID {key}"})
    out = []
    for r in d.get("results", []):
        out.append({"title": (r.get("alt_description") or query).strip().capitalize()[:80], "url": r["urls"]["raw"] + "&w=2200&q=85&fm=jpg",
                    "page": r["links"]["html"], "license": "Unsplash License", "artist": r["user"]["name"], "desc": (r.get("description") or "")[:200],
                    "track": r["links"]["download_location"]})
    return out


def from_library(picks, photos, credits):
    """Photos chosen from the owner's own library (read only, people-free): copied with their real dates, places and
    camera; captions, keywords and any embedded regions removed, demo keywords added."""
    n = 0
    for p in picks:
        src = Path(p["src"])
        when = None
        r = subprocess.run([EXIFTOOL, "-s3", "-d", "%Y-%m-%d", "-DateTimeOriginal", str(src)], capture_output=True, text=True)
        when = (r.stdout.strip() or "2025-01-01")[:10]
        folder = photos / when[:4] / f"{p['folder_date']} {p['name']}"
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
        subprocess.run([EXIFTOOL, "-q", "-m", "-overwrite_original", "-XMP:all=", "-IPTC:all=", "-ImageDescription=", "-UserComment=",
                        *[f"-XMP-digiKam:TagsList+={t}" for t in p["tags"]], *[f"-XMP-dc:Subject+={t.split('/')[-1]}" for t in p["tags"]],
                        f"-XMP-xmp:Rating={random.choice([0, 0, 3, 4, 5])}", str(dst)], check=True, capture_output=True)
        credits.append(f"| {dst.relative_to(photos)} | the project owner's own photo | the project owner | used with permission |")
        n += 1
    return n


def write_meta(path, when, title, desc, tags, rating, lat=None, lon=None, label=None, artist=None, credit=None):
    args = [EXIFTOOL, "-q", "-m", "-overwrite_original", "-charset", "filename=utf8",
            f"-DateTimeOriginal={when:%Y:%m:%d %H:%M:%S}", f"-CreateDate={when:%Y:%m:%d %H:%M:%S}",
            f"-XMP-dc:Title={title}", f"-XMP-xmp:Rating={rating}", "-Make=", "-Model="]
    if desc:
        args.append(f"-XMP-dc:Description={desc}")
    if artist:
        args += [f"-XMP-dc:Creator={artist}", f"-XMP-dc:Rights={credit}"]
    for t in tags:
        args += [f"-XMP-digiKam:TagsList+={t}", f"-XMP-lr:HierarchicalSubject+={t.replace('/', '|')}", f"-XMP-dc:Subject+={t.split('/')[-1]}"]
    if lat is not None:
        args += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}", f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    if label:
        args.append(f"-XMP-iptcCore:Location={label}")
    subprocess.run(args + [str(path)], check=True, capture_output=True)


def empty_digikam_db(target: Path, template: Path, root: Path):
    """digiKam's schema (tables, indexes, triggers) and settings, its internal tags and People root, one album root:
    no rows from the template's library."""
    src = sqlite3.connect(f"file:{template}?mode=ro", uri=True)
    target.unlink(missing_ok=True)
    dst = sqlite3.connect(target)
    for typ in ("table", "index", "trigger"):
        for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type = ? AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'", (typ,)):
            dst.execute(sql)
    for k, v in src.execute("SELECT keyword, value FROM Settings"):
        if k in ("databaseUUID", "Scanned", "DeleteRemovedTime", "RemovedItemsTime", "DeleteRemovedCompleteScanCount"):
            continue
        dst.execute("INSERT INTO Settings VALUES (?, ?)", (k, v))
    keep = {r[0] for r in src.execute("SELECT id FROM Tags WHERE id = 1 OR pid = 1 OR name = 'People' AND pid = 0")}
    keep |= {r[0] for r in src.execute("SELECT tagid FROM TagProperties WHERE property IN ('unknownPerson', 'ignoredPerson', 'unconfirmedPerson')")}
    for row in src.execute(f"SELECT id, pid, name FROM Tags WHERE id IN ({','.join(map(str, keep))}) ORDER BY id"):
        dst.execute("INSERT INTO Tags (id, pid, name) VALUES (?,?,?)", row)
    for row in src.execute(f"SELECT tagid, property, value FROM TagProperties WHERE tagid IN ({','.join(map(str, keep))}) AND property != 'faceEngineId'"):
        dst.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?,?,?)", row)
    dst.execute("INSERT INTO AlbumRoots (id, label, status, type, identifier, specificPath) VALUES (1, 'demo', 0, 1, ?, ?)", (f"volumeid:?path={root}", str(root)))
    dst.execute("INSERT INTO Albums (albumRoot, relativePath, date) VALUES (1, '/', ?)", (dt.date.today().isoformat(),))
    dst.commit()
    src.close()
    dst.execute("VACUUM")
    dst.close()


def plan_commons(per_place):
    jobs = []
    for name, start, places, tags in TRIPS:
        day0 = dt.datetime.fromisoformat(start + "T09:00:00")
        folder = f"{day0:%Y}/{day0:%Y-%m-%d} {name}"
        for k, (lat, lon, label) in enumerate(places):
            got = commons_near(lat, lon, per_place)
            for j, p in enumerate(got):
                when = day0 + dt.timedelta(days=k, hours=j * 1.3, minutes=random.randint(0, 50))
                jobs.append({"source": "commons", "folder": folder, "when": when, "url": p["url"], "title": label if j == 0 else p["title"][:70], "desc": p["desc"],
                             "tags": tags + [f"Where/{name}/{label}"], "rating": random.choice([0, 0, 0, 3, 4, 5]), "lat": p["lat"], "lon": p["lon"], "label": label,
                             "artist": p["artist"], "credit": f"{p['artist']}, {p['license']}, via Wikimedia Commons", "page": p["page"], "license": p["license"], "ptitle": p["title"]})
            print(f"  planned {name} / {label}: {len(got)}", flush=True)
    return jobs


def plan_unsplash(key):
    jobs = []
    for start, name, query, tags in [("2024-05-18", "Friends weekend", "friends laughing outdoors", ["Friends"]), ("2024-11-28", "Thanksgiving", "family dinner table", ["Family", "Holidays"]),
                                     ("2024-02-10", "Ski trip", "friends skiing", ["Friends", "Winter"]), ("2024-04-20", "Garden party", "garden party friends", ["Friends", "Celebrations"])]:
        day0 = dt.datetime.fromisoformat(start + "T12:00:00")
        for j, p in enumerate(unsplash(key, query, 14)):
            jobs.append({"source": "unsplash", "folder": f"{day0:%Y}/{day0:%Y-%m-%d} {name}", "when": day0 + dt.timedelta(minutes=j * 19), "url": p["url"], "track": p["track"],
                         "title": p["title"], "desc": p["desc"], "tags": tags, "rating": random.choice([0, 0, 3, 4, 5]), "artist": p["artist"],
                         "credit": f"Photo by {p['artist']} on Unsplash", "page": p["page"], "license": "Unsplash License", "ptitle": p["title"]})
    return jobs


def plan_family():
    events = [("2024-06-15", "Family picnic", ["Family", "Summer"]), ("2024-09-07", "Birthday", ["Family", "Celebrations"]), ("2023-12-25", "Christmas", ["Family", "Holidays"]),
              ("2024-03-30", "Easter weekend", ["Family", "Holidays"]), ("2024-08-03", "Lake house", ["Family", "Summer"])]
    jobs = []
    for k, p in enumerate(openverse_series("Family Moments", 150)):
        start, name, tags = events[k % len(events)]
        day0 = dt.datetime.fromisoformat(start + "T11:00:00")
        jobs.append({"source": "stocksnap", "folder": f"{day0:%Y}/{day0:%Y-%m-%d} {name}", "when": day0 + dt.timedelta(minutes=(k // len(events)) * 23), "url": p["url"],
                     "title": p["title"], "desc": "", "tags": tags, "rating": random.choice([0, 0, 3, 4, 5]), "artist": p["artist"],
                     "credit": f"{p['artist']}, {p['license']}, via StockSnap", "page": p["page"], "license": p["license"], "ptitle": p["title"]})
    return jobs


def fetch_one(job, photos, key=None):
    """Download (or take from the cache), write the file and its metadata. Returns the credit row, or None."""
    f = photos / job["folder"] / f"IMG_{job['when']:%Y%m%d_%H%M%S}.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = get(job["url"], binary=True)
        if data[:2] != b"\xff\xd8":
            raise ValueError("not a JPEG")
        f.write_bytes(data)
        if job.get("track") and key:
            try:
                get(job["track"], headers={"Authorization": f"Client-ID {key}"})
            except Exception:
                pass
        write_meta(f, job["when"], job["title"], job["desc"], job["tags"], job["rating"], job.get("lat"), job.get("lon"), job.get("label"), job["artist"], job["credit"])
    except Exception as e:
        f.unlink(missing_ok=True)
        print(f"  skipped {job['url'][:80]} ({type(e).__name__}: {str(e)[:80]})", flush=True)
        return None
    return f"| {f.relative_to(photos)} | [{job['ptitle'][:60]}]({job['page']}) | {job['artist']} | {job['license']} |"


def main():
    from concurrent.futures import ThreadPoolExecutor
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "iopix-demo"))
    ap.add_argument("--template", default=str(Path.home() / "Pictures/digikamDB/digikam4.db"), help="a digiKam database to take the empty schema from")
    ap.add_argument("--unsplash-key", default=os.environ.get("UNSPLASH_KEY") or ((Path.home() / ".unsplash-key").read_text().strip() if (Path.home() / ".unsplash-key").exists() else None))
    ap.add_argument("--library-picks", default=str(SOURCES / "library-picks.json"), help="JSON list of people-free photos from the owner's library to include (copied, never changed)")
    ap.add_argument("--per-place", type=int, default=9)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)
    out = Path(a.out).expanduser()
    photos, db = out / "photos", out / "db"
    photos.mkdir(parents=True, exist_ok=True)
    db.mkdir(exist_ok=True)
    t0 = time.time()
    # 1. what to fetch: each source planned at the same time (their APIs are separate)
    with ThreadPoolExecutor(3) as pool:
        fc = pool.submit(plan_commons, a.per_place)
        fu = pool.submit(plan_unsplash, a.unsplash_key) if a.unsplash_key else None
        ff = pool.submit(plan_family)
        plans = {"commons": fc.result(), "unsplash": fu.result() if fu else [], "stocksnap": ff.result()}
    print("planned:", {k: len(v) for k, v in plans.items()}, f"in {time.time() - t0:.0f}s", flush=True)
    # 2. download and tag: a few workers per source, all sources at once (each source stays at a polite rate)
    workers = {"commons": 3, "unsplash": 4, "stocksnap": 4}
    credits = []
    pools = {k: ThreadPoolExecutor(workers[k]) for k in plans}
    futures = [pools[k].submit(fetch_one, job, photos, a.unsplash_key) for k, jobs in plans.items() for job in jobs]
    libn = 0
    if a.library_picks and Path(a.library_picks).exists():  # the owner's own people-free photos, copied locally meanwhile
        libn = from_library(json.loads(Path(a.library_picks).read_text()), photos, credits)
    done = 0
    for fut in futures:
        row = fut.result()
        if row:
            credits.append(row)
        done += 1
        if done % 50 == 0:
            print(f"  {done} / {len(futures)} downloaded", flush=True)
    for p in pools.values():
        p.shutdown()
    (out / "CREDITS.md").write_text("# Demo library credits\n\nPhotos used in iopix's documentation screenshots, under the licenses shown: Wikimedia Commons "
                                    "(Creative Commons or public domain), StockSnap (CC0), Unsplash (Unsplash License), and the project owner's own photos used with permission.\n\n"
                                    "| File | Source | Author | License |\n|---|---|---|---|\n" + "\n".join(sorted(credits)) + "\n")
    empty_digikam_db(db / "digikam4.db", Path(a.template).expanduser(), photos)
    n = sum(1 for _ in photos.rglob("*") if _.is_file() and not _.name.startswith("."))
    print(f"{n} photos ({libn} from the owner's library) in {photos} after {time.time() - t0:.0f}s; empty digiKam database in {db}; credits in {out / 'CREDITS.md'}")


if __name__ == "__main__":
    sys.exit(main())
