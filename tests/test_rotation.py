"""Wrongly rotated photos (app/ai/rotation.py): the classifier learns four turns from embeddings, the four-turn vote
confirms a finding, and dismissed findings stay out until the file changes."""
import pytest

from app.ai import rotation
from app.data import vdb

np = pytest.importorskip("numpy")


def _fake_embeddings(n: int, seed: int = 0):
    """Four classes that live in four directions of a 32-d space, with noise: enough for a softmax to learn."""
    centres = np.random.default_rng(42).normal(size=(4, 32))  # the same four directions for every call
    rng = np.random.default_rng(seed)
    X, Y = [], []
    for k in range(4):
        pts = centres[k] + 0.8 * rng.normal(size=(n, 32))
        X += list(pts / np.linalg.norm(pts, axis=1, keepdims=True))
        Y += [k] * n
    return np.array(X, np.float32), np.array(Y)


def test_train_predict_and_vote():
    X, Y = _fake_embeddings(60)
    clf = rotation.train(X, Y, epochs=300)
    Xt, Yt = _fake_embeddings(20, seed=1)
    p = rotation.predict(clf, Xt)
    assert (p.argmax(1) == Yt).mean() > 0.9
    # the four turns of an upright photo read as 0, 1, 2, 3: the vote says upright
    turned = [np.eye(4)[k] * 0.9 + 0.025 for k in range(4)]
    assert rotation.vote(turned) == (0, pytest.approx(1.0, abs=0.01))
    # a file turned clockwise once: its turns read as 1, 2, 3, 0
    assert rotation.vote([np.eye(4)[(1 + k) % 4] * 0.9 + 0.025 for k in range(4)])[0] == 1
    # readings that disagree are not sure of anything
    t, sure = rotation.vote([np.eye(4)[0] * 0.9 + 0.025, np.eye(4)[0] * 0.9 + 0.025, np.eye(4)[1] * 0.9 + 0.025, np.eye(4)[3] * 0.9 + 0.025])
    assert sure < rotation.CONFIRM_MIN


def test_turns_and_ops():
    from PIL import Image
    im = Image.new("RGB", (30, 20))
    sizes = [t.size for t in rotation.turns(im)]
    assert sizes == [(30, 20), (20, 30), (30, 20), (20, 30)]
    assert rotation.TURN_OP == {1: "left", 2: "180", 3: "right"}


def test_findings_and_dismiss(lib):
    with vdb.writer() as con:
        con.execute("DELETE FROM ai_rotation")
        con.execute("DELETE FROM quality_accepted WHERE check_name = 'rotation'")
        con.executemany("INSERT INTO ai_rotation (photo_id, turn, score, stamp) VALUES (?,?,?,?)", [(1, 1, 0.99, "s1"), (2, 2, 0.95, "s2")])
    try:
        out = rotation.findings()
        assert [(i["id"], i["op"]) for i in out["items"]] == [(1, "left"), (2, "180")] and out["dismissed"] == 0
        assert out["items"][0]["text"].startswith("Looks on its side")
        assert rotation.dismiss([2]) == 1
        out = rotation.findings()
        assert [i["id"] for i in out["items"]] == [1] and out["dismissed"] == 1
        with vdb.writer() as con:  # the file changed: the finding is back
            con.execute("UPDATE ai_rotation SET stamp = 's2b' WHERE photo_id = 2")
        assert [i["id"] for i in rotation.findings()["items"]] == [1, 2]
        assert rotation.dismiss([2], on=False) == 1
        assert [i["id"] for i in rotation.findings([2])["items"]] == [2]
    finally:
        with vdb.writer() as con:
            con.execute("DELETE FROM ai_rotation")
            con.execute("DELETE FROM quality_accepted WHERE check_name = 'rotation'")


def test_endpoint(client, lib):
    d = client.get("/api/rotation").json()
    assert {"total", "items", "dismissed", "ready", "coverage"} <= set(d)
