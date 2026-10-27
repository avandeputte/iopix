"""A JPEG without its closing marker (some phones' burst frames) still gets a real thumbnail, not a black square."""
import io

from PIL import Image

from app import thumbs


def test_a_jpeg_missing_its_end_marker_gets_a_thumbnail(tmp_path, monkeypatch):
    im = Image.new("RGB", (800, 600), (200, 40, 40))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    assert data.endswith(b"\xff\xd9")
    src = tmp_path / "BURST_COVER.jpg"
    src.write_bytes(data[:-2])  # no FFD9, as in the Pixel burst frames
    monkeypatch.setattr(thumbs.config, "CACHE_DIR", tmp_path / "cache")
    st = src.stat()
    out = thumbs.make("t", {"id": 1, "path": str(src), "video": 0, "duration": None, "w": 800, "h": 600,
                            "hash": "x", "size": st.st_size, "modified": "2017-06-07T19:30:42"})
    with Image.open(out) as t:
        r, g, b = t.convert("RGB").getpixel((t.width // 2, t.height // 2))
    assert r > 150 and g < 90 and b < 90  # the picture itself, not a placeholder
