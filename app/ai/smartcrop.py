"""Smart crop: where the subject of a photo is, so a cropped thumbnail keeps it in the frame.

Every square or wide tile (Explore, the cards, the Apple TV app) crops the photo to its own shape, and a
centre crop cuts heads off portraits and loses the child at the edge of a group shot. The focus point of a
photo is the centre of what it is about: the faces digiKam knows (confirmed, unnamed or suggested; not the
ones marked ignored), else the people object detection found, else the centre of the frame. Faces count
more than bodies, and a small group of faces pulls the point to their middle. The point is kept in the
index (column `focus`, "x,y" as fractions of the upright picture), answered with the badges for tiles, and
`/thumb/<id>?fit=square` serves a thumbnail cropped around it. Nothing is written to any photo.
"""


def focus(w: int, h: int, faces: list = (), persons: list = ()) -> tuple:
    """(x, y) as fractions of the picture: faces [(x, y, w, h)] in upright pixels of a w x h picture, persons
    [(x, y, w, h)] as fractions. The weighted centre of the faces (larger faces count more, with a floor so a group
    is not pulled onto the one close face), else of the people, else the middle."""
    if not w or not h:
        return 0.5, 0.5
    pts = []
    for x, y, fw, fh in faces:
        if fw <= 0 or fh <= 0:
            continue
        area = (fw * fh) / float(w * h)
        pts.append(((x + fw / 2) / w, (y + fh / 2) / h, max(area, 0.004) ** 0.5))
    if not pts:
        for x, y, pw, ph in persons:
            if pw <= 0 or ph <= 0:
                continue
            # a person's face sits in the upper part of the box
            pts.append((x + pw / 2, y + 0.3 * ph, max(pw * ph, 0.01) ** 0.5))
    if not pts:
        return 0.5, 0.5
    total = sum(p[2] for p in pts)
    fx = sum(p[0] * p[2] for p in pts) / total
    fy = sum(p[1] * p[2] for p in pts) / total
    return round(min(1.0, max(0.0, fx)), 3), round(min(1.0, max(0.0, fy)), 3)


def crop_box(width: int, height: int, aspect: float, point: tuple) -> tuple:
    """The largest box of the given aspect (w / h) inside a width x height picture, placed so the focus point is as
    near its centre as the edges allow: (left, top, right, bottom) in pixels."""
    fx, fy = point or (0.5, 0.5)
    if width / height > aspect:  # too wide: full height, a slice across
        cw, ch = int(round(height * aspect)), height
    else:
        cw, ch = width, int(round(width / aspect))
    left = int(round(fx * width - cw / 2))
    top = int(round(fy * height - ch / 2))
    left = max(0, min(width - cw, left))
    top = max(0, min(height - ch, top))
    return left, top, left + cw, top + ch


def parse(text) -> tuple:
    """The stored "x,y" back to numbers; the middle when missing or malformed."""
    try:
        x, y = (float(v) for v in str(text).split(","))
        if 0 <= x <= 1 and 0 <= y <= 1:
            return x, y
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.5, 0.5
