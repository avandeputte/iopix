"""The viewer database (MariaDB-only since v1.113): the schema lands in an empty database, data copies to
another database with ids intact, a viewer.db from before v1.113 is imported once, the dump is the backup
form, and the viewer waits for a database instead of starting on a file."""

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
        con.execute("INSERT OR REPLACE INTO pix_ai_quality VALUES (?,?,?,?,?)", (1, 0.5, 0.4, 0, 0))
        con.execute("INSERT OR REPLACE INTO pix_ai_scans VALUES (?,?,?,?)", (1, "quality", "2026-10-15T00:00:00.000", None))
        con.execute("INSERT OR REPLACE INTO pix_ai_descriptions VALUES (?,?)", (1, "a dog on a beach"))
    return aid


def test_a_fresh_empty_database_gets_every_table(other_db):
    """setup.sh and the set-up page point a new install at an empty database: the first start creates the tables."""
    con = vdb.connect(cfg=other_db)
    try:
        assert vdb.columns(con, "pix_ai_quality") == []
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
        assert counts["pix_favorite_photos"] >= 2 and counts["pix_albums"] >= 1  # (accounts live in the Shared ioSuite database)
        r = dst.execute("SELECT id, name FROM pix_albums WHERE id = ?", (aid,)).fetchone()
        assert r["name"] == "Trip" and r[1] == "Trip", "rows answer to names and indexes"
        assert dst.execute("SELECT value FROM pix_settings WHERE name = 'zz_probe'").fetchone()[0] == "value with ? and %s and `key`"
        assert dst.execute("SELECT stamp FROM pix_ai_scans WHERE photo_id = 1 AND job = 'quality'").fetchone()[0] == "2026-10-15T00:00:00.000", "text stamps stay text"
    finally:
        src.close(); dst.close()
    t = vdb.test_connection(other_db)
    assert t["tables"]["pix_favorite_photos"] >= 2 and "pix_favorite_photos" in t["existing"]
    res = analysis.results()
    assert res[1]["sharp"] == 0.5 and res[1]["caption_ai"] == "a dog on a beach"


def test_private_albums_and_accounts_on_the_server(client, fresh):
    """The modules' queries on MariaDB (GROUP BY under ONLY_FULL_GROUP_BY, COLLATE NOCASE dropped, the `key` column)."""
    with vdb.writer() as con:  # leftovers from the tests before
        for t in ("pix_favorite_photos", "pix_album_photos", "pix_albums"):
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
    assert state.album_item_ids([priv]) == [] and state.album(priv)["shared"] is False
    tok = state.viewer.set(me)
    try:
        assert state.album_item_ids([priv]) == [1] and sorted(state.albums_of(1)) == sorted([aid, priv])
    finally:
        state.viewer.reset(tok)


def test_a_column_added_to_the_schema_reaches_an_existing_table(client):
    c = vdb.connect()
    try:
        c.execute("ALTER TABLE pix_albums DROP COLUMN shared")
        c.commit()
        vdb.ensure_schema(c)
        assert "shared" in vdb.columns(c, "pix_albums")
    finally:
        c.close()


def test_switch_refuses_the_database_in_use(client):
    with pytest.raises(ValueError):
        vdb.switch(vdb.settings(), copy=False)


def test_dump_is_the_backup_form(client, fresh, tmp_path):
    if not vdb.dump_tool():
        pytest.skip("no mysqldump/mariadb-dump on this machine")
    _seed()
    out = tmp_path / "viewer.sql"
    vdb.dump(out)
    text = out.read_text(errors="replace")
    assert "CREATE TABLE `pix_favorite_photos`" in text and "'zz_probe'" in text


def test_without_a_configuration_the_viewer_waits(monkeypatch):
    monkeypatch.setattr(vdb, "CONF", vdb.config.DATA_DIR / "nowhere.json")
    monkeypatch.setattr(vdb, "_cfg", None)
    monkeypatch.setattr(vdb.config, "db_env", lambda k: None)
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
    assert "undo" not in vdb.TABLES["pix_photo_history"][0] and " undo " not in vdb.TABLES["pix_photo_history"][1]
