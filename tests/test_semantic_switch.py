"""Switching the search model: the new index takes over once every photo that can be embedded
is in it (files without a thumbnail do not block it forever), and on an install that never
had a CLIP index the new index answers from the start instead of nothing."""
import numpy as np

from app.ai import semantic
from app.data import state


def _fresh(tmp_path, monkeypatch, clip_index: bool):
    monkeypatch.setattr(semantic, "_HAVE", True)
    monkeypatch.setattr(semantic.config, "SEMANTIC", True)
    monkeypatch.setattr(semantic, "_ready", lambda name: name == "siglip2" or clip_index)
    s = semantic.Semantic()
    for e in s.engines.values():
        e.file = tmp_path / e.cfg["file"]
        e.complete_marker = tmp_path / (e.cfg["file"] + ".complete")
        e.ids, e.vecs = np.zeros(0, np.int32), np.zeros((0, e.cfg["dim"]), np.float32)
        monkeypatch.setattr(e, "load", lambda: None)
    if clip_index:
        s.engines["clip"].file.write_bytes(b"x")
    state.set_setting("search_model", "siglip2")
    return s


def _stub_start(engine, have, failed):
    def start(photos):
        engine.ids = np.asarray(have, np.int32)
        engine.failed = set(failed)
        engine.running = False
        if engine.on_finish:
            engine.on_finish(engine)
    return start


def test_switch_waits_for_missing_photos_but_not_for_unembeddable_ones(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch, clip_index=True)
    photos = [{"id": i} for i in (1, 2, 3)]
    new = s.engines["siglip2"]
    assert s.active is s.engines["clip"] and s.upgrade is new
    monkeypatch.setattr(new, "start", _stub_start(new, have=[1], failed=[]))
    s.start(photos)
    assert not new.complete_marker.exists(), "two photos still to index: no switch yet"
    monkeypatch.setattr(new, "start", _stub_start(new, have=[1, 3], failed=[2]))
    s.start(photos)
    assert new.complete_marker.exists(), "photo 2 cannot be embedded: that must not block the switch"
    assert s.active is new and s.upgrade is None


def test_without_a_clip_index_the_new_engine_answers_while_it_is_built(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch, clip_index=False)
    new = s.engines["siglip2"]
    assert s.active is new, "no CLIP index: search the growing SigLIP index rather than nothing"
    assert s.upgrade is new, "it is still being completed"


def test_an_error_inside_the_active_engine_lookup_surfaces_instead_of_recursing(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch, clip_index=True)
    monkeypatch.setattr(semantic.Semantic, "_complete", lambda self, e: (_ for _ in ()).throw(AttributeError("inner")))
    import pytest
    with pytest.raises(RuntimeError, match="inner"):
        s.active


def test_revision_lookup_survives_a_missing_numpy(tmp_path, monkeypatch):
    f = tmp_path / "embeddings-clip.npz"
    f.write_bytes(b"x")
    monkeypatch.setattr(semantic, "np", None)
    semantic._revisions.clear()
    assert semantic._stored_revision(f) is None
