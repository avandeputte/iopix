"""Videos (app/ai/videos.py): the cover frame, chapters, transcript cleaning, and what the API serves."""
import json

import pytest

from app.ai import videos
from app.data import vdb

pytest.importorskip("numpy")


def test_clean_text_drops_loops_on_noise():
    assert videos.clean_text("What does your mouth taste like? Um") == "What does your mouth taste like? Um"
    assert videos.clean_text("Oh Oh Oh Oh Oh Oh Oh Oh Oh Oh Oh") == ""          # a loop and nothing else
    assert videos.clean_text("Happy birthday to you you you you you you dear Sam") == "Happy birthday to you dear Sam"
    assert videos.clean_text("") == "" and videos.clean_text("Hi") == ""


def test_chapters_fall_back_to_even_moments(monkeypatch):
    monkeypatch.setattr(videos, "scene_cuts", lambda src, d: [])
    assert videos.chapters_for("x", 8) == []
    ch = videos.chapters_for("x", 100)
    assert len(ch) == 6 and ch[0] < ch[-1] < 100
    monkeypatch.setattr(videos, "scene_cuts", lambda src, d: [4.0, 20.5, 33.0])
    assert videos.chapters_for("x", 100) == [4.0, 20.5, 33.0]


def test_cover_picks_the_sharp_lit_frame(monkeypatch):
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(0)
    frames = {}

    def fake_frame(src, t, height=480):
        if t < 10:   # dark and flat
            return Image.new("RGB", (320, 240), (8, 8, 8))
        if t < 40:   # bright, textured
            return Image.fromarray(rng.integers(60, 200, (240, 320, 3), dtype=np.uint8))
        return Image.new("RGB", (320, 240), (250, 250, 250))  # blown out
    monkeypatch.setattr(videos, "_frame", fake_frame)
    t, score = videos.pick_cover("x", 60)
    assert 10 <= t < 40 and score > 0
    assert videos.pick_cover("x", 1.5)[0] <= 1.0


def test_info_and_transcripts(lib):
    with vdb.writer() as con:
        con.execute("DELETE FROM video_info")
        con.execute("INSERT INTO video_info VALUES (?,?,?,?,?,?,?,?,?)", (5, "s", 12.5, json.dumps([3.0, 20.0]), "happy birthday", json.dumps([{"t": 1.0, "end": 3.0, "text": "happy birthday"}]), "en", "parakeet", 0))
    try:
        i = videos.info(5)
        assert i["cover_t"] == 12.5 and i["chapters"] == [3.0, 20.0] and i["segments"][0]["t"] == 1.0
        assert videos.cover_time(5) == 12.5 and videos.cover_time(6) is None
        assert videos.transcripts() == {5: "happy birthday"} and videos.signature() == "v1"
        assert videos.info(6) == {}
    finally:
        with vdb.writer() as con:
            con.execute("DELETE FROM video_info")


def test_video_frame_endpoint(client, lib):
    from app.data import index
    con = index.open_index()
    try:
        row = con.execute("SELECT id FROM photos WHERE video = 1 LIMIT 1").fetchone()
        pid = con.execute("SELECT id FROM photos WHERE video = 0 LIMIT 1").fetchone()[0]
    finally:
        con.close()
    assert client.get(f"/video/{pid}/frame?t=1").status_code == 404  # not a video
    if row:
        r = client.get(f"/video/{row[0]}/frame?t=0.5&h=120")
        assert r.status_code in (200, 503)
        d = client.get(f"/api/photo/{row[0]}").json()
        assert "videoInfo" in d
