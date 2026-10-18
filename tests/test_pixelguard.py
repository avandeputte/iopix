"""Writing metadata into an image file must leave the picture itself alone: the image data is hashed before
and after, and a write that changed it is undone."""
import shutil
from pathlib import Path

import pytest

from app.photos import edits, pixelguard


def _jpeg(lib, tmp_path) -> Path:
    src = next((p for p in Path(lib["root"]).rglob("*") if p.suffix.lower() in (".jpg", ".jpeg")), None)
    if src is None:
        pytest.skip("no JPEG in the scratch library")
    dst = tmp_path / "probe.jpg"
    shutil.copy2(src, dst)
    return dst


def test_metadata_write_keeps_the_image_data(client, lib, tmp_path):
    if not edits.available():
        pytest.skip("exiftool not available")
    f = _jpeg(lib, tmp_path)
    before = pixelguard.hashes([str(f)])[str(f)]
    if not before:
        pytest.skip("this exiftool cannot hash image data")
    failed = edits.run_exiftool([(str(f), ["-XMP-dc:Title=probe title", "-GPSLatitude=40.5", "-GPSLatitudeRef=N"])])
    assert failed == []
    assert pixelguard.hashes([str(f)])[str(f)] == before, "same pixels, different metadata"


def test_a_write_that_changes_the_picture_is_undone(client, lib, tmp_path):
    if not edits.available():
        pytest.skip("exiftool not available")
    from PIL import Image
    f = _jpeg(lib, tmp_path)
    original = f.read_bytes()
    with pixelguard.Guard([str(f)]) as g:
        if not g.before.get(str(f)):
            pytest.skip("this exiftool cannot hash image data")
        im = Image.open(f).convert("RGB")
        im.putpixel((0, 0), (255, 0, 0))
        im.save(f, "JPEG", quality=70)  # a "metadata write" that re-encoded the picture
        bad = g.verify()
    assert bad == [str(f)]
    assert f.read_bytes() == original, "the file is back as it was"
