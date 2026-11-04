"""The viewer database (MariaDB-only since v1.113): the schema lands in an empty database, data copies to
another database with ids intact, a viewer.db from before v1.113 is imported once, the dump is the backup
form, and the viewer waits for a database instead of starting on a file."""
import json
import sqlite3

import pytest

import mariadb
from app.ai import analysis
from app.data import state, vdb
from app import users


@pytest.fixture
def other_db():
    """A second, empty database on the test server, dropped afterwards."""
    cfg = mariadb.fresh_database("iopix_test_other")
    yield cfg
    import pymysql
    c = mariadb.server()
    con = pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"], autocommit=True)
    con.cursor().execute("DROP DATABASE IF EXISTS `iopix_test_other`")
    con.close()


def _seed():
    users.create("alex", "correct horse", "admin")
    state.set_setting("zz_probe", "value with ? and %s and `key`")
    state.set_favorites([(1, "h1"), (2, "h2")], True)
    aid = state.create_album("Trip"); state.set_album_items(aid, [(1, "h1")], True)
    with vdb.writer() as con:
        con.execute("INSERT OR REPLACE INTO ai_quality VALUES (?,?,?,?,?)", (1, 0.5, 0.4, 0, 0))
        con.execute("INSERT OR REPLACE INTO ai_scans VALUES (?,?,?,?)", (1, "quality", "2026-10-15T00:00:00.000", None))
        con.execute("INSERT OR REPLACE INTO ai_descriptions VALUES (?,?)", (1, "a dog on a beach"))
    return aid


def test_a_fresh_empty_database_gets_every_table(other_db):
    """setup.sh and the set-up page point a new install at an empty database: the first start creates the tables."""
    con = vdb.connect(cfg=other_db)
    try:
        assert vdb.columns(con, "ai_quality") == []
        vdb.ensure_schema(con)
        assert set(vdb.TABLES) <= vdb.table_names(con)
        assert vdb.add_missing_columns(con) == [], "a second open adds nothing"
    finally:
        con.close()


def test_copy_to_another_database_keeps_every_row(client, fresh, other_db):
    aid = _seed()
    t = vdb.test_connection(other_db)
    assert t["ok"] and t["tables"] == {} and t["existing"] == []
    src, dst = vdb.connect(), vdb.connect(cfg=other_db)
    try:
        counts = vdb.copy_all(src, dst, drop=True)
        assert counts["favorite_photos"] >= 2 and counts["viewer_albums"] >= 1  # (accounts live in the Shared ioSuite database)
        r = dst.execute("SELECT id, name FROM viewer_albums WHERE id = ?", (aid,)).fetchone()
        assert r["name"] == "Trip" and r[1] == "Trip", "rows answer to names and indexes"
        assert dst.execute("SELECT value FROM app_settings WHERE key = 'zz_probe'").fetchone()[0] == "value with ? and %s and `key`"
        assert dst.execute("SELECT stamp FROM ai_scans WHERE photo_id = 1 AND job = 'quality'").fetchone()[0] == "2026-10-15T00:00:00.000", "text stamps stay text"
    finally:
        src.close(); dst.close()
    t = vdb.test_connection(other_db)
    assert t["tables"]["favorite_photos"] >= 2 and "favorite_photos" in t["existing"]
    res = analysis.results()
    assert res[1]["sharp"] == 0.5 and res[1]["caption_ai"] == "a dog on a beach"


