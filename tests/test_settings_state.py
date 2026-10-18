"""Settings and viewer state live in the viewer's own database: defaults,
round trips, the read cache, and the read-only switch."""
from app import config
from app.data import state


def test_setting_default_and_roundtrip():
    assert state.get_setting("no-such-key", "dflt") == "dflt"
    state.set_setting("probe", "1")
    assert state.get_setting("probe") == "1"
    state.set_setting("probe", "2")
    assert state.get_setting("probe") == "2"


def test_read_only_follows_setting_unless_locked(monkeypatch):
    state.set_setting("read_only", "0")
    assert state.read_only() is False
    state.set_setting("read_only", "1")
    assert state.read_only() is True
    state.set_setting("read_only", "0")
    monkeypatch.setattr(config, "READ_ONLY", True)
    assert state.read_only() is True


def test_favorites_and_albums_roundtrip():
    # items are (photo id, file hash) pairs so a favorite survives a re-import under a new id
    state.set_favorites([(1, "h1"), (2, "h2")], True)
    assert {1, 2} <= set(state.summary()["favorites"])  # a plain list of photo ids, newest first
    state.set_favorites([(1, "h1")], False)
    assert 1 not in state.summary()["favorites"] and 2 in state.summary()["favorites"]
    aid = state.create_album("tests")
    state.set_album_items(aid, [(1, "h1"), (2, "h2")], True)
    albums = {a["name"]: a for a in state.summary()["albums"]}
    assert "tests" in albums and albums["tests"]["n"] == 2
    state.set_album_items(aid, [(2, "h2")], False)
    assert {a["name"]: a for a in state.summary()["albums"]}["tests"]["n"] == 1
    state.rename_album(aid, "tests2")
    assert "tests2" in {a["name"] for a in state.summary()["albums"]}
    state.delete_album(aid)
    assert "tests2" not in {a["name"] for a in state.summary()["albums"]}
