"""The conventions every app of the suite follows (iosuite_shared.testing): the port, one version, config.env, and an
access marker on every API route (test_access.py also checks each route's level against access_table.json)."""
from pathlib import Path

from iosuite_shared import testing

from app import __version__, access, config

ROOT = Path(__file__).resolve().parent.parent


def test_suite_conventions():
    testing.check_conventions(ROOT, "iopix", __version__, config)


def test_every_api_route_declares_who_may_call_it():
    from app.main import app
    testing.check_access_declared(app, access.declared)


def test_standard_paths():
    assert config.APP_DIR == ROOT and config.STATIC_DIR == ROOT / "static"
    assert config.INDEX_DB == config.DATA_DIR / "index.db"
    assert config.LOG_FILE is None  # LOG_FILE= in the tests' environment


def test_settings_are_written_into_config_env(tmp_path, monkeypatch):
    """What the app saves itself (Settings › iopix database) goes into config.env: the line replaced in place, a
    commented example filled in, the rest added; comments kept, owner-only, and read back the same."""
    f = tmp_path / "config.env"
    f.write_text("# iopix settings\nDB_HOST=old.lan\n# DB_PORT=3306\nPORT=8600\n")
    for n in ("DB_HOST", "DB_PORT", "DB_PASSWORD"):
        monkeypatch.delenv(n, raising=False)
    config.save_values({"DB_HOST": "db.lan", "DB_PORT": "3307", "DB_PASSWORD": "p w#'x"}, f)
    assert f.read_text() == "# iopix settings\nDB_HOST=db.lan\nDB_PORT=3307\nPORT=8600\nDB_PASSWORD=\"p w#'x\"\n"
    assert f.stat().st_mode & 0o777 == 0o600
    config.save_values({"DB_PASSWORD": ' "quoted" '}, f)
    assert config.file_values(f)["DB_PASSWORD"] == ' "quoted" ' and "DB_PASSWORD=' \"quoted\" '" in f.read_text()
    assert config.file_values(f)["DB_HOST"] == "db.lan"
    config.save_values({"DB_PASSWORD": "a'b"}, f)
    assert config.file_values(f)["DB_PASSWORD"] == "a'b"


def test_a_setting_from_the_environment_is_told_apart(tmp_path, monkeypatch):
    f = tmp_path / "config.env"
    f.write_text("DB_HOST=db.lan\n")
    monkeypatch.setattr(config, "CONFIG_FILE", f)
    monkeypatch.setattr(config, "_PROCESS_ENV", {"DB_HOST": "db.lan", "DB_NAME": "compose"})
    assert not config.from_environment("DB_HOST"), "systemd's EnvironmentFile: the file's own value"
    assert config.from_environment("DB_NAME") and not config.from_environment("DB_USER")


def test_no_settings_file_beside_config_env():
    """The database connection lives in config.env (DB_*), not in a file of its own under data/."""
    for f in ("app/data/vdb.py", "setup.sh", "README.md", "static/js/settings.js", "app/main.py", "proxmox/ct/iopix.sh"):
        assert "viewer-db.json" not in (ROOT / f).read_text(), f
