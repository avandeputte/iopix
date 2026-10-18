"""Album roots: digiKam's recorded path can be mapped to where the folder is mounted here,
per root, from Settings; the single-root PHOTOS_ROOT default still applies otherwise."""

from app import config
from app.digikam import dkdb
from app.data import state


def test_default_single_root_uses_photos_root(client, lib):
    state.set_setting("album_roots", "")
    with dkdb.connect() as con:
        paths = dkdb.root_paths(con)
    assert list(paths.values()) == [str(config.PHOTOS_ROOT)]
    roots = client.get("/api/roots").json()["roots"]
    assert roots[0]["localPath"] == str(config.PHOTOS_ROOT) and roots[0]["exists"] and roots[0]["sample"] > 0 and roots[0]["overridden"] is False


def test_override_maps_the_recorded_path(client, lib, tmp_path):
    with dkdb.connect() as con:
        recorded = con.execute("SELECT specificPath FROM AlbumRoots").fetchone()[0]
    r = client.post("/api/settings", json={"album_roots": {recorded: str(tmp_path)}})
    assert r.status_code == 200 and r.json()["album_roots"] == {recorded.rstrip("/"): str(tmp_path)}
    with dkdb.connect() as con:
        assert list(dkdb.root_paths(con).values()) == [str(tmp_path)]
    roots = client.get("/api/roots").json()["roots"]
    assert roots[0]["overridden"] is True and roots[0]["exists"] is True and roots[0]["sample"] == 0, "an empty mount shows 0 files"
    client.post("/api/settings", json={"album_roots": {recorded: ""}})
    assert dkdb.root_overrides() == {}
    assert client.post("/api/settings", json={"album_roots": [1, 2]}).status_code == 400
