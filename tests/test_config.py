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
