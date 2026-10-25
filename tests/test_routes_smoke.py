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


def test_meta_photos_and_state_answer_304_while_unchanged(client, lib):
    """The apps reload these often: with the ETag sent back, an unchanged answer is a 304 with no body."""
    for url in ("/api/meta", "/api/photos", "/api/state"):
        r = client.get(url)
        assert r.status_code == 200 and r.headers.get("etag"), url
        again = client.get(url, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304 and again.content == b"", url
        assert client.get(url, headers={"If-None-Match": 'W/"other"'}).status_code == 200


def test_a_video_phones_cannot_play_gets_a_phone_friendly_copy(client, lib, monkeypatch, tmp_path):
    """/api/photo says whether Apple's players take a video as it is; /video/<id>.mp4 makes an H.264/AAC MP4 copy on
    the first request (503 + Retry-After meanwhile) and then serves it with Range requests."""
    import shutil, subprocess, time
    from app import thumbs, videostream
    if not thumbs.FFMPEG or not lib["videos"]:
        import pytest; pytest.skip("needs ffmpeg and a video")
    monkeypatch.setattr(videostream, "DIR", tmp_path / "v")
    vid = sorted(lib["videos"])[0]
    d = client.get(f"/api/photo/{vid}").json()
    assert isinstance(d["playable"], bool)
    assert "playable" not in client.get(f"/api/photo/{lib['ids'][0] if lib['ids'][0] not in lib['videos'] else lib['ids'][1]}").json()
    assert not videostream.playable("clip.avi", "mjpeg", "pcm_u8") and videostream.playable("IMG_1.MOV", "hevc", "aac")
    r = client.get(f"/video/{vid}.mp4")
    assert r.status_code in (200, 503), r.text
    until = time.time() + 120
    while r.status_code == 503 and time.time() < until:
        assert r.headers["retry-after"] and r.json()["status"] == "converting"
        time.sleep(1); r = client.get(f"/video/{vid}.mp4")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4", r.text[:200]
    part = client.get(f"/video/{vid}.mp4", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    f = tmp_path / "copy.mp4"; f.write_bytes(r.content)
    probe = shutil.which("ffprobe")
    if probe:
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(f)], capture_output=True, text=True).stdout.strip()
        assert out.strip(",") == "h264", out
    assert client.get(f"/video/{lib['ids'][0] if lib['ids'][0] not in lib['videos'] else lib['ids'][1]}.mp4").status_code == 404


def test_explore_in_one_request(client, lib):
    """/api/explore bundles what the Explore page fetches, each part as its own endpoint gives it."""
    md = "06-15"
    e = client.get(f"/api/explore?md={md}&n=5&pairs=2").json()
    assert set(e) == {"random", "thenAndNow", "events", "onThisDay", "peopleGroups"}
    assert len(e["random"]["id"]) <= 5 and "pairs" in e["thenAndNow"] and "groups" in e["peopleGroups"]
    assert e["onThisDay"]["count"] == client.get(f"/api/photos?md={md}").json()["count"]
    assert e["events"] == client.get("/api/events").json()["events"]
    assert client.get("/api/explore").json()["onThisDay"] is None
