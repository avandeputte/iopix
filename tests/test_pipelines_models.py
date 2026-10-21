"""Model downloads are pinned to a commit or release and verified against a recorded sha256, and
the installed copy of a model has an identity (installed_revision) that the data computed from it
records."""
import hashlib
import logging
import zipfile

import pytest

from app.ai import models


def test_plain_downloads_are_pinned_and_carry_a_checksum():
    for key, m in models.REGISTRY.items():
        if m["kind"] in ("url", "zip"):
            assert "/main/" not in m["url"] and "releases/latest" not in m["url"], f"{key} is not pinned"
            assert len(m.get("sha256", "")) == 64 and int(m["sha256"], 16) >= 0, f"{key} has no sha256"


def test_fetch_file_verifies_the_checksum(tmp_path, caplog):
    src = tmp_path / "model.onnx"
    src.write_bytes(b"weights" * 1000)
    good = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "out"
    models.fetch_file(src.as_uri(), out / "model.onnx", sha256=good.upper())
    assert (out / "model.onnx").read_bytes() == src.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        models.fetch_file(src.as_uri(), out / "bad.onnx", sha256="0" * 64)
    assert sorted(p.name for p in out.iterdir()) == ["model.onnx"], "nothing of a refused download is kept, not even the .part file"
    with caplog.at_level(logging.WARNING, logger="app.ai.models"):
        models.fetch_file(src.as_uri(), out / "unverified.onnx")
    assert (out / "unverified.onnx").exists() and "no checksum" in caplog.text, "no checksum recorded: kept, with a warning"


def test_fetch_file_verifies_the_member_taken_out_of_a_zip(tmp_path):
    z = tmp_path / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("other.onnx", b"no")
        zf.writestr("models/det.onnx", b"detector")
    out = tmp_path / "out"
    models.fetch_file(z.as_uri(), out / "det.onnx", "det.onnx", hashlib.sha256(b"detector").hexdigest())
    assert (out / "det.onnx").read_bytes() == b"detector"
    with pytest.raises(ValueError, match="checksum"):
        models.fetch_file(z.as_uri(), out / "det2.onnx", "det.onnx", "f" * 64)
    assert sorted(p.name for p in out.iterdir()) == ["det.onnx"]


def test_installed_revision_per_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MODEL_DIR", tmp_path)
    assert models.installed_revision("yunet") is None, "not installed"
    f = tmp_path / models.REGISTRY["yunet"]["file"]
    f.write_bytes(b"onnx")
    assert models.installed_revision("yunet") == hashlib.sha256(b"onnx").hexdigest()
    f.write_bytes(b"onnx, another file")
    assert models.installed_revision("yunet") == hashlib.sha256(b"onnx, another file").hexdigest(), "a replaced file is another revision"
    for repo in models.REGISTRY["clip"]["repos"]:
        d = tmp_path / f"models--{repo.replace('/', '--')}"
        (d / "refs").mkdir(parents=True)
        (d / "refs" / "main").write_text("abc123def\n")
        (d / "snapshots" / "abc123def").mkdir(parents=True)
        (d / "snapshots" / "abc123def" / "model.onnx").write_text("")
    assert models.installed_revision("clip") == "abc123def+abc123def", "the full commit ids, one per repository"
    assert models.installed_revision("geonames") is None, "data without a revision"
    assert models.installed_revision("nonsense") is None
