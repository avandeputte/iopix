"""Tag covers (one query and one text embedding per tag) are computed again only when what they depend
on changed: the tags' photo counts or the search index."""
import sqlite3

from app import core


def _index(path, counts: dict):
    """A tiny index with the tables _compute_tag_covers reads: {tag id: [photo ids]}."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, name TEXT, person INTEGER, n_all INTEGER);
        CREATE TABLE IF NOT EXISTS photo_tags (photo_id INTEGER, tag_id INTEGER);
        CREATE TABLE IF NOT EXISTS tag_closure (tag INTEGER, anc INTEGER);
        CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY, video INTEGER, pick INTEGER);
        DELETE FROM tags; DELETE FROM photo_tags; DELETE FROM tag_closure; DELETE FROM photos;
    """)
    for tag, photos in counts.items():
        con.execute("INSERT INTO tags VALUES (?,?,0,?)", (tag, f"tag {tag}", len(photos)))
        con.execute("INSERT INTO tag_closure VALUES (?,?)", (tag, tag))
        for p in photos:
            con.execute("INSERT OR IGNORE INTO photos VALUES (?,0,0)", (p,))
            con.execute("INSERT INTO photo_tags VALUES (?,?)", (p, tag))
    con.commit()
    con.close()


def test_tag_covers_are_skipped_when_nothing_they_depend_on_changed(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    _index(db, {1: [10, 11], 2: [12]})

    def open_db():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        return con
    calls = []

    class Sem:
        key = "clip:1"
    monkeypatch.setattr(core, "db", open_db)
    monkeypatch.setattr(core, "semantic_enabled", lambda: True)
    monkeypatch.setattr(core, "semantic", Sem())
    monkeypatch.setattr(core.concepts, "representative", lambda ids, text=None: calls.append((tuple(ids), text)) or ids[0])
    monkeypatch.setattr(core, "_tag_covers", {})
    monkeypatch.setattr(core, "_tag_covers_from", {"sig": None})
    monkeypatch.setitem(core.state, "source", "v1")

    core._compute_tag_covers("v1")
    assert core._tag_covers == {1: 10, 2: 12} and len(calls) == 2
    core._compute_tag_covers("v1")
    assert len(calls) == 2, "the same tags with the same counts and the same index: nothing recomputed"
    _index(db, {1: [10, 11, 13], 2: [12]})  # a photo tagged
    core._compute_tag_covers("v1")
    assert len(calls) == 4 and core._tag_covers == {1: 10, 2: 12}
    core.semantic.key = "clip:2"  # more embeddings saved: the covers may be better now
    core._compute_tag_covers("v1")
    assert len(calls) == 6
    _index(db, {1: [10, 11, 13], 2: [12, 14]})
    monkeypatch.setitem(core.state, "source", "v2")  # the library changed under the computation: it stops and records nothing
    core._compute_tag_covers("v1")
    assert len(calls) == 6
    monkeypatch.setitem(core.state, "source", "v1")
    core._compute_tag_covers("v1")
    assert len(calls) == 8, "an interrupted run leaves the next one to do the work"
