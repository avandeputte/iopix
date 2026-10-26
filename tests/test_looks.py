"""Photo quality (looks.py): the aesthetic head is read without running pickled code, focus follows the
sharpest part of the frame, the best frame of a burst avoids closed eyes, likely rejects carry their
reasons, and a model installed later fills in only its own part."""
import os
import pickle
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.ai import analysis, looks, models, moments
from app.data import index, query

MODELS = Path("/private/tmp/claude-501/-Users-alex-Pictures/cd6ab508-dd1f-4df1-999c-d6fe0a83fe5c/scratchpad/models")
np = pytest.importorskip("numpy")


class _Evil:
    def __reduce__(self):
        return (os.system, ("echo pwned",))


def test_the_head_reader_refuses_anything_but_tensors(tmp_path):
    p = tmp_path / "bad.pth"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("archive/data.pkl", pickle.dumps({"weight": _Evil()}, protocol=2))
    with pytest.raises(pickle.UnpicklingError):
        looks.read_state_dict(p)


def test_the_head_reader_reads_the_laion_head():
    head = MODELS / "laion_aesthetic_l14.pth"
    if not head.exists():
        pytest.skip("aesthetic head not available here")
    sd = looks.read_state_dict(head)
    assert sd["layers.0.weight"].shape == (1024, 768) and sd["layers.7.weight"].shape == (1, 16)
    assert all(v.dtype == np.float32 and np.isfinite(v).all() for v in sd.values())


def test_focus_follows_the_sharpest_part_of_the_frame():
    from PIL import Image, ImageFilter
    rng = np.random.default_rng(0)
    noise = Image.fromarray((rng.random((768, 1024)) * 255).astype("uint8"))
    soft = noise.filter(ImageFilter.GaussianBlur(6))
    subject = soft.copy()
    subject.paste(noise.crop((400, 300, 600, 500)), (400, 300))  # a crisp subject on a soft background
    assert looks.focus(subject) > 5 * looks.focus(soft)
    flat = Image.new("L", (800, 600), 128)
    assert looks.focus(flat) == 0.0  # nothing in the frame says anything about focus


def test_the_best_frame_of_a_burst_avoids_closed_eyes():
    # (id, ts, name, device, sharp, faceq, video, focus, aesthetic, judged, closed)
    rows = [(1, 100, "a.jpg", "cam|4x3", 50.0, 0.8, 0, 300.0, 5.0, 2, 2),   # sharpest, but both faces blink
            (2, 101, "b.jpg", "cam|4x3", 48.0, 0.8, 0, 290.0, 5.1, 2, 0),
            (3, 102, "c.jpg", "cam|4x3", 10.0, 0.2, 0, 60.0, 4.8, 2, 0)]   # soft all over
    out = moments.find_bursts(rows)
    assert out[2][:2] == (1, 1)
    assert out[1] == (1, 0, 1) and out[3] == (1, 0, 1)  # both clearly worse: closed eyes, and blur
    # rows from before photo quality (seven fields) still work, on sharpness alone
    old = moments.find_bursts([r[:7] for r in rows])
    assert old[1][1] == 1 and set(old[1]) <= {0, 1}