def test_private_albums_and_accounts_on_the_server(client, fresh):
    """The modules' queries on MariaDB (GROUP BY under ONLY_FULL_GROUP_BY, COLLATE NOCASE dropped, the `key` column)."""
    with vdb.writer() as con:  # leftovers from the tests before
        for t in ("favorite_photos", "viewer_album_photos", "viewer_albums"):
            con.execute(f"DELETE FROM {t}")
    users.create("alex", "correct horse", "admin"); users.create("vi", "", "viewer")
    state._settings = (-1, {})
    state.set_setting("zz_probe", "héllo ? %s"); state._settings = (-1, {})
    assert state.get_setting("zz_probe") == "héllo ? %s"
    me = users.get(next(u["id"] for u in users.list_users() if u["name"] == "alex"))
    assert me["role"] == "admin" and users.authenticate("alex", "correct horse") and not users.authenticate("alex", "wrong")
    assert users.authenticate("vi", ""), "a passwordless viewer signs in with one click"
    u = users.create("new", "password-1", "editor", "New@Example.com")
    assert u["email"] == "new@example.com" and users.get(u["id"])["name"] == "new"
    aid = state.create_album("Trip"); state.set_album_items(aid, [(1, "h1")], True)
    priv = state.create_album("Secret", me["id"], shared=False); state.set_album_items(priv, [(1, "h1")], True)
    assert "Secret" not in {a["name"] for a in state.summary(None)["albums"]}
    assert {a["name"] for a in state.summary(me)["albums"]} >= {"Trip", "Secret"}
    assert state.album_item_ids([priv]) == [] and state.album(priv)["is_shared"] is False
    tok = state.viewer.set(me)
    try:
        assert state.album_item_ids([priv]) == [1] and sorted(state.albums_of(1)) == sorted([aid, priv])
    finally:
        state.viewer.reset(tok)


def test_an_older_table_without_the_album_columns_is_migrated_in_place(client):
    c = vdb.connect()
    try:
        c.execute("CREATE TABLE zz_old_albums AS SELECT id, name, created FROM viewer_albums")
        c.execute("DROP TABLE viewer_albums"); c.execute("RENAME TABLE zz_old_albums TO viewer_albums")
        c.commit()
        vdb.ensure_schema(c)
        assert {"owner_id", "is_shared"} <= set(vdb.columns(c, "viewer_albums"))
        assert {r[0] for r in c.execute("SELECT is_shared FROM viewer_albums")} <= {1}
    finally:
        c.close()


def _legacy_file(path):
    """A viewer.db as an install from before v1.113 left it: a few tables in the SQLite spelling, with rows."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT); INSERT INTO app_settings VALUES ('theme', 'dark'), ('mcp_admin_token', 'secret');
        CREATE TABLE user_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL COLLATE NOCASE, hash TEXT NOT NULL, role TEXT NOT NULL,
            settings TEXT NOT NULL DEFAULT '{}', created INTEGER, last_login INTEGER, person INTEGER, email TEXT, session_gen INTEGER DEFAULT 0, listed INTEGER NOT NULL DEFAULT 1);
        INSERT INTO user_accounts (name, hash, role) VALUES ('old', 'h', 'admin');
        CREATE TABLE favorite_photos (photo_id INTEGER PRIMARY KEY, hash TEXT, added INTEGER); INSERT INTO favorite_photos VALUES (5, 'h5', 1), (6, 'h6', 2);
        CREATE TABLE photo_history (id INTEGER PRIMARY KEY AUTOINCREMENT, photo_id INTEGER, ts INTEGER, user_name TEXT, action TEXT, summary TEXT, details TEXT, undo TEXT, sidecar_copy TEXT, path TEXT);
        INSERT INTO photo_history (photo_id, ts, action, summary, undo) VALUES (1, 2, 'edit', 's', '{"rating": 3}');
        CREATE TABLE tags (id INTEGER PRIMARY KEY, pid INTEGER, name TEXT, icon INTEGER, iconkde TEXT, lft INTEGER, rgt INTEGER);
        INSERT INTO tags VALUES (0, -1, '_Digikam_root_tag_', NULL, NULL, 1, 2);
    """); con.commit(); con.close()


