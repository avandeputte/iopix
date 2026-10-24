"""Offline reverse geocoding: GPS coordinates to "City, Region, Country".

Uses the GeoNames cities1000 gazetteer (every populated place over 1,000
people; CC BY 4.0, geonames.org) stored in data/geonames. Nothing is looked
up online. Run `python -m app.geo.places fetch` once to download the data.
"""
import io
import os
import math
import sys
import urllib.request
import zipfile

from app import config

DIR = config.DATA_DIR / "geonames"
BASE = "https://download.geonames.org/export/dump/"
FILES = ("cities1000.txt", "admin1CodesASCII.txt", "countryInfo.txt")
CELL = 0.5  # degrees
CELLS_X = int(round(360 / CELL))

_geo = None


def available() -> bool:
    return all((DIR / f).exists() for f in FILES)


def signature() -> str:
    return "geo2" if available() else "nogeo"


def fetch():
    """The three GeoNames files, written as .part first and renamed together at the end: a download that stops
    halfway leaves what was there (or nothing) rather than a truncated file every index build would trip on."""
    DIR.mkdir(parents=True, exist_ok=True)
    parts = []
    try:
        with urllib.request.urlopen(BASE + "cities1000.zip", timeout=120) as r:
            data = zipfile.ZipFile(io.BytesIO(r.read())).read("cities1000.txt")
        (DIR / "cities1000.txt.part").write_bytes(data); parts.append("cities1000.txt")
        for name in FILES[1:]:
            with urllib.request.urlopen(BASE + name, timeout=120) as r:
                (DIR / (name + ".part")).write_bytes(r.read())
            parts.append(name)
        for name in parts:
            os.replace(DIR / (name + ".part"), DIR / name)
    finally:
        for name in FILES:
            (DIR / (name + ".part")).unlink(missing_ok=True)


class Geocoder:
    def __init__(self):
        countries, admin1 = {}, {}
        for line in (DIR / "countryInfo.txt").read_text("utf-8").splitlines():
            if line and not line.startswith("#"):
                c = line.split("\t")
                countries[c[0]] = c[4]
        for line in (DIR / "admin1CodesASCII.txt").read_text("utf-8").splitlines():
            c = line.split("\t")
            if len(c) > 1:
                admin1[c[0]] = c[1]
        self.cells = {}
        with open(DIR / "cities1000.txt", encoding="utf-8") as f:
            for line in f:
                c = line.split("\t")
                if len(c) < 15:  # a truncated last line
                    continue
                if c[7] == "PPLX":  # a neighbourhood of a city; name the city instead
                    continue
                try:
                    lat, lon, cc = float(c[4]), float(c[5]), c[8]
                except ValueError:
                    continue
                place = (lat, lon, c[1], admin1.get(f"{cc}.{c[10]}"), countries.get(cc, cc), int(c[14] or 0))
                self.cells.setdefault((math.floor(lat / CELL), math.floor(lon / CELL)), []).append(place)
        self.cache = {}

    def lookup(self, lat: float, lon: float):
        """Nearest populated place as (city, region, country), or None if nothing within ~150 km."""
        key = (round(lat, 3), round(lon, 3))
        if key in self.cache:
            return self.cache[key]
        cy, cx, coslat = math.floor(lat / CELL), math.floor(lon / CELL), math.cos(math.radians(lat))
        best, best_d = None, 1e9
        for reach in (1, 3):
            span = range(-reach, reach + 1)
            for dy in span:
                for dx in span:
                    for p in self.cells.get((cy + dy, (cx + dx + CELLS_X // 2) % CELLS_X - CELLS_X // 2), ()):  # the cells wrap at the antimeridian
                        d = math.hypot(p[0] - lat, (((p[1] - lon + 180) % 360) - 180) * coslat) * 111.2
                        # favour real towns slightly over tiny hamlets that happen to be nearer
                        d_adj = d - min(3.0, math.log10(max(p[5], 1000)) - 3.0)
                        if d_adj < best_d:
                            best, best_d = p, d_adj
            if best and best_d < 40:
                break
        out = (best[2], best[3], best[4]) if best and best_d < 150 else None
        self.cache[key] = out
        return out


def geocoder():
    global _geo
    if _geo is None and available():
        _geo = Geocoder()
    return _geo


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
        print("GeoNames data saved to", DIR)
    else:
        g = geocoder()
        print(g.lookup(float(sys.argv[1]), float(sys.argv[2])) if g else "No data; run: python -m app.geo.places fetch")