def test_likely_rejects_carry_their_reasons():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE photos (id INTEGER, video INTEGER, focusn REAL, sharpn REAL, faceq REAL, eyes_checked INTEGER, eyes_closed INTEGER,"
                " bright REAL, clip_lo REAL, clip_hi REAL)")
    con.executemany("INSERT INTO photos VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, 0, 0.01, 0.5, None, 0, 0, 0.5, 0, 0),       # blurry all over
        (2, 0, 0.01, 0.5, 0.9, 0, 0, 0.5, 0, 0),        # soft frame, but a sharp face: not a reject
        (3, 0, 0.6, 0.6, 0.7, 3, 2, 0.5, 0, 0),         # most faces with closed eyes
        (4, 0, 0.6, 0.6, 0.7, 3, 1, 0.5, 0, 0),         # one of three: not a reject
        (5, 0, 0.6, 0.6, None, 0, 0, 0.02, 0.9, 0),     # nearly black
        (6, 0, 0.6, 0.6, None, 0, 0, 0.5, 0, 0),        # the worse frame of a burst
        (7, 0, None, 0.02, None, None, None, 0.5, 0, 0),  # not measured for focus yet: overall sharpness decides
        (8, 1, 0.0, 0.0, None, None, None, 0.0, 1, 1),  # a video: never
    ])
    got = dict((pid, why) for why, pid in index.likely_rejects(con, {6: (1, 0, 1), 4: (1, 1, 0)}))
    assert got == {1: "blur", 3: "eyes", 5: "exposure", 6: "burst", 7: "blur"}


def test_the_quality_filters():
    where, params = query.build_where([("rejects", "1"), ("reject", "eyes"), ("reject", "nonsense"), ("looks", "top"), ("eyes", "closed")])
    assert "reject IS NOT NULL" in where and "aesthn >= 0.9" in where and "eyes_closed > 0" in where
    assert params == ["%,eyes,%"]
    assert "looks" in query.SORTS


def test_a_model_installed_later_fills_in_only_its_part(client, lib, monkeypatch):
    if not (looks.aesthetic_available() or all((MODELS / f).exists() for f in ("clip_vit_l14_vision.onnx", "laion_aesthetic_l14.pth"))):
        pytest.skip("aesthetic model not available here")
    monkeypatch.setattr(models, "MODEL_DIR", MODELS)
    looks.reset_sessions()
    con = index.open_index()
    con.row_factory = sqlite3.Row
    photos = [dict(r) for r in con.execute("SELECT id, path, video, duration, w, h, hash, size, modified FROM photos WHERE video = 0 ORDER BY id LIMIT 4")]
    con.close()
    analysis.reset("looks")
    try:
        # first without the aesthetic model: focus only
        monkeypatch.setattr(looks, "aesthetic_available", lambda: False)
        monkeypatch.setattr(looks, "eyes_available", lambda: False)
        analysis._looks_job(photos, analysis._run["gen"])
        db = analysis.connect()
        first = {r[0]: r[1:] for r in db.execute("SELECT photo_id, aesthetic, focus FROM ai_looks")}
        db.close()
        assert set(first) == {p["id"] for p in photos} and all(a is None and f is not None for a, f in first.values())
        assert not analysis._looks_todo(photos, analysis._looks_parts())  # nothing left to do
        # the model arrives: only the aesthetic score is computed, focus is kept as it was
        monkeypatch.setattr(looks, "aesthetic_available", lambda: True)
        eyes = all((MODELS / f).exists() for f in ("eye_state_vit.safetensors", "face_detection_yunet_2023mar.onnx"))
        if eyes:
            from app.faces import facedetect
            facedetect.reset_session()
            monkeypatch.setattr(looks, "eyes_available", lambda: True)
        assert len(analysis._looks_todo(photos, analysis._looks_parts())) == len(photos)
        monkeypatch.setattr(looks, "focus", lambda im: pytest.fail("focus measured again"))
        analysis._looks_job(photos, analysis._run["gen"])
        db = analysis.connect()
        second = {r[0]: r[1:] for r in db.execute("SELECT photo_id, aesthetic, focus, eyes_checked, eyes_closed FROM ai_looks")}
        db.close()
        for pid, (a, f, judged, closed) in second.items():
            assert 1 < a < 10 and f == first[pid][1]
            assert not eyes or (judged is not None and 0 <= closed <= judged)
    finally:
        analysis.reset("looks")
        looks.reset_sessions()


