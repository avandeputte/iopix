"""GPX track correlation, like digiKam's Geolocation Editor: give photos
without coordinates the position a GPS track recorded at the moment they
were taken.

Tracks are parsed from GPX files the user uploads (never written anywhere
but the app's data folder). Photo times are camera local time; track times
are UTC, so the user supplies the camera's UTC offset (and a clock
correction if the camera was off). A photo gets the position interpolated
between the two track points around its time, when they are no further
apart than `max_gap` seconds. The result is a preview; applying it goes
through the normal GPS edit path (sidecar, file, database).
"""
import bisect
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


def parse(data: bytes) -> list:
    """[(utc timestamp, lat, lon, ele or None)] sorted by time."""
    root = ET.fromstring(data)
    pts = []
    for el in root.iter():
        # GPX 1.0 and 1.1 differ only in namespace, so match on the local name; waypoints count too
        if not el.tag.endswith("}trkpt") and not el.tag.endswith("}wpt") and el.tag != "trkpt":
            continue
        try:
            lat, lon = float(el.get("lat")), float(el.get("lon"))
        except (TypeError, ValueError):
            continue
        t = ele = None
        for c in el:
            if c.tag.endswith("time") and c.text:
                try:
                    txt = c.text.strip().replace("Z", "+00:00")
                    dt = datetime.fromisoformat(txt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    t = dt.timestamp()
                except ValueError:
                    pass
            elif c.tag.endswith("ele") and c.text:
                try:
                    ele = float(c.text)
                except ValueError:
                    pass
        if t is not None:
            pts.append((t, lat, lon, ele))
    pts.sort()
    return pts


def _interp(a, b, t):
    if b[0] == a[0]:
        return a[1], a[2], a[3]
    f = (t - a[0]) / (b[0] - a[0])
    ele = (a[3] + (b[3] - a[3]) * f) if (a[3] is not None and b[3] is not None) else (a[3] if a[3] is not None else b[3])
    return a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, ele


def correlate(points: list, photos: list, utc_offset: int, clock_shift: int = 0, max_gap: int = 600) -> list:
    """photos: [{id, ts}] where ts is the camera's wall-clock time taken as UTC (as the
    index stores it). utc_offset: seconds to subtract to get real UTC (e.g. -14400 for a
    camera on US Eastern daylight time means utc = ts + 14400). clock_shift: seconds to
    add to the camera time first. Returns [{id, lat, lon, alt, gap, matched}]."""
    times = [p[0] for p in points]
    out = []
    for p in photos:
        t = p["ts"] + clock_shift - utc_offset
        i = bisect.bisect_left(times, t)
        best = None
        if 0 < i < len(points):
            a, b = points[i - 1], points[i]
            gap = max(t - a[0], b[0] - t)
            if b[0] - a[0] <= max_gap * 2 and gap <= max_gap:
                lat, lon, ele = _interp(a, b, t)
                best = (lat, lon, ele, gap)
        if best is None and points:  # nearest single point, if close enough
            cands = [points[k] for k in (i - 1, i) if 0 <= k < len(points)]
            near = min(cands, key=lambda q: abs(q[0] - t))
            if abs(near[0] - t) <= max_gap:
                best = (near[1], near[2], near[3], abs(near[0] - t))
        if best:
            out.append({"id": p["id"], "lat": round(best[0], 7), "lon": round(best[1], 7),
                        "alt": (round(best[2], 1) if best[2] is not None else None), "gap": int(best[3]), "matched": True})
        else:
            out.append({"id": p["id"], "matched": False})
    return out


def summary(points: list) -> dict:
    if not points:
        return {"points": 0}
    lat = [p[1] for p in points]
    lon = [p[2] for p in points]
    return {"points": len(points), "start": int(points[0][0]), "end": int(points[-1][0]),
            "bbox": [min(lat), min(lon), max(lat), max(lon)],
            "km": round(sum(math.hypot(points[k][1] - points[k - 1][1], (points[k][2] - points[k - 1][2]) * math.cos(math.radians(points[k][1]))) * 111.2
                           for k in range(1, len(points))), 1)}
