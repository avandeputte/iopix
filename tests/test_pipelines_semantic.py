"""The vector pipelines: embeddings carry the model revision they came from and are dropped when the
installed model is another revision; generations are read from the saved file (so they survive a
restart); the concept table, the near-duplicate pairs and the face groups are computed in blocks with
the same results as the whole-matrix versions they replace."""
import threading
import time

import numpy as np

from app.ai import concepts, neardups, semantic
from app.data import state, vdb
from app.faces import facevecs


def _enable(monkeypatch, rev="rev-A"):
    monkeypatch.setattr(semantic, "_HAVE", True)
    monkeypatch.setattr(semantic.config, "SEMANTIC", True)
    monkeypatch.setattr(semantic, "_ready", lambda name: True)
    cur = {"rev": rev}
    monkeypatch.setattr(semantic.Engine, "installed_revision", lambda self: cur["rev"])
    return cur


def _engine(tmp_path, name="clip"):
    e = semantic.Engine(name)
    e.file = tmp_path / e.cfg["file"]
    e.complete_marker = tmp_path / (e.cfg["file"] + ".complete")
    return e


def _vectors(n, dim, seed=0, dtype=np.float32):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, dim)).astype(dtype)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


# ---- the model revision as part of every vector's identity
def test_embeddings_carry_the_model_revision_and_are_dropped_when_it_changes(tmp_path, monkeypatch):
    cur = _enable(monkeypatch)
    e = _engine(tmp_path)
    e.load()
    assert e.revision == "rev-A" and len(e.ids) == 0
    e.ids, e.vecs = np.arange(5, dtype=np.int32), _vectors(5, 512)
    with e._lock:
        e._save()
    e.complete_marker.write_text("ok\n")
    assert semantic._stored_revision(e.file) == "rev-A"
    again = _engine(tmp_path)  # a restart with the same model
    again.load()
    assert again.ids.tolist() == [0, 1, 2, 3, 4] and again.revision == "rev-A"
    cur["rev"] = "rev-B"  # the model was downloaded again and is another revision
    third = _engine(tmp_path)
    third.load()
    assert len(third.ids) == 0 and third.revision == "rev-B", "vectors of another revision are dropped"
    assert not third.file.exists() and not third.complete_marker.exists(), "the index is incomplete until rebuilt"
    # a loaded engine notices too (models._reset calls this after a download)
    again.check_revision()
    assert len(again.ids) == 0 and again.revision == "rev-B"


def test_a_file_from_before_revisions_is_adopted_and_clip_gets_its_marker(tmp_path, monkeypatch):
    _enable(monkeypatch)
    e = _engine(tmp_path)
    np.savez(e.file, ids=np.arange(3, dtype=np.int32), vecs=_vectors(3, 512).astype(np.float16))  # as earlier versions wrote it
    assert semantic._stored_revision(e.file) is None
    e.load()
    assert len(e.ids) == 3 and e.revision == "rev-A", "kept: computed with the model installed then"
    assert e.complete_marker.exists(), "complete by the old rule (CLIP's file exists); the marker says so from now on"
    with e._lock:
        e._save()
    assert semantic._stored_revision(e.file) == "rev-A", "stamped at the first save"


def test_a_rebuilt_clip_index_is_not_complete_until_marked(tmp_path, monkeypatch):
    _enable(monkeypatch)
    s = semantic.Semantic()
    for e in s.engines.values():
        e.file = tmp_path / e.cfg["file"]
        e.complete_marker = tmp_path / (e.cfg["file"] + ".complete")
    clip = s.engines["clip"]
    np.savez(clip.file, ids=np.arange(2, dtype=np.int32), vecs=_vectors(2, 512).astype(np.float16))
    assert s._complete(clip), "a CLIP file from before markers counts as complete"
    clip.file.unlink()
    semantic._write(clip.file, np.arange(1, dtype=np.int32), _vectors(1, 512), "rev-A")
    assert not s._complete(clip), "written by this version (it carries a revision): it needs the marker like every other index"
    clip.complete_marker.write_text("ok\n")
    assert s._complete(clip)


def test_pruning_a_file_that_is_not_loaded_keeps_its_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "_HAVE", True)
    g = semantic.semantic.engines["clip"]
    monkeypatch.setattr(g, "file", tmp_path / "embeddings.npz")
    monkeypatch.setattr(g, "ids", None)
    monkeypatch.setattr(g, "vecs", None)
    for other in semantic.semantic.engines.values():
        if other is not g:
            monkeypatch.setattr(other, "file", tmp_path / other.cfg["file"])  # absent
    semantic._write(g.file, np.arange(4, dtype=np.int32), _vectors(4, 512), "rev-A")
    semantic.remove_ids([1, 2])
    ids, vecs, rev = semantic._read(g.file)
    assert ids.tolist() == [0, 3] and vecs.shape == (2, 512) and rev == "rev-A" and g.ids is None
    semantic.remap_ids({3: 9})
    ids, _, rev = semantic._read(g.file)
    assert ids.tolist() == [0, 9] and rev == "rev-A"


