"""Face grouping on either model: ArcFace crops are aligned on their landmarks first and tell people
apart better than the raw crops; a model switch throws the old vectors away once."""
import itertools
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from app.faces import facedetect, facevecs
from app.data import index, state, vdb
from app.ai import models
from app import thumbs

MODELS = Path("/private/tmp/claude-501/-Users-alex-Pictures/cd6ab508-dd1f-4df1-999c-d6fe0a83fe5c/scratchpad/models")


def test_similarity_transform_maps_landmarks_onto_the_template():
    pts = [(x * 2 + 10, y * 2 + 5) for x, y in facevecs.ARCFACE_TEMPLATE]  # the template scaled and shifted
    m = facevecs._similarity(pts, facevecs.ARCFACE_TEMPLATE)
    src = np.hstack([np.asarray(pts), np.ones((5, 1))])
    assert np.allclose(src @ m.T, np.asarray(facevecs.ARCFACE_TEMPLATE), atol=1e-6)


def _people(limit=4, each=4):
    con = index.open_index()
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT f.tag_id, f.photo_id, f.x, f.y, f.w, f.h, p.path, p.w pw, p.h ph FROM faces f JOIN photos p ON p.id = f.photo_id "
                           "WHERE f.kind = 'confirmed' AND p.video = 0 ORDER BY f.tag_id, f.photo_id").fetchall()
    finally:
        con.close()
    by = {}
    for r in rows:
        by.setdefault(r["tag_id"], []).append(r)
    return {k: v[:each] for k, v in by.items() if len(v) >= 2}


@pytest.mark.parametrize("model", ["sface", "arcface"])
def test_vectors_separate_people(client, lib, monkeypatch, model):
    if facevecs.np is None or not (MODELS / models.REGISTRY[model]["file"]).exists() or not (MODELS / models.REGISTRY["yunet"]["file"]).exists():
        pytest.skip(f"{model} model not available here")
    people = _people()
    if len(people) < 2:
        pytest.skip("the fixture library needs two people with two confirmed faces each")
    monkeypatch.setattr(models, "MODEL_DIR", MODELS)
    state.set_setting("face_vectors", model)
    state.set_setting("face_detector", "yunet")
    facevecs.reset_session(); facedetect.reset_session()
    try:
        assert facevecs.chosen() == model and facevecs.available()
        vecs = {}
        for tag, rs in list(people.items())[:4]:
            paths = [str(thumbs.make_face(0, {"id": r["photo_id"], "path": r["path"], "w": r["pw"], "h": r["ph"]}, (r["x"], r["y"], r["w"], r["h"]))) for r in rs]
            vecs[tag] = facevecs.embed_paths(paths)
            assert vecs[tag].shape[1] == (512 if model == "arcface" else 128)
            assert np.allclose(np.linalg.norm(vecs[tag], axis=1), 1, atol=1e-4)
        same = [float(x) for v in vecs.values() for x in (v @ v.T)[np.triu_indices(len(v), 1)]]
        diff = [float(x) for a, b in itertools.combinations(vecs, 2) for x in (vecs[a] @ vecs[b].T).ravel()]
        print(f"{model}: same-person similarity {np.mean(same):.2f}, different people {np.mean(diff):.2f}")
        assert np.mean(same) > np.mean(diff), (model, np.mean(same), np.mean(diff))
    finally:
        state.set_setting("face_vectors", ""); state.set_setting("face_detector", "")
        facevecs.reset_session(); facedetect.reset_session()


def test_switching_the_model_forgets_the_old_vectors(client, lib, monkeypatch):
    con = vdb.connect()
    try:
        con.execute("INSERT OR REPLACE INTO ai_face_embeddings (photo_id, x, y, w, h, vec) VALUES (1, 0, 0, 10, 10, ?)", (np.zeros(128, np.float32).tobytes(),))
        con.commit()
    finally:
        con.close()
    state.set_setting("face_vectors_built", "sface")
    monkeypatch.setattr(facevecs, "chosen", lambda: "arcface")
    facevecs._same_model()
    con = vdb.connect()
    try:
        assert con.execute("SELECT count(*) FROM ai_face_embeddings").fetchone()[0] == 0
    finally:
        con.close()
    assert state.get_setting("face_vectors_built") == "arcface"
    facevecs._same_model()  # a second call changes nothing
    state.set_setting("face_vectors_built", "sface")
