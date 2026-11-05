"""Suggestions the user said no to ("Not them"): a (kind, photo, person) that is never suggested again.
Kinds: jersey, clothing. Kept in the viewer database only."""
import time

from app.data import vdb
from app.photos import history

KINDS = ("jersey", "clothing")


def ids(kind: str) -> set:
    """{(photo id, tag id)} dismissed for this kind."""
    with vdb.reader() as con:
        return {(r[0], r[1]) for r in con.execute("SELECT photo_id, tag_id FROM pix_dismissed_suggestions WHERE kind = ?", (kind,))}


def set_dismissed(kind: str, pairs: list, on: bool = True) -> int:
    """pairs: [(photo id, tag id)]; on: false takes a dismissal back."""
    if kind not in KINDS:
        raise ValueError("Unknown suggestion kind")
    pairs = [(int(p), int(t)) for p, t in pairs]
    with vdb.writer() as con:
        if on:
            con.executemany("INSERT OR REPLACE INTO pix_dismissed_suggestions (kind, photo_id, tag_id, user_name, created_at) VALUES (?,?,?,?,?)",
                            [(kind, p, t, history.actor(), int(time.time())) for p, t in pairs])
        else:
            con.executemany("DELETE FROM pix_dismissed_suggestions WHERE kind = ? AND photo_id = ? AND tag_id = ?", [(kind, p, t) for p, t in pairs])
    return len(pairs)
