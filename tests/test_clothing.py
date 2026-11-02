"""Clothing matches within an event (app/ai/clothing.py): an unnamed person box takes the name of the named box it looks
most like, only when the match is clear."""
import pytest

from app.ai import clothing

np = pytest.importorskip("numpy")


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def test_match_event_names_a_clear_match_only():
    red = _unit([1, 0.1, 0]); blue = _unit([0, 1, 0.1]); other = _unit([0.6, 0.6, 0.3])
    faces = {1: [(10, 0.2, 0.2)], 2: [(11, 0.2, 0.2)]}  # Sam (red) named in photo 1, Lee (blue) in photo 2
    boxes = [(1, (0.1, 0.1, 0.2, 0.6), red), (2, (0.1, 0.1, 0.2, 0.6), blue),
             (3, (0.5, 0.1, 0.2, 0.6), _unit(red + 0.05 * blue)),   # clearly red: Sam
             (4, (0.5, 0.1, 0.2, 0.6), other),                      # between the two: nobody
             (1, (0.6, 0.1, 0.2, 0.6), _unit(red + 0.02 * blue))]   # red, but in Sam's own photo: never
    out = clothing.match_event(boxes, faces, {})
    assert [(s["id"], s["tag"]) for s in out] == [(3, 10)]
    assert out[0]["n"] == 1 and out[0]["score"] > 0.9 and out[0]["lead"] > clothing.MARGIN
    # already tagged on that photo: nothing to suggest
    assert clothing.match_event(boxes, faces, {3: {10}}) == []
    # a single named person in the event: the high bar applies
    faces1 = {1: [(10, 0.2, 0.2)]}
    one = clothing.match_event([boxes[0], (3, (0.5, 0.1, 0.2, 0.6), _unit(red + 0.8 * blue))], faces1, {})
    assert one == []
    one = clothing.match_event([boxes[0], (3, (0.5, 0.1, 0.2, 0.6), _unit(red + 0.02 * blue))], faces1, {})
    assert [(s["id"], s["tag"]) for s in one] == [(3, 10)]


def test_tag_in_box_wants_the_face_in_the_upper_part():
    box = (0.1, 0.1, 0.2, 0.6)
    assert clothing._tag_in_box([(10, 0.2, 0.2)], box) == 10
    assert clothing._tag_in_box([(10, 0.2, 0.6)], box) is None   # a face low in the box is someone in front
    assert clothing._tag_in_box([(10, 0.2, 0.2), (11, 0.25, 0.2)], box) is None  # two faces: ambiguous


def test_endpoint(client, lib):
    d = client.get("/api/clothing").json()
    assert {"total", "items", "ready", "coverage"} <= set(d)
