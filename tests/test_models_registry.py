"""The models registry: what is installed is read from disk, update checks
compare the right thing per source, and the network is never required."""
import io
import json

import pytest

from app.ai import models


class _Resp(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _fake_get(table):
    def get(url, head=False):
        for key, (body, headers) in table.items():
            if key in url:
                return _Resp(body, headers)
        raise OSError("offline: " + url)
    return get


@pytest.fixture
def hf_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(models, "RECORD", tmp_path / "downloads.json")
    repo = models.REGISTRY["blip"]["repos"][0]
    d = tmp_path / f"models--{repo.replace('/', '--')}"
    (d / "refs").mkdir(parents=True)
    (d / "refs" / "main").write_text("0123456789abcdef\n")
    (d / "snapshots" / "0123456789abcdef").mkdir(parents=True)
    (d / "snapshots" / "0123456789abcdef" / "config.json").write_text("{}")
    return tmp_path


def test_hf_local_revision_and_missing(hf_cache):
    loc = models.local("blip")
    assert loc["installed"] and loc["revision"] == "0123456"
    assert models.local("clip")["installed"] is False  # nothing cached for it


def test_hf_check_current_vs_update(hf_cache, monkeypatch):
    repo = models.REGISTRY["blip"]["repos"][0]
    monkeypatch.setattr(models, "_get", _fake_get({repo: (json.dumps({"sha": "0123456789abcdef", "lastModified": "2025-01-01T00:00:00Z"}).encode(), {})}))
    assert models.check("blip")["status"] == "current"
    monkeypatch.setattr(models, "_get", _fake_get({repo: (json.dumps({"sha": "ffffffffffffffff"}).encode(), {})}))
    out = models.check("blip")
    assert out["status"] == "update" and out["remote"] == "fffffff"


def test_check_reports_errors_instead_of_raising(hf_cache, monkeypatch):
    monkeypatch.setattr(models, "_get", _fake_get({}))
    out = models.check("blip")
    assert out["status"] == "error" and "offline" in out["error"]


def test_pip_version_comparison(monkeypatch):
    monkeypatch.setattr(models, "local", lambda key: {"installed": True, "revision": "3.9.0"})
    monkeypatch.setattr(models, "_get", _fake_get({"pypi.org": (json.dumps({"info": {"version": "3.10.0"}}).encode(), {})}))
    assert models.check("ocr")["status"] == "update"
    monkeypatch.setattr(models, "local", lambda key: {"installed": True, "revision": "3.10.0"})
    assert models.check("ocr")["status"] == "current"


def test_data_source_is_not_nagging_for_daily_exports(monkeypatch):
    import time
    monkeypatch.setattr(models, "local", lambda key: {"installed": True, "revision": "2026-01-01", "at": int(time.time()) - 3600})
    monkeypatch.setattr(models, "_get", _fake_get({"geonames": (b"", {"Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())})}))
    assert models.check("geonames")["status"] == "current"


def test_listing_shape():
    out = models.listing()
    keys = {m["key"] for m in out["models"]}
    assert keys == set(models.REGISTRY)
    for m in out["models"]:
        assert {"label", "what", "page", "size", "kind", "installed"} <= set(m)
    assert "running" in out["progress"]


def test_choice_groups_and_defaults(monkeypatch):
    from app.data import state
    for group, c in models.CHOICES.items():
        opts = models.options(group)
        assert c["default"] in opts and len(opts) >= 2, group
    state.set_setting("face_detector", "scrfd")
    assert models.chosen("face_detector") == "scrfd"
    assert models.path_for("face_detector").name == "scrfd_10g.onnx"
    state.set_setting("face_detector", "yolox")  # a model of another feature: ignored
    assert models.chosen("face_detector") == "yunet"
    state.set_setting("face_detector", "")
    assert models.listing()["choices"]["object_model"] == {"label": "Object detection", "chosen": "yolox", "options": ["yolox", "yolox_m", "yolox_l", "yolox_x"]}


def test_fetch_file_takes_one_member_out_of_a_zip(tmp_path):
    import zipfile
    z = tmp_path / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("other.onnx", b"no")
        zf.writestr("models/det_10g.onnx", b"yes")
    dst = tmp_path / "out" / "scrfd_10g.onnx"
    models.fetch_file(z.as_uri(), dst, "det_10g.onnx")
    assert dst.read_bytes() == b"yes" and sorted(p.name for p in dst.parent.iterdir()) == ["scrfd_10g.onnx"]
    with pytest.raises(FileNotFoundError):
        models.fetch_file(z.as_uri(), tmp_path / "out" / "x.onnx", "missing.onnx")
    models.fetch_file(z.as_uri(), tmp_path / "plain.zip")
    assert (tmp_path / "plain.zip").read_bytes() == z.read_bytes()
