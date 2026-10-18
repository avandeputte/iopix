"""Look-alike face groups and near-duplicate sets are connected components over stored
similarities; the viewer proposes no names (groups only), both pages degrade without data."""
import numpy as np

from app.faces import facevecs
from app.data import index, vdb
from app.ai import neardups


def _unknown_faces(n):
    con = index.open_index()
    try:
        return con.execute("SELECT f.photo_id, f.x, f.y, f.w, f.h FROM faces f JOIN photos p ON p.id = f.photo_id "
                           "WHERE f.kind = 'unknown' AND p.video = 0 LIMIT ?", (n,)).fetchall()
    finally:
        con.close()


def _vec(seed, like=None, noise=0.0):
    rng = np.random.default_rng(seed)
    v = like + rng.normal(0, noise, 128) if like is not None else rng.normal(0, 1, 128)
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_face_groups_from_vectors(client):
    faces = _unknown_faces(4)
    if len(faces) < 3:
        import pytest
        pytest.skip("the scratch library has fewer than 3 unnamed faces")
    base = _vec(1)
    vecs = [base, _vec(2, base, 0.05), _vec(3)] + ([_vec(4)] if len(faces) > 3 else [])  # two alike, the rest on their own
    con = vdb.connect()
    try:
        con.execute("DELETE FROM ai_face_embeddings")
        con.executemany("INSERT INTO ai_face_embeddings (photo_id, x, y, w, h, vec) VALUES (?,?,?,?,?,?)",
                        [(*f, vecs[i].tobytes()) for i, f in enumerate(faces)])
        con.commit()
    finally:
        con.close()
    g = facevecs.groups("unknown", 0.45)
    assert g["groups"] and g["groups"][0]["n"] == 2, g["groups"][:1]
    pair = {(f["photo"], f["x"]) for f in g["groups"][0]["faces"]}
    assert pair == {(faces[0][0], faces[0][1]), (faces[1][0], faces[1][1])}
    assert g["groups"][0]["suggest"] is None and g["people"] == 0, "no named-person prototypes yet: no guess"
    assert len(g["ungrouped"]) >= 1
    # a prototype of a named person that matches the pair: the group and that face carry a suggestion
    person = index.open_index().execute("SELECT id, name FROM tags WHERE person = 1 AND n_all > 0 LIMIT 1").fetchone()
    con = vdb.connect()
    try:
        con.execute("INSERT OR REPLACE INTO ai_person_references (tag_id, photo_id, x, y, w, h, vec) VALUES (?,?,?,?,?,?,?)", (person[0], 999999, 1, 1, 1, 1, _vec(5, base, 0.05).tobytes()))
        con.commit()
    finally:
        con.close()
    g = facevecs.groups("unknown", 0.45)
    s0 = g["groups"][0]["suggest"]
    assert s0 and s0["tag"] == person[0] and s0["name"] == person[1] and s0["score"] >= 0.4
    assert all(f["suggest"] is None or f["suggest"]["score"] < 0.4 for f in g["ungrouped"]), "a face far from every prototype gets no guess"
    con = vdb.connect(); con.execute("DELETE FROM ai_person_references"); con.commit(); con.close()
    r = client.get("/api/faces/groups?kind=unknown").json()
    assert r["groups"][0]["n"] == 2 and "available" in r and "progress" in r
    assert client.get("/api/faces/groups?kind=nope").status_code == 400
    con = vdb.connect(); con.execute("DELETE FROM ai_face_embeddings"); con.commit(); con.close()
    assert facevecs.groups("unknown")["groups"] == [] and len(facevecs.groups("unknown")["missing"]) >= 3


def test_near_duplicate_sets(client, lib):
    ids = lib["ids"][:5]
    con = vdb.connect()
    try:
        con.execute("DELETE FROM ai_similar_photos")
        con.executemany("INSERT INTO ai_similar_photos (a, b, score) VALUES (?,?,?)", [(ids[0], ids[1], 0.98), (ids[1], ids[2], 0.95), (ids[3], ids[4], 0.91)])
        con.commit()
    finally:
        con.close()
    from app.data import state
    state.set_setting("near_dups_gen", "x"); state.set_setting("near_dups_level", "normal")
    g = neardups.groups()
    assert [x["n"] for x in g] == [3, 2] and g[0]["score"] == 0.98
    assert {x["id"] for x in g[0]["items"]} == set(ids[:3]) and all("album" in x and "name" in x for x in g[0]["items"])
    r = client.get("/api/neardups").json()
    assert len(r["groups"]) == 2 and r["levels"]["normal"] == 0.94 and r["current"] is False, "computed for another index generation"
    assert client.post("/api/neardups", json={"level": "weird"}).status_code == 400
    assert client.post("/api/neardups", json={"level": "normal"}).status_code == 409, "content search is off in the test environment"
    con = vdb.connect(); con.execute("DELETE FROM ai_similar_photos"); con.commit(); con.close()
    state.set_setting("near_dups_gen", "")
    assert client.get("/api/neardups").json()["groups"] == []
