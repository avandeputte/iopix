"""Shared and private albums: shared ones are seen by everyone and created, changed and deleted only by
editors and admins; private ones are seen and changed only by the user who made them, whatever their role."""
from app.data import state
from app import users


def _login(client, name, pw):
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"name": name, "password": pw})
    assert r.status_code == 200, r.text


def _albums(client):
    return {a["name"]: a for a in client.get("/api/state").json()["albums"]}


def _clear_albums():
    con = state.connect()
    con.execute("DELETE FROM viewer_album_photos"); con.execute("DELETE FROM viewer_albums")
    con.commit(); con.close()


def test_shared_and_private_albums(client, fresh, lib):
    _clear_albums()
    users.create("alex", "admin-pass1", "admin"); users.create("ed", "editor-pass", "editor")
    users.create("vi", "viewer-pass", "viewer"); users.create("vo", "viewer-pass2", "viewer")
    p1, p2 = lib["ids"][0], lib["ids"][1]
    try:
        # a viewer: private albums only
        _login(client, "vi", "viewer-pass")
        st = client.get("/api/state").json()
        assert st["canShareAlbums"] is False and st["canPrivateAlbums"] is True
        assert client.post("/api/ualbums", json={"name": "Team", "shared": True}).status_code == 403
        r = client.post("/api/ualbums", json={"name": "Mine", "ids": [p1]})  # default for a viewer: private
        assert r.status_code == 200
        mine = r.json()["id"]
        a = _albums(client)["Mine"]
        assert a["shared"] is False and a["mine"] is True and a["canEdit"] is True and a["n"] == 1
        assert client.post(f"/api/ualbums/{mine}", json={"ids": [p2], "on": True}).status_code == 200
        assert client.post(f"/api/ualbums/{mine}", json={"shared": True}).status_code == 403, "a viewer cannot share"
        assert client.get(f"/api/photos?ualbum={mine}").json()["count"] == 2

        # an editor makes a shared album; the viewer sees it but cannot change it
        _login(client, "ed", "editor-pass")
        st = client.get("/api/state").json()
        assert st["canShareAlbums"] is True and "Mine" not in _albums(client), "someone else's private album is invisible"
        assert client.get(f"/api/photos?ualbum={mine}").json()["count"] == 0, "and so are its photos"
        assert client.post(f"/api/ualbums/{mine}", json={"name": "x"}).status_code == 404
        team = client.post("/api/ualbums", json={"name": "Team", "ids": [p1]}).json()["id"]  # default for an editor: shared
        assert _albums(client)["Team"]["shared"] is True
        _login(client, "vi", "viewer-pass")
        t = _albums(client)["Team"]
        assert t["shared"] is True and t["canEdit"] is False
        for body in ({"name": "Renamed"}, {"ids": [p2], "on": True}, {"delete": True}, {"shared": False}):
            assert client.post(f"/api/ualbums/{team}", json=body).status_code == 403, body

        # the other viewer sees the shared album only; a visitor too
        _login(client, "vo", "viewer-pass2")
        assert set(_albums(client)) == {"Team"}
        client.cookies.clear()
        assert set(_albums(client)) == {"Team"} and client.post("/api/ualbums", json={"name": "z"}).status_code == 401

        # an admin can share their own private album, and make a shared one private (it becomes theirs)
        _login(client, "alex", "admin-pass1")
        assert "Mine" not in _albums(client), "private means private, admins included"
        own = client.post("/api/ualbums", json={"name": "Draft", "shared": False}).json()["id"]
        assert client.post(f"/api/ualbums/{own}", json={"shared": True}).status_code == 200
        assert client.post(f"/api/ualbums/{team}", json={"shared": False}).status_code == 200
        _login(client, "ed", "editor-pass")
        assert "Team" not in _albums(client) and _albums(client)["Draft"]["canEdit"] is True
        assert client.post(f"/api/ualbums/{own}", json={"delete": True}).status_code == 200, "editors delete shared albums"
        # photo details list only the albums the reader may see
        _login(client, "vo", "viewer-pass2")
        assert client.get(f"/api/photo/{p1}").json()["ualbums"] == []
    finally:
        _clear_albums()


def test_albums_without_accounts_are_shared(client, fresh, lib):
    _clear_albums()
    try:
        st = client.get("/api/state").json()
        assert st["canShareAlbums"] is True and st["canPrivateAlbums"] is False
        assert client.post("/api/ualbums", json={"name": "Open", "shared": False}).status_code == 403
        aid = client.post("/api/ualbums", json={"name": "Open"}).json()["id"]
        assert _albums(client)["Open"]["shared"] is True and state.album(aid)["owner_id"] is None
    finally:
        _clear_albums()