def test_the_api_shows_quality_and_filters_likely_rejects(client, lib):
    con = index.open_index()
    a, b = [r[0] for r in con.execute("SELECT id FROM photos WHERE video = 0 ORDER BY id LIMIT 2")]
    con.close()
    db = analysis.connect()
    try:
        db.executemany("INSERT OR REPLACE INTO ai_looks VALUES (?,?,?,?,?)", [(a, 6.2, 900.0, 2, 2), (b, 4.1, 300.0, 2, 0)])
        db.commit()
        index.build()
        qa = client.get(f"/api/photo/{a}").json()["quality"]
        assert qa["aesthetic"] == 6.2 and qa["aestheticRank"] == 1.0 and qa["eyesChecked"] == 2 and qa["eyesClosed"] == 2
        assert "eyes" in qa["reject"] and "Eyes closed" in qa["rejectLabels"]
        assert "eyes" not in client.get(f"/api/photo/{b}").json()["quality"]["reject"]
        ids = client.get("/api/photos?reject=eyes").json()["id"]
        assert a in ids and b not in ids
        assert a in client.get("/api/photos?rejects=1").json()["id"]
        assert client.get("/api/photos?sort=looks").json()["id"][0] == a
        assert client.get("/api/facets?eyes=closed").json()["extras"]["reject_eyes"] >= 1
    finally:
        db.execute(f"DELETE FROM ai_looks WHERE photo_id IN ({a}, {b})")
        db.commit()
        db.close()
        index.build()


def test_the_aesthetic_model_downloads_and_counts_both_its_files(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MODEL_DIR", tmp_path)
    got = []
    monkeypatch.setattr(models, "fetch_file", lambda url, dst, member=None, sha256=None: (got.append((dst.name, sha256)), dst.write_bytes(b"x")))
    assert not models.local("aesthetic")["installed"]
    models._download_here("aesthetic")
    assert [g[0] for g in got] == ["clip_vit_l14_vision.onnx", "laion_aesthetic_l14.pth"] and all(len(g[1]) == 64 for g in got)
    assert models.local("aesthetic")["installed"]
    (tmp_path / "laion_aesthetic_l14.pth").unlink()
    assert not models.local("aesthetic")["installed"]  # half a model is not installed



def test_safetensors_reader_and_gelu(tmp_path):
    import json
    import struct
    a = np.arange(6, dtype="<f4").reshape(2, 3)
    head = json.dumps({"x": {"dtype": "F32", "shape": [2, 3], "data_offsets": [0, 24]}, "__metadata__": {}}).encode()
    p = tmp_path / "t.safetensors"
    p.write_bytes(struct.pack("<Q", len(head)) + head + a.tobytes())
    assert (looks.read_safetensors(p)["x"] == a).all()
    import math
    xs = np.linspace(-6, 6, 101).astype(np.float32)
    exact = np.array([0.5 * x * (1 + math.erf(x / math.sqrt(2))) for x in xs])
    assert np.abs(looks._gelu(xs) - exact).max() < 1e-5


def test_the_eye_model_tells_open_from_closed(monkeypatch):
    if not (MODELS / "eye_state_vit.safetensors").exists():
        pytest.skip("eye model not available here")
    from PIL import Image, ImageDraw
    monkeypatch.setattr(models, "MODEL_DIR", MODELS)
    looks.reset_sessions()

    def eye(open_):  # a drawn eye: an iris in an almond, or a lid line with lashes
        im = Image.new("RGB", (64, 64), (225, 185, 160))
        d = ImageDraw.Draw(im)
        if open_:
            d.ellipse((8, 20, 56, 44), fill=(245, 245, 245), outline=(60, 40, 30), width=2)
            d.ellipse((24, 22, 40, 42), fill=(70, 45, 25))
            d.ellipse((29, 28, 35, 36), fill=(5, 5, 5))
        else:
            d.arc((8, 14, 56, 40), 20, 160, fill=(60, 40, 30), width=3)
        return im
    p_open, p_closed = looks.eye_open([eye(True), eye(False)])
    assert p_open > p_closed
    looks.reset_sessions()
