"""Every GET route answers without a server error: a NameError or a broken import in a route that no other test
calls would otherwise only show up when someone opens that page."""
import re

import pytest
from fastapi.routing import APIRoute

SKIP = ("/api/backup/", "/api/log/download", "/api/zip", "/orig/", "/mcp", "/api/update")  # downloads and network calls


def _routes():
    from app.main import app
    out = []

    def walk(routes):
        for r in routes:
            if hasattr(r, "original_router"):  # FastAPI's wrapper around an included router
                walk(r.original_router.routes)
            elif isinstance(r, APIRoute) and "GET" in r.methods and not any(r.path.startswith(s) for s in SKIP):
                out.append(r.path)
    walk(app.routes)
    return sorted(set(out))


def _fill(path, lib):
    pid = lib["ids"][0]
    vals = {"photo_id": pid, "tag_id": 4, "album_id": 1, "token": "notatoken12345", "entry_id": 1, "which": "app", "search_id": 1,
            "user_id": 1, "x": 0, "y": 0, "w": 10, "h": 10}
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: str(vals.get(m.group(1), 1)), path)


def test_every_get_route_answers_without_a_server_error(client, lib):
    failures = []
    params = {"/api/trail": "?day=2025-08-14", "/api/elevation": "?lat=40&lon=-80", "/api/location/suggest": f"?id={lib['ids'][0]}",
              "/api/faces/nearby": f"?photo={lib['ids'][0]}", "/api/quality/items": "?check=nolocation", "/api/trash/thumb": "?info=/nonexistent",
              "/api/geocode": "", "/api/faces/queue": "", "/api/import/layouts": ""}
    paths = _routes()
    assert len(paths) > 50, paths
    for p in paths:
        url = _fill(p, lib) + params.get(p, "")
        try:
            r = client.get(url)
        except Exception as e:
            failures.append(f"{url}: raised {type(e).__name__}: {e}")
            continue
        if r.status_code >= 500 and not (p == "/api/geocode" or "network" in r.text.lower()):
            failures.append(f"{url}: {r.status_code} {r.text[:200]}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("page", ["explore", "photos", "people", "places", "folders", "albums", "tags", "map", "duplicates", "detected",
                                  "bursts", "events", "history", "analysis", "settings", "maintenance", "consistency", "quality", "trash",
                                  "import", "faces", "favorites"])
def test_every_page_is_routed(page):
    from pathlib import Path
    js = "".join(p.read_text() for p in (Path(__file__).resolve().parent.parent / "static" / "js").glob("*.js"))
    assert re.search(rf"\b{page}\s*:", js) or f"'{page}'" in js, page
