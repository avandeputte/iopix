"""Smart crop (app/ai/smartcrop.py): the focus point of a photo and the crop box around it; the badges answer and the
fitted thumbnail carry it."""
from app.ai import smartcrop


def test_focus_point_prefers_faces_then_people_then_the_middle():
    assert smartcrop.focus(1000, 800) == (0.5, 0.5)
    assert smartcrop.focus(1000, 800, faces=[(100, 100, 200, 200)]) == (0.2, 0.25)
    # two faces: between them, the larger one pulling a little more
    x, y = smartcrop.focus(1000, 800, faces=[(0, 0, 100, 100), (800, 600, 200, 200)])
    assert 0.4 < x < 0.9 and 0.3 < y < 0.9
    # a tiny face in a group does not lose its say entirely (the weight has a floor)
    x, _ = smartcrop.focus(4000, 3000, faces=[(0, 0, 20, 20), (3800, 2800, 200, 200)])
    assert x < 0.95
    # people boxes stand in when there is no face: their upper part
    assert smartcrop.focus(1000, 800, persons=[(0.6, 0.2, 0.2, 0.8)]) == (0.7, 0.44)
    assert smartcrop.focus(0, 0, faces=[(1, 1, 1, 1)]) == (0.5, 0.5)


def test_crop_box_keeps_the_point_inside():
    # a wide picture cropped square around a point on the right edge: the box stops at the edge
    assert smartcrop.crop_box(1000, 500, 1.0, (0.95, 0.5)) == (500, 0, 1000, 500)
    assert smartcrop.crop_box(1000, 500, 1.0, (0.5, 0.5)) == (250, 0, 750, 500)
    # a tall picture cropped to 16:9: a slice at the point's height
    left, top, right, bottom = smartcrop.crop_box(600, 1200, 16 / 9, (0.5, 0.2))
    assert (left, right) == (0, 600) and bottom - top == round(600 / (16 / 9)) and top <= 0.2 * 1200 <= bottom
    assert smartcrop.parse("0.25,0.75") == (0.25, 0.75)
    assert smartcrop.parse(None) == (0.5, 0.5) and smartcrop.parse("2,0") == (0.5, 0.5)


def test_badges_and_fitted_thumbnail(client, lib):
    from app.data import index
    con = index.open_index()
    try:
        pid = con.execute("SELECT id FROM photos WHERE video = 0 LIMIT 1").fetchone()[0]
    finally:
        con.close()
    b = client.get(f"/api/badges?ids={pid}").json()
    assert "focus" in b
    r = client.get(f"/thumb/{pid}?fit=square")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(r.content))
    assert abs(im.width - im.height) <= 1
    assert client.get(f"/thumb/{pid}?fit=16x9").status_code == 200
    assert client.get(f"/thumb/{pid}?fit=abc").status_code == 400
    p = client.get("/api/photos?limit=5").json()
    assert "focus" in p
