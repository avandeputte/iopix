"""The viewer database bridge: the same code runs on SQLite and on MySQL; data copies across with
ids intact, in both directions; the SQLite snapshot is the portable backup form. The MySQL half
runs only when VIEWER_TEST_MYSQL=host:port/database:user:password points at a server where that
database may be created and dropped."""
import os
import sqlite3

import pytest

from app.ai import analysis
from app.data import state, vdb
from app import users


def _mysql_cfg():
    v = os.environ.get("VIEWER_TEST_MYSQL")
    if not v:
        return None
    hostport, _, rest = v.partition("/")
    database, user, password = rest.split(":", 2)
    host, _, port = hostport.partition(":")
    return {"type": "mysql", "host": host, "port": int(port or 3306), "database": database, "user": user, "password": password}


def test_sqlite_snapshot_and_copy_keep_every_row(client, tmp_path, fresh):
    users.create("alex", "correct horse", "admin")
    state.set_setting("zz_probe", "value with ? and %s and `key`")
    state.set_favorites([(1, "h1"), (2, "h2")], True)
    aid = state.create_album("Trip"); state.set_album_items(aid, [(1, "h1")], True)
    with analysis._writer() as con:
        con.execute("INSERT OR REPLACE INTO ai_quality VALUES (?,?,?,?,?)", (1, 0.5, 0.4, 0, 0))
        con.execute("INSERT OR REPLACE INTO ai_scans VALUES (?,?,?,?)", (1, "quality", "s1", None))
    snap = tmp_path / "snap.db"
    vdb.snapshot_sqlite(snap)
    con = sqlite3.connect(snap)
    assert con.execute("SELECT value FROM app_settings WHERE key = 'zz_probe'").fetchone()[0] == "value with ? and %s and `key`"
    assert con.execute("SELECT count(*) FROM favorite_photos").fetchone()[0] >= 2 and con.execute("SELECT name FROM user_accounts").fetchone()[0] == "alex"
    assert con.execute("SELECT sharp FROM ai_quality WHERE photo_id = 1").fetchone()[0] == 0.5
    con.close()
    # copy_all into another SQLite file, then read it through the bridge
    dst_path = tmp_path / "copy.db"
    src = vdb.connect()
    dst = vdb.VConn(vdb._sqlite_raw(dst_path, 5), False, True)
    try:
        counts = vdb.copy_all(src, dst, drop=True)
        assert counts["user_accounts"] >= 1 and counts["favorite_photos"] >= 2 and counts["viewer_albums"] >= 1
        r = dst.execute("SELECT id, name FROM viewer_albums WHERE id = ?", (aid,)).fetchone()
        assert r["name"] == "Trip" and r[1] == "Trip", "rows answer to names and indexes"
    finally:
        src.close(); dst.close()


def test_switch_refuses_sqlite_to_sqlite(client):
    if vdb.is_mysql():
        pytest.skip("the suite runs with the viewer on MySQL; switching to SQLite is then a real switch")
    with pytest.raises(ValueError):
        vdb.switch({"type": "sqlite"}, copy=False)