# ---- generations from the saved file, not an in-memory counter
def test_the_index_key_comes_from_the_saved_file_and_survives_a_restart(tmp_path, monkeypatch):
    _enable(monkeypatch)
    e = _engine(tmp_path)
    assert e.file_key() == "clip:0", "no file yet"
    e.ids, e.vecs = np.arange(4, dtype=np.int32), _vectors(4, 512)
    with e._lock:
        e._save()
    k1 = e.file_key()
    assert _engine(tmp_path).file_key() == k1, "the same file after a restart: the same key"
    time.sleep(0.01)
    e.ids, e.vecs = np.arange(5, dtype=np.int32), _vectors(5, 512)
    with e._lock:
        e._save()
    assert e.file_key() != k1, "more vectors saved: another key"

    class Sem:
        active = e

        @property
        def key(self):
            return self.active.file_key()
    monkeypatch.setattr(neardups, "semantic", Sem())
    assert neardups.expected_gen("normal") == f"{e.file_key()}:normal"


# ---- the concept table: chunks + argpartition, rebuilt at most once a minute while indexing
def test_concept_table_in_chunks_matches_the_full_computation(monkeypatch):
    rng = np.random.default_rng(1)
    n, dim = 1000, 64
    vecs = _vectors(n, dim, seed=1)
    T = _vectors(len(concepts.NAMES), dim, seed=2)
    ids = rng.permutation(n).astype(np.int32)

    class Stub:
        _lock = threading.Lock()
        key = "stub:1"

        def load(self):
            pass
    stub = Stub()
    stub.ids, stub.vecs = ids, vecs
    monkeypatch.setattr(concepts, "semantic", stub)
    monkeypatch.setattr(concepts, "_text_matrix", lambda: T)
    monkeypatch.setattr(concepts, "CHUNK", 128)  # several chunks, the last one partial
    d = concepts._build()
    with np.errstate(all="ignore"):  # Accelerate emits spurious float warnings on matmul
        logits = 100.0 * (vecs @ T.T)
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    p /= p.sum(axis=1, keepdims=True)
    top = np.argsort(-p, axis=1)[:, :concepts.MAX_LABELS]
    top_p = np.take_along_axis(p, top, axis=1)
    top_p = np.where(top_p < concepts.SECONDARY_RATIO * top_p[:, :1], 0.0, top_p)
    assert d["top"].shape == (n, 3) and (d["top"] == top).all()
    assert np.allclose(d["top_p"], top_p, atol=1e-5)
    assert d["gen"] == "stub:1" and d["pos"][int(ids[7])] == 7


def test_concept_table_is_rebuilt_at_most_once_a_minute_while_indexing(monkeypatch):
    calls = []

    class Stub:
        running = True
        key = "stub:1"
    stub = Stub()

    def build():
        calls.append(stub.key)
        return {"gen": stub.key, "built": time.time(), "ids": np.arange(1), "top": np.zeros((1, 3), int), "top_p": np.zeros((1, 3), np.float32), "pos": {0: 0}}
    monkeypatch.setattr(concepts, "semantic", stub)
    monkeypatch.setattr(concepts, "enabled", lambda: True)
    monkeypatch.setattr(concepts, "_build", build)
    monkeypatch.setattr(concepts, "_cache", {"gen": None})
    assert concepts._data()["gen"] == "stub:1"
    stub.key = "stub:2"  # the job saved more embeddings
    assert concepts._data()["gen"] == "stub:1" and calls == ["stub:1"], "the previous table answers while the job runs"
    concepts._cache["built"] -= concepts.REBUILD_EVERY + 1
    assert concepts._data()["gen"] == "stub:2" and calls == ["stub:1", "stub:2"], "a minute later it is rebuilt"
    stub.running = False
    stub.key = "stub:3"
    assert concepts._data()["gen"] == "stub:3", "no job running: a change is picked up at once"


