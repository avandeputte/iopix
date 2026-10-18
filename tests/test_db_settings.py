"""The digiKam database connection is a setting the admin can change at runtime: settings
override the environment, the password is stored but never shown, a test connection reports
counts without saving, and saving kicks the index."""
from app.digikam import dkdb
from app.data import state


def _clear():
    for k in dkdb.KEYS:
        state.set_setting("db_" + k, "")


def test_settings_override_environment_and_hide_password(client):
    _clear()
    assert dkdb.settings()["type"] == "sqlite" and dkdb.is_mysql() is False
    r = client.post("/api/settings", json={"database": {"type": "mysql", "host": "db.local", "port": "3307", "database": "dk", "user": "u", "password": "p@ss"}})
    assert r.status_code == 200
    c = dkdb.settings()
    assert c["type"] == "mysql" and c["host"] == "db.local" and c["port"] == 3307 and c["password"] == "p@ss"
    pub = client.get("/api/settings").json()["database"]
    assert "password" not in pub and pub["password_set"] is True and pub["host"] == "db.local"
    assert "p@ss" not in client.get("/api/settings").text
    client.post("/api/settings", json={"database": {"type": "mysql", "password": ""}})
    assert dkdb.settings()["password"] == "p@ss", "an empty password field keeps the stored one"
    assert "MySQL u@db.local:3307/dk" == dkdb.describe()
    _clear()
    assert dkdb.describe().endswith("digikam4.db")


def test_test_connection_sqlite_and_mysql(client, monkeypatch, lib):
    _clear()
    resp = client.post("/api/db/test", json={"type": "sqlite", "sqlite_dir": str(lib["db"].parent)})
    assert resp.status_code == 200, resp.text
    r = resp.json()
    assert r["ok"] and r["images"] == len(lib["ids"]) and r["roots"]
    bad = client.post("/api/db/test", json={"type": "sqlite", "sqlite_dir": "/nowhere"})
    assert bad.status_code == 400 and "digikam4.db" in bad.json()["detail"]

    class FakeCur:
        def __init__(self): self.q = None
        def execute(self, sql, params=()): self.q = sql
        def fetchone(self): return (3,) if "count" in self.q else ("17",)
        def fetchall(self): return [("/photos",)]
        def __iter__(self): return iter(self.fetchall())
        @property
        def lastrowid(self): return 0
        @property
        def rowcount(self): return 0
    class FakeRaw:
        def cursor(self): return FakeCur()
        def close(self): pass
        def commit(self): pass
        def rollback(self): pass
    import pymysql
    monkeypatch.setattr(pymysql, "connect", lambda **kw: FakeRaw())  # the real module may already be imported
    resp = client.post("/api/db/test", json={"type": "mysql", "host": "h", "port": "3306", "database": "d", "user": "u", "password": "p"})
    assert resp.status_code == 200, resp.text
    r = resp.json()
    assert r["ok"] and r["images"] == 3 and r["dbVersion"] == "17" and r["roots"] == ["/photos"]
    _clear()


def test_mysql_change_fingerprint_sees_edits_in_place(monkeypatch):
    """On MySQL the viewer notices its own tag edits (badge picture, rename, move) and face changes,
    not only added or removed rows, so the index is rebuilt and the UI shows them."""
    from app.digikam import dkdb
    monkeypatch.setattr(dkdb, "is_mysql", lambda: True)
    rows = {"icon": 86}
    class Cur:
        def __init__(self, sql): self.sql = sql
        def fetchone(self):
            if "Scanned" in self.sql: return ("2026-10-14",)
            if "FROM Tags" in self.sql: return (10, 10, 1000 + rows["icon"])  # the CRC sum moves with the icon
            return (1, 1)
    class Con:
        def execute(self, sql, params=()): return Cur(sql)
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(dkdb, "connect", lambda *a, **k: Con())
    before = dkdb.source_version()
    rows["icon"] = 30088
    assert dkdb.source_version() != before
    sqls = []
    class Con2(Con):
        def execute(self, sql, params=()): sqls.append(sql); return Cur(sql)
    monkeypatch.setattr(dkdb, "connect", lambda *a, **k: Con2())
    dkdb.source_version()
    assert any("CRC32" in s and "FROM Tags" in s for s in sqls) and any("FROM TagProperties" in s for s in sqls) and any("CRC32" in s and "FROM ImageTagProperties" in s for s in sqls)


def test_mysql_dump_only_for_destructive_operations(monkeypatch, tmp_path):
    """MySQL: ordinary changes take no dump (whatever the mode); destructive ones (force=True) take a fresh one
    each time in auto and daily (the old name for auto); skip never dumps."""
    from app.digikam import dkdb
    monkeypatch.setattr(dkdb, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(dkdb, "is_mysql", lambda: True)
    mode = {"backup": "daily"}
    monkeypatch.setattr(dkdb, "settings", lambda: {**{k: "" for k in dkdb.KEYS}, "type": "mysql", "host": "h", "port": 3306, "database": "d", "user": "u", "password": "p", **mode})
    calls = []
    def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
        calls.append(cmd); stdout.write(b"x" * 2048)
        class R: returncode = 0; stderr = b""
        return R()
    monkeypatch.setattr(dkdb.subprocess, "run", fake_run)
    monkeypatch.setattr(dkdb.shutil, "which", lambda n: "/usr/bin/mysqldump")
    assert dkdb.backup() is None and not calls, "an ordinary change: no dump"
    first = dkdb.backup(force=True)
    assert first.exists() and len(calls) == 1
    assert dkdb.backup(force=True) != first and len(calls) == 2, "each destructive operation gets its own"
    mode["backup"] = "auto"
    dkdb.backup(); assert len(calls) == 2
    dkdb.backup(force=True); assert len(calls) == 3
    mode["backup"] = "skip"
    assert dkdb.backup(force=True) is None and len(calls) == 3


def test_a_bad_database_setting_does_not_kill_the_index_watcher(client, lib):
    """Pointing the settings at an unreachable MySQL made the refresh raise inside the watcher
    thread, which then died silently: no later change was ever indexed. It survives now."""
    import time
    from app import main as appmain
    _clear()
    client.post("/api/settings", json={"database": {"type": "mysql", "host": "127.0.0.1", "port": "1", "database": "dk", "user": "u", "password": "p"}})
    time.sleep(1.5)  # the kick from saving runs the refresh against the bad setting
    _clear()
    appmain.state["version"] = None  # make the next refresh observable
    appmain._kick.set()
    end = time.time() + 10
    while time.time() < end and not appmain.state.get("version"):
        time.sleep(0.2)
    assert appmain.state.get("version"), "the watcher thread is still alive and refreshed the index"
    assert any(t.name for t in __import__("threading").enumerate() if t.is_alive() and t._target is appmain._watch) if hasattr(appmain, "_watch") else True
