"""Raw files open as the JPEG preview they carry, upright by the raw file's own orientation tag; the
preview cache is pruned by the files' modification time (access times stand still on noatime mounts)."""
import os
import shutil
import subprocess
import time

import pytest
from PIL import Image, UnidentifiedImageError

from app import config, thumbs
from app.photos import edits


def _fake_raw(tmp_path, ext=".cr2"):
    """A 'raw' file the way the test can make one: a JPEG with an embedded EXIF thumbnail and Orientation 6,
    under a raw extension. exiftool reads it by content, extracts the thumbnail and reports the orientation,
    exactly the two things the raw path asks of a real file."""
    big = tmp_path / "big.jpg"
    im = Image.new("RGB", (600, 400), (200, 30, 30))
    im.paste(Image.new("RGB", (300, 400), (30, 30, 200)), (0, 0))  # left half blue, right half red
    im.save(big, quality=90)
    thumb = tmp_path / "thumb.jpg"
    im.resize((160, 107)).save(thumb, quality=85)
    subprocess.run([*edits.exiftool_cmd(), "-q", "-overwrite_original", f"-ThumbnailImage<={thumb}", "-Orientation#=6", str(big)], check=True)
    raw = tmp_path / ("shot" + ext)
    shutil.copy(big, raw)
    return raw


@pytest.mark.skipif(not edits.EXIFTOOL, reason="exiftool is not available here")
def test_raw_file_opens_as_its_embedded_preview_turned_by_the_raw_orientation(tmp_path):
    raw = _fake_raw(tmp_path)
    im, _ = thumbs.open_oriented(str(raw), 200, 200)
    assert im.mode == "RGB"
    assert (im.width, im.height) == (107, 160), "the embedded 160x107 thumbnail, turned upright by Orientation 6 (rotate 90 CW)"
    # left half blue in the stored picture: after a 90 degree clockwise turn it is the top half
    r, g, b = im.getpixel((im.width // 2, 10))
    assert b > 150 and r < 100, "the top of the upright picture is the blue (left) half"
    r, g, b = im.getpixel((im.width // 2, im.height - 10))
    assert r > 150 and b < 100


@pytest.mark.skipif(not edits.EXIFTOOL, reason="exiftool is not available here")
def test_raw_thumbnail_is_cached_like_any_photo_and_a_raw_without_preview_gets_the_placeholder(tmp_path):
    raw = _fake_raw(tmp_path, ".nef")
    photo = {"id": 987_654_301, "path": str(raw), "video": 0, "duration": None, "w": 400, "h": 600, "hash": "rawtest1", "size": raw.stat().st_size, "modified": "2026-01-01T00:00:00"}
    try:
        out = thumbs.make("t", photo)
        with Image.open(out) as t:
            assert t.height > t.width, "a portrait thumbnail: the raw's orientation was applied"
        bad = tmp_path / "noise.arw"
        bad.write_bytes(os.urandom(4096))
        with pytest.raises(UnidentifiedImageError):
            thumbs.open_oriented(str(bad), 200, 200)
        photo2 = dict(photo, id=987_654_302, path=str(bad), hash="rawtest2", size=4096)
        out2 = thumbs.make("t", photo2)
        with Image.open(out2) as t:
            assert t.size[1] == 240, "the placeholder, as for any file that cannot be decoded"
    finally:
        for pid in (987_654_301, 987_654_302):
            thumbs.forget(pid)


def test_raw_path_without_exiftool_asks_pillow_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(edits, "EXIFTOOL", None)
    bad = tmp_path / "noise.dng"
    bad.write_bytes(b"\xff" * 64)
    with pytest.raises(UnidentifiedImageError):
        thumbs.open_oriented(str(bad), 200, 200)


def test_preview_cache_is_pruned_by_modification_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "PREVIEW_CACHE_MAX_MB", 1)
    d = tmp_path / "p" / "00"
    d.mkdir(parents=True)
    now = time.time()
    files = {}
    for name, age in (("1-a.jpg", 300), ("2-b.jpg", 200), ("3-c.jpg", 100)):
        f = d / name
        f.write_bytes(b"x" * 600_000)
        # made `age` seconds ago, but "read" in the opposite order: the newest-made file was read longest ago
        os.utime(f, (now - 1000 + age, now - age))
        files[name] = f
    thumbs.prune_previews()
    left = sorted(p.name for p in d.iterdir())
    assert left == ["3-c.jpg"], f"the two oldest-made files go, whatever their access times: {left}"