def test_a_viewer_db_from_before_is_imported_once(tmp_path, other_db, monkeypatch):
    """An upgraded install's first switch copies every row out of its viewer.db (the v1.24 column name included,
    digiKam's root tag with id 0 kept), then renames the file so it is never read again."""
    legacy = tmp_path / "viewer.db"
    _legacy_file(legacy)
    monkeypatch.setattr(vdb, "LEGACY_SQLITE", legacy)
    monkeypatch.setattr(vdb, "CONF", tmp_path / "viewer-db.json")
    monkeypatch.setattr(vdb, "_cfg", None)
    monkeypatch.setattr(vdb.config, "viewer_db_env", lambda k: None)  # the suite's VIEWER_DB_* must not count as "configured"
    assert not vdb.configured() and vdb.public_settings()["legacy_sqlite"] == str(legacy)
    with pytest.raises(ValueError):
        vdb.switch({"host": other_db["host"]}, copy=True)  # a database and a user are needed too
    r = vdb.switch(other_db, copy=True, drop=True)
    assert r["imported"] == str(legacy) and r["copied"]["favorite_photos"] == 2 and r["copied"]["tags"] == 1 and "user_accounts" not in r["copied"]
    assert not legacy.exists() and (tmp_path / "viewer.db.imported").exists()
    assert json.loads((tmp_path / "viewer-db.json").read_text())["database"] == other_db["database"]
    con = vdb.connect(cfg=other_db)
    try:
        assert con.execute("SELECT undo_values FROM photo_history WHERE photo_id = 1").fetchone()[0] == '{"rating": 3}'
        assert con.execute("SELECT id FROM tags").fetchone()[0] == 0, "digiKam's root tag keeps id 0 (NO_AUTO_VALUE_ON_ZERO)"
        assert con.execute("SELECT value FROM app_settings WHERE key = 'theme'").fetchone()[0] == "dark"
        assert set(vdb.TABLES) <= vdb.table_names(con), "the tables the file never had are created too"
    finally:
        con.close()
    assert vdb.settings()["database"] == other_db["database"]


def test_switch_refuses_the_database_in_use(client):
    with pytest.raises(ValueError):
        vdb.switch(vdb.settings(), copy=False)


def test_dump_is_the_backup_form_and_leaves_the_token_out(client, fresh, tmp_path):
    if not vdb.dump_tool():
        pytest.skip("no mysqldump/mariadb-dump on this machine")
    _seed()
    before = state.get_setting("mcp_admin_token", "")
    state.set_setting("mcp_admin_token", "very-secret-token-of-24-chars")
    try:
        out = tmp_path / "viewer.sql"
        vdb.dump(out)
        text = out.read_text(errors="replace")
        assert "CREATE TABLE `favorite_photos`" in text and "'zz_probe'" in text and "very-secret" not in text and "mcp_admin_token" not in text
        vdb.dump(tmp_path / "all.sql", skip_token=False)
        assert "very-secret" in (tmp_path / "all.sql").read_text(errors="replace")
    finally:
        state.set_setting("mcp_admin_token", before)


def test_without_a_configuration_the_viewer_waits(monkeypatch):
    monkeypatch.setattr(vdb, "CONF", vdb.config.DATA_DIR / "nowhere.json")
    monkeypatch.setattr(vdb, "_cfg", None)
    monkeypatch.setattr(vdb.config, "viewer_db_env", lambda k: None)
    monkeypatch.setattr(vdb, "error", None)
    assert not vdb.configured()
    vdb.init()  # never raises
    assert vdb.error and not vdb.ready() and vdb.describe() == "no database configured"
    with pytest.raises(RuntimeError):
        vdb.connect()


def test_an_unreachable_server_is_recorded_not_fatal(monkeypatch):
    monkeypatch.setattr(vdb, "settings", lambda: {"type": "mysql", "host": "127.0.0.1", "port": 1, "database": "x", "user": "u", "password": ""})
    monkeypatch.setattr(vdb, "error", None)
    vdb.init()
    assert vdb.error and vdb.configured() and not vdb.ready() and "unavailable" in vdb.describe()


def test_every_declared_column_is_in_the_create_statement():
    for name, (cols, ddl) in vdb.TABLES.items():
        assert list(vdb.declared_columns(ddl)) == cols, name
    assert "undo" not in vdb.TABLES["photo_history"][0] and " undo " not in vdb.TABLES["photo_history"][1]