# ---- near-duplicates: blocks of rows, pairs written per block
def test_near_duplicate_pairs_in_blocks_match_the_full_comparison(monkeypatch):
    n, dim = 700, 32
    rng = np.random.default_rng(2)
    base = rng.normal(size=(n, dim))
    for i in range(0, n, 9):  # planted near copies: a photo and its resized / re-saved twin
        base[i + 1] = base[i] + 0.04 * rng.normal(size=dim)
    vecs = base / np.linalg.norm(base, axis=1, keepdims=True)  # float64: the blocked and the full product agree to the last bit, so no pair sits on the threshold by rounding
    ids = np.arange(100, 100 + n, dtype=np.int32)

    class Eng:
        _lock = threading.Lock()

        def load(self):
            pass
    eng = Eng()
    eng.ids, eng.vecs = ids, vecs

    class Sem:
        active = eng
        key = "stub:7"
    monkeypatch.setattr(neardups, "semantic", Sem())
    neardups._run["gen"] += 1
    try:
        neardups._job(neardups._run["gen"], "loose")
        assert neardups.progress["error"] is None
        con = vdb.connect()
        try:
            pairs = {(a, b): s for a, b, s in con.execute("SELECT a, b, score FROM ai_similar_photos")}
        finally:
            con.close()
        threshold = neardups.LEVELS["loose"]
        with np.errstate(all="ignore"):  # Accelerate emits spurious float warnings on matmul
            sims = vecs @ vecs.T
        expect = {}
        for i in range(n):
            row = sims[i].copy()
            row[: i + 1] = -1
            js = np.nonzero(row >= threshold)[0]
            if len(js) > neardups.PER_PHOTO:
                js = js[np.argsort(-row[js])[:neardups.PER_PHOTO]]
            for j in js.tolist():
                expect[(int(ids[i]), int(ids[j]))] = float(row[j])
        assert len(expect) >= n // 9 and set(pairs) == set(expect)
        assert all(abs(pairs[k] - expect[k]) < 1e-6 for k in expect)
        assert state.get_setting("near_dups_gen") == "stub:7:loose" and state.get_setting("near_dups_level") == "loose"
    finally:
        con = vdb.connect()
        try:
            con.execute("DELETE FROM ai_similar_photos")
            con.commit()
        finally:
            con.close()
        state.set_setting("near_dups_gen", "")


# ---- face groups: row blocks against the whole matrix, a union-find over the pairs
def test_face_groups_in_blocks_match_the_full_comparison():
    n, dim = 2500, 16  # more than two blocks of 1024
    rng = np.random.default_rng(3)
    m = rng.normal(size=(n, dim))
    for c in range(12):  # planted look-alike clusters of 6 faces
        m[c * 6:(c + 1) * 6] = m[c * 6] + 0.15 * rng.normal(size=(6, dim))
    m = m / np.linalg.norm(m, axis=1, keepdims=True)
    threshold = 0.7
    comps = facevecs._components(m, threshold)
    with np.errstate(all="ignore"):  # Accelerate emits spurious float warnings on matmul
        sims = m @ m.T
    ii, jj = np.nonzero(np.triu(sims, 1) >= threshold)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, j in zip(ii.tolist(), jj.tolist()):
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a
    expect = {}
    for i in range(n):
        expect.setdefault(find(i), []).append(i)
    assert sorted(sorted(v) for v in comps.values()) == sorted(sorted(v) for v in expect.values())
    assert sum(1 for v in comps.values() if len(v) > 1) >= 12 and sum(len(v) for v in comps.values()) == n


def test_face_vectors_are_dropped_when_the_model_revision_changes(monkeypatch):
    monkeypatch.setattr(facevecs, "chosen", lambda: "sface")
    revs = {"r": "aaa"}
    monkeypatch.setattr(facevecs.models, "installed_revision", lambda key: revs["r"])

    def count():
        con = vdb.connect()
        try:
            return con.execute("SELECT count(*) FROM ai_face_embeddings").fetchone()[0]
        finally:
            con.close()

    def seed():
        con = vdb.connect()
        try:
            con.execute("INSERT OR REPLACE INTO ai_face_embeddings (photo_id, x, y, w, h, vec) VALUES (1, 0, 0, 10, 10, ?)", (np.zeros(128, np.float32).tobytes(),))
            con.commit()
        finally:
            con.close()
    try:
        seed()
        state.set_setting("face_vectors_built", "sface")  # what versions before revisions wrote
        facevecs._same_model()
        assert count() == 1 and state.get_setting("face_vectors_built") == "sface@aaa", "adopted, and the revision is recorded from now on"
        revs["r"] = "bbb"
        facevecs._same_model()
        assert count() == 0 and state.get_setting("face_vectors_built") == "sface@bbb", "the same model as another file: recomputed"
        seed()
        facevecs._same_model()
        assert count() == 1, "nothing changed: nothing dropped"
    finally:
        con = vdb.connect()
        try:
            con.execute("DELETE FROM ai_face_embeddings")
            con.commit()
        finally:
            con.close()
        state.set_setting("face_vectors_built", "sface")