def test_mysql_round_trip(client, tmp_path, fresh, monkeypatch):
    cfg = _mysql_cfg()
    if not cfg:
        pytest.skip("VIEWER_TEST_MYSQL not set")
    import pymysql
    admin = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"], autocommit=True)
    admin.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` CHARACTER SET utf8mb4")
    try:
        # seed the SQLite side, then copy to MySQL (drop + recreate) and run the modules against MySQL
        con = state.connect()  # leftovers from earlier tests in the session
        for t in ("favorite_photos", "viewer_album_photos", "viewer_albums"):
            con.execute(f"DELETE FROM {t}")
        con.commit(); con.close()
        users.create("alex", "correct horse", "admin"); users.create("vi", "", "viewer")
        state.set_setting("zz_probe", "héllo ? %s"); state.set_favorites([(1, "h1")], True)
        aid = state.create_album("Trip"); state.set_album_items(aid, [(1, "h1")], True)
        with analysis._writer() as con:
            con.execute("INSERT OR REPLACE INTO ai_quality VALUES (?,?,?,?,?)", (1, 0.5, 0.4, 0, 0))
            con.execute("INSERT OR REPLACE INTO ai_scans VALUES (?,?,?,?)", (1, "quality", "2026-10-15T00:00:00.000", None))
            con.execute("INSERT OR REPLACE INTO ai_descriptions VALUES (?,?)", (1, "a dog on a beach"))
        t = vdb.test_connection(cfg)
        assert t["ok"] and "MariaDB" in t["version"] or "ok" in str(t)
        src = vdb.connect(); dst = vdb.connect(cfg=cfg)
        try:
            counts = vdb.copy_all(src, dst, drop=True)
        finally:
            src.close(); dst.close()
        assert counts["user_accounts"] == 2 and counts["favorite_photos"] >= 1 and counts["ai_descriptions"] >= 1
        monkeypatch.setattr(vdb, "settings", lambda: dict(cfg))
        monkeypatch.setattr(vdb, "error", None)
        assert vdb.is_mysql()
        # the modules, unchanged, on MySQL
        state._settings = (-1, {})
        assert state.get_setting("zz_probe") == "héllo ? %s"
        state.set_setting("zz_probe", "changed"); state._settings = (-1, {})
        assert state.get_setting("zz_probe") == "changed"
        me = users.get(next(u["id"] for u in users.list_users() if u["name"] == "alex"))
        assert me["role"] == "admin" and me["email"] == ""
        assert users.authenticate("alex", "correct horse") and not users.authenticate("alex", "wrong")
        assert users.authenticate("vi", "") , "a passwordless viewer signs in with one click"
        u = users.create("new", "password-1", "editor", "New@Example.com")
        assert u["email"] == "new@example.com" and users.get(u["id"])["name"] == "new"
        assert any(a["name"] == "Trip" and a["n"] == 1 for a in state.summary()["albums"])
        assert 1 in state.favorite_ids() and state.albums_of(1) == [aid] and state.album_id_by_name("trip") == aid
        # private albums on MySQL: seen by their owner only (the GROUP BY must satisfy ONLY_FULL_GROUP_BY too)
        priv = state.create_album("Secret", me["id"], shared=False); state.set_album_items(priv, [(1, "h1")], True)
        assert "Secret" not in {a["name"] for a in state.summary(None)["albums"]}
        assert {a["name"] for a in state.summary(me)["albums"]} >= {"Trip", "Secret"}
        assert state.album_item_ids([priv]) == [] and state.album(priv)["is_shared"] is False
        tok = state.viewer.set(me)
        try:
            assert state.album_item_ids([priv]) == [1] and sorted(state.albums_of(1)) == sorted([aid, priv])
        finally:
            state.viewer.reset(tok)
        # an older MySQL table without the two columns is migrated in place, its albums shared
        c = vdb.connect()
        try:
            c.execute("CREATE TABLE zz_old_albums AS SELECT id, name, created FROM viewer_albums")
            c.execute("DROP TABLE viewer_albums"); c.execute("RENAME TABLE zz_old_albums TO viewer_albums")
            c.commit()
            vdb.ensure_schema(c)
            assert {"owner_id", "is_shared"} <= set(vdb.columns(c, "viewer_albums"))
            assert {r[0] for r in c.execute("SELECT is_shared FROM viewer_albums")} == {1}
        finally:
            c.close()
        with analysis._writer() as con:
            con.execute("INSERT OR REPLACE INTO ai_quality VALUES (?,?,?,?,?)", (2, 0.1, 0.9, 0, 0))
            con.execute("INSERT OR REPLACE INTO ai_scans VALUES (?,?,?,?)", (2, "quality", "2026-10-15T00:00:00.000", None))
        res = analysis.results()
        assert res[1]["sharp"] == 0.5 and res[2]["bright"] == 0.9 and res[1]["caption_ai"] == "a dog on a beach"
        con = vdb.connect()
        try:
            assert con.execute("SELECT stamp FROM ai_scans WHERE photo_id = ? AND job = 'quality'", (1,)).fetchone()[0] == "2026-10-15T00:00:00.000", "text stamps stay text on MySQL"
        finally:
            con.close()
        # and back to SQLite: the portable snapshot
        snap = tmp_path / "from-mysql.db"
        vdb.snapshot_sqlite(snap)
        c2 = sqlite3.connect(snap)
        assert c2.execute("SELECT count(*) FROM user_accounts").fetchone()[0] == 3 and c2.execute("SELECT value FROM app_settings WHERE key = 'zz_probe'").fetchone()[0] == "changed"
        c2.close()
    finally:
        admin.cursor().execute(f"DROP DATABASE IF EXISTS `{cfg['database']}`")
        admin.close()


def test_v124_undo_column_is_renamed_on_sqlite(tmp_path, monkeypatch):
    """v1.24 called the change log column "undo", a reserved word on MariaDB/MySQL (the table could not be
    created there at all). SQLite files from then are migrated in place; the row data is kept."""
    db = tmp_path / "viewer.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE photo_history (id INTEGER PRIMARY KEY AUTOINCREMENT, photo_id INTEGER, ts INTEGER, user_name TEXT, action TEXT, summary TEXT, details TEXT, undo TEXT, sidecar_copy TEXT, path TEXT)")
    con.execute("INSERT INTO photo_history (photo_id, ts, action, summary, undo) VALUES (1, 2, 'edit', 's', '{\"rating\": 3}')")
    con.commit(); con.close()
    monkeypatch.setattr(vdb, "SQLITE_FILE", db)
    monkeypatch.setattr(vdb, "settings", lambda: {"type": "sqlite"})
    vdb.ensure_schema()
    c = vdb.connect()
    try:
        assert "undo_values" in vdb.columns(c, "photo_history") and "undo" not in vdb.columns(c, "photo_history")
        assert c.execute("SELECT undo_values FROM photo_history WHERE photo_id = 1").fetchone()[0] == '{"rating": 3}'
    finally:
        c.close()
    assert "undo" not in vdb.TABLES["photo_history"][0] and " undo " not in vdb.TABLES["photo_history"][2]


def test_every_declared_column_is_in_both_create_statements():
    for name, (cols, sq, my) in vdb.TABLES.items():
        assert list(vdb.declared_columns(sq)) == cols, name
        assert list(vdb.declared_columns(my)) == cols, name


def test_columns_added_since_a_database_was_created_are_added_on_open(tmp_path, monkeypatch):
    """v1.63.0 added session_gen to user_accounts but only SQLite databases got it: the MySQL one failed at the
    first signed-in request. Every column TABLES declares is now added to a table that lacks it, on both engines."""
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE user_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL COLLATE NOCASE, hash TEXT NOT NULL, "
                "role TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}', created INTEGER, last_login INTEGER, person INTEGER, email TEXT)")
    raw.execute("INSERT INTO user_accounts (name, hash, role) VALUES ('old', 'h', 'admin')")
    raw.execute("CREATE TABLE viewer_albums (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created INTEGER)")
    raw.commit(); raw.close()
    vc = vdb.VConn(vdb._sqlite_raw(db, 5), False, True)
    vdb.ensure_schema(vc)
    assert "session_gen" in vdb.columns(vc, "user_accounts") and "is_shared" in vdb.columns(vc, "viewer_albums")
    assert tuple(vc.execute("SELECT name, session_gen FROM user_accounts").fetchone()) == ("old", 0)
    assert vdb.add_missing_columns(vc) == []  # a second open adds nothing
    vc.raw.close()
