"""Content search only counts as available when a model is really on disk: a
models folder holding other downloads (YOLOX, BLIP) must not make the viewer
try to load CLIP and fail at every start (seen on a fresh Linux install)."""
from app.ai import semantic


def test_models_folder_alone_is_not_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "MODEL_DIR", tmp_path)
    (tmp_path / "yolox_s.onnx").write_bytes(b"x")
    assert semantic._ready("clip") is False
    assert semantic._ready("siglip2") is False
    monkeypatch.setattr(semantic, "_HAVE", True)
    monkeypatch.setattr(semantic.config, "SEMANTIC", True)
    assert semantic.enabled() is False


def test_clip_cache_or_marker_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "MODEL_DIR", tmp_path)
    ref = tmp_path / "models--Qdrant--clip-ViT-B-32-vision" / "refs"
    ref.mkdir(parents=True)
    (ref / "main").write_text("abc\n")
    assert semantic._ready("clip") is True
    (tmp_path / "siglip2.ready").write_text("ok\n")
    assert semantic._ready("siglip2") is True
    monkeypatch.setattr(semantic, "_HAVE", True)
    monkeypatch.setattr(semantic.config, "SEMANTIC", True)
    assert semantic.enabled() is True
