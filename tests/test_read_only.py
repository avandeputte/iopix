"""Read-only mode: every library-changing endpoint is refused, the status
flags tell the front end, viewer-only state still works, and the switch
cannot be turned off when locked by the environment."""
import pytest

from app import config
from app.data import state

WRITES = [
    ("/api/edit", {"ids": [1], "rating": 3}),
    ("/api/edit/revert", {"ids": [1]}),
    ("/api/edits/db", {}),
    ("/api/tags/op", {"op": "add", "parent": 0, "name": "x"}),
    ("/api/faces/op", {"op": "ignore", "image": 1, "rect": [0, 0, 1, 1]}),
    ("/api/faces/batch", {"ops": []}),
    ("/api/faces/person", {"name": "x", "parent": 0}),
    ("/api/placetags", {}),
    ("/api/gpx/apply", {}),
    ("/api/consistency/sync", {}),
]


@pytest.fixture
def read_only(client):
    assert client.post("/api/settings", json={"read_only": True}).status_code == 200
    yield
    client.post("/api/settings", json={"read_only": False})


def test_flags_follow_the_switch(client, read_only):
    s = client.get("/api/status").json()
    assert s["readOnly"] is True and s["editing"] is False and s["dbWrite"] is False


@pytest.mark.parametrize("path,body", WRITES)
def test_write_endpoints_are_refused(client, read_only, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 403, (path, r.status_code, r.text[:120])
    assert "read-only" in r.json()["detail"].lower()


def test_viewer_state_still_works(client, read_only, lib):
    pid = lib["ids"][0]
    assert client.post("/api/favorites", json={"ids": [pid], "on": True}).status_code == 200
    assert client.post("/api/favorites", json={"ids": [pid], "on": False}).status_code == 200


def test_locked_by_environment(client, monkeypatch):
    monkeypatch.setattr(config, "READ_ONLY", True)
    state.set_setting("read_only", "0")
    assert client.get("/api/status").json()["readOnlyLocked"] is True
    assert client.post("/api/settings", json={"read_only": False}).status_code == 400
    monkeypatch.setattr(config, "READ_ONLY", False)


def test_switch_off_restores_writes(client):
    client.post("/api/settings", json={"read_only": False})
    s = client.get("/api/status").json()
    assert s["readOnly"] is False
