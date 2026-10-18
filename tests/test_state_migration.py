"""Installs from before v1.8 kept state.db and analysis.db; the first start folds both into one
viewer.db with every row, and parks the old files instead of deleting them."""
import sqlite3

from app.data import state


def test_two_files_become_one(tmp_path, monkeypatch):
    old_state, old_an = tmp_path / "state.db", tmp_path / "analysis.db"
    con = sqlite3.connect(old_state)
    con.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT); INSERT INTO settings VALUES ('theme', 'dark');
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, hash TEXT, role TEXT); INSERT INTO users (name, hash, role) VALUES ('alex', 'x', 'admin');
        CREATE TABLE favorites (photo_id INTEGER PRIMARY KEY, hash TEXT, added INTEGER); INSERT INTO favorites VALUES (7, 'h', 1);
    """); con.commit(); con.close()
    con = sqlite3.connect(old_an)
    con.executescript("""
        CREATE TABLE quality (photo_id INTEGER PRIMARY KEY, stamp TEXT, sharp REAL, bright REAL, clip_lo REAL, clip_hi REAL);
        INSERT INTO quality VALUES (7, 's', 0.5, 0.4, 0, 0), (8, 's', 0.1, 0.9, 0, 0);
        CREATE TABLE objects (photo_id INTEGER, cls TEXT, score REAL, x REAL, y REAL, w REAL, h REAL); CREATE INDEX objects_photo ON objects (photo_id);
        INSERT INTO objects VALUES (7, 'dog', 0.9, 0, 0, 1, 1);
        CREATE TABLE captions (photo_id INTEGER PRIMARY KEY, stamp TEXT, text TEXT); INSERT INTO captions VALUES (7, 's', 'a dog');
    """); con.commit(); con.close()
    new = tmp_path / "viewer.db"
    monkeypatch.setattr(state, "STATE_DB", new)
    monkeypatch.setattr(state, "LEGACY", (old_state, old_an))
    monkeypatch.setattr(state.config, "DATA_DIR", tmp_path)
    park = state.migrate_legacy()
    assert park and park.parent.name == "db-backups" and (park / "state.db").exists() and (park / "analysis.db").exists()
    assert not old_state.exists() and not old_an.exists() and new.exists()
    from app.data import vdb
    vc = vdb.VConn(sqlite3.connect(new), False, True)
    vdb.rename_old_tables(vc); vc.commit(); vc.raw.close()  # what vdb.init does next at start
    con = sqlite3.connect(new)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"app_settings", "user_accounts", "favorite_photos", "ai_quality", "ai_objects", "ai_descriptions"} <= tables, "old names are renamed in place"
    assert con.execute("SELECT value FROM app_settings WHERE key = 'theme'").fetchone()[0] == "dark"
    assert con.execute("SELECT count(*) FROM ai_quality").fetchone()[0] == 2 and con.execute("SELECT text FROM ai_descriptions").fetchone()[0] == "a dog"
    assert not con.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'objects_photo'").fetchone(), "the old index name is dropped (recreated under the new one)"
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()
    assert state.migrate_legacy() is None, "runs once"


def test_nothing_to_migrate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DB", tmp_path / "viewer.db")
    monkeypatch.setattr(state, "LEGACY", (tmp_path / "state.db", tmp_path / "analysis.db"))
    assert state.migrate_legacy() is None
