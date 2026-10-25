"""Users and permissions: open until the first user exists; then anonymous is read-only,
viewers keep settings and favorites on the server, editors write the library, admins
manage settings, maintenance and users; sessions are signed cookies."""
from app import users


def _login(client, name, pw):
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"name": name, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def test_open_until_first_user(client, fresh):
    s = client.get("/api/status").json()
    assert s["usersExist"] is False and s["isAdmin"] is True and s["editing"] is True
    assert client.post("/api/favorites", json={"ids": [], "on": True}).status_code == 200
    assert client.post("/api/users", json={"name": "x", "password": "password1", "role": "viewer"}).status_code == 200, "open mode: anyone may create (that is how the first admin gets made)"


def test_setup_creates_the_admin_once(client, fresh):
    r = client.post("/api/auth/setup", json={"name": "alex", "password": "correct horse"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin" and users.COOKIE in r.cookies
    assert client.post("/api/auth/setup", json={"name": "eve", "password": "hackhackhack"}).status_code == 409
    me = client.get("/api/auth/me").json()
    assert me["user"]["name"] == "alex" and me["isAdmin"]


def test_anonymous_is_read_only_once_users_exist(client, fresh, lib):
    users.create("alex", "correct horse", "admin")
    client.cookies.clear()
    s = client.get("/api/status").json()
    assert s["user"] is None and s["editing"] is False and s["dbWrite"] is False and s["isAdmin"] is False
    assert client.post("/api/edit", json={"ids": [lib["ids"][0]], "rating": 3}).status_code == 401
    assert client.post("/api/settings", json={"bg_threads": 2}).status_code == 401
    assert client.post("/api/favorites", json={"ids": [], "on": True}).status_code == 401
    assert client.get("/api/photos").status_code == 200, "browsing stays open"


def test_roles(client, fresh, lib):
    users.create("alex", "correct horse", "admin"); users.create("ed", "editor-pass", "editor"); users.create("vi", "viewer-pass", "viewer")
    _login(client, "vi", "viewer-pass")
    s = client.get("/api/status").json()
    assert s["user"]["role"] == "viewer" and s["editing"] is False and s["signedIn"] is True
    assert client.post("/api/favorites", json={"ids": [lib["ids"][0]], "on": True}).status_code == 200
    assert client.post("/api/edit", json={"ids": [lib["ids"][0]], "rating": 3}).status_code == 403
    assert client.post("/api/settings", json={"bg_threads": 2}).status_code == 403
    assert client.get("/api/users").status_code == 403
    # reads that are not for everyone: the server log and the merge plan (admins), the change log and the elevation lookup (editors)
    assert client.get("/api/log").status_code == 403 and client.get("/api/log/download").status_code == 403
    assert client.get("/api/library/merge").status_code == 403
    assert client.get("/api/history").status_code == 403 and client.get("/api/elevation?lat=1&lon=1").status_code == 403
    assert "database" not in client.get("/api/settings").json() and "paths" not in client.get("/api/maintenance").json()
    _login(client, "ed", "editor-pass")
    s = client.get("/api/status").json()
    assert s["editing"] is True and s["isAdmin"] is False
    assert client.post("/api/settings", json={"bg_threads": 2}).status_code == 403
    assert client.get("/api/history").status_code == 200 and client.get("/api/log").status_code == 403
    assert client.post("/api/users", json={"name": "z", "password": "password1", "role": "viewer"}).status_code == 403
    _login(client, "alex", "correct horse")
    assert client.get("/api/status").json()["isAdmin"] is True
    assert client.get("/api/log").status_code == 200 and "paths" in client.get("/api/maintenance").json()
    assert client.post("/api/settings", json={"bg_threads": 2}).status_code == 200
    assert client.get("/api/users").status_code == 200 and len(client.get("/api/users").json()["users"]) == 3


def test_wrong_password_and_bad_cookie(client, fresh):
    users.create("alex", "correct horse", "admin")
    assert client.post("/api/auth/login", json={"name": "alex", "password": "nope"}).status_code == 401
    client.cookies.set(users.COOKIE, "1.9999999999.deadbeef")
    assert client.get("/api/auth/me").json()["user"] is None


def test_per_user_settings_roundtrip(client, fresh):
    users.create("vi", "viewer-pass", "viewer")
    _login(client, "vi", "viewer-pass")
    assert client.post("/api/auth/settings", json={"badges": "0", "tile": "L"}).json() == {"badges": "0", "tile": "L"}
    assert client.get("/api/auth/me").json()["settings"] == {"badges": "0", "tile": "L"}
    assert client.post("/api/auth/settings", json={"badges": None}).json() == {"tile": "L"}


def test_admin_manages_users_and_cannot_remove_the_last_admin(client, fresh):
    users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    u = client.post("/api/users", json={"name": "ed", "password": "editor-pass", "role": "editor"}).json()
    assert client.post(f"/api/users/{u['id']}", json={"role": "admin"}).status_code == 200
    assert client.post(f"/api/users/{u['id']}", json={"password": "new-password-1"}).status_code == 200
    client.cookies.clear(); _login(client, "ed", "new-password-1")
    me = client.get("/api/auth/me").json()["user"]
    assert client.post(f"/api/users/{me['id']}", json={"delete": True}).status_code == 400, "not while signed in as that user"
    alex = next(x for x in client.get("/api/users").json()["users"] if x["name"] == "alex")
    assert client.post(f"/api/users/{alex['id']}", json={"role": "viewer"}).status_code == 200
    assert client.post(f"/api/users/{me['id']}", json={"role": "viewer"}).status_code == 400, "the only admin left"
    assert client.post("/api/users", json={"name": "ed", "password": "password1", "role": "viewer"}).status_code == 400, "name taken"
    assert client.post("/api/users", json={"name": "short", "password": "short", "role": "viewer"}).status_code == 400


def test_own_password_change(client, fresh):
    users.create("vi", "viewer-pass", "viewer")
    _login(client, "vi", "viewer-pass")
    assert client.post("/api/auth/password", json={"current": "wrong", "password": "another-pass"}).status_code == 403
    assert client.post("/api/auth/password", json={"current": "viewer-pass", "password": "another-pass"}).status_code == 200
    _login(client, "vi", "another-pass")


def test_passwordless_viewer_one_click_and_limits(client, fresh):
    users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    assert client.post("/api/users", json={"name": "kid", "password": "", "role": "editor"}).status_code == 400, "no password means viewer only"
    kid = client.post("/api/users", json={"name": "kid", "password": "", "role": "viewer"}).json()
    assert kid["passwordless"] is True
    assert client.post(f"/api/users/{kid['id']}", json={"role": "editor"}).status_code == 400, "cannot be promoted without a password"
    alex = next(x for x in client.get("/api/users").json()["users"] if x["name"] == "alex")
    assert client.post(f"/api/users/{alex['id']}", json={"password": ""}).status_code == 400, "an admin keeps a password"
    client.cookies.clear()
    acc = client.get("/api/auth/accounts").json()["accounts"]
    assert [(a["name"], a["passwordless"]) for a in acc] == [("alex", False), ("kid", True)]
    r = client.post("/api/auth/login", json={"name": "kid"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "viewer"
    s = client.get("/api/status").json()
    assert s["user"]["name"] == "kid" and s["editing"] is False and s["signedIn"] is True
    assert client.post("/api/favorites", json={"ids": [], "on": True}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"name": "alex"}).status_code == 401, "a protected account still needs its password"
    _login(client, "alex", "correct horse")
    assert client.post(f"/api/users/{kid['id']}", json={"password": "now-protected"}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"name": "kid"}).status_code == 401


def test_account_linked_to_a_person(client, fresh):
    users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    tags = client.get("/api/meta").json()["tags"]
    person = next(t for t in tags if t.get("person") and t["full"] != "People")
    plain = next(t for t in tags if not t.get("person"))
    me = client.get("/api/auth/me").json()["user"]
    assert client.post(f"/api/users/{me['id']}", json={"person": plain["id"]}).status_code == 400, "only a person tag"
    assert client.post("/api/auth/person", json={"person": person["id"]}).json()["person"] == person["id"]
    assert client.get("/api/status").json()["user"]["person"] == person["id"]
    assert client.get("/api/users").json()["users"][0]["person"] == person["id"]
    client.cookies.clear()
    assert client.get("/api/auth/accounts").json()["accounts"][0]["person"] == person["id"], "the picker can show the face"
    _login(client, "alex", "correct horse")
    assert client.post("/api/auth/person", json={"person": None}).json()["person"] is None
    # only an admin links accounts to people: an editor or viewer cannot link themselves
    users.create("ed", "editor-pass", "editor")
    _login(client, "ed", "editor-pass")
    assert client.post("/api/auth/person", json={"person": person["id"]}).status_code == 403
    _login(client, "alex", "correct horse")
    ed = next(u for u in client.get("/api/users").json()["users"] if u["name"] == "ed")
    assert client.post(f"/api/users/{ed['id']}", json={"person": person["id"]}).status_code == 200


def test_optional_email_on_accounts(client, fresh):
    """An account may carry an email address (for a proxy to identify users by later): set at
    creation or afterwards, validated, unique, clearable, and shown in the admin's list."""
    users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    u = client.post("/api/users", json={"name": "ed", "password": "editor-pass", "role": "editor", "email": " Ed@Example.com "}).json()
    assert u["email"] == "ed@example.com", "trimmed and lower-cased"
    assert client.post("/api/users", json={"name": "ann", "password": "", "role": "viewer", "email": "not-an-address"}).status_code == 400
    assert client.post("/api/users", json={"name": "ann", "password": "", "role": "viewer", "email": "ed@example.com"}).status_code == 400, "unique"
    ann = client.post("/api/users", json={"name": "ann", "password": "", "role": "viewer"}).json()
    assert ann["email"] == ""
    assert client.post(f"/api/users/{ann['id']}", json={"email": "ann@example.com"}).status_code == 200
    assert client.post(f"/api/users/{ann['id']}", json={"email": "ed@example.com"}).status_code == 400
    listed = {x["name"]: x["email"] for x in client.get("/api/users").json()["users"]}
    assert listed == {"alex": "", "ed": "ed@example.com", "ann": "ann@example.com"}
    assert client.post(f"/api/users/{ann['id']}", json={"email": ""}).status_code == 200
    assert next(x for x in client.get("/api/users").json()["users"] if x["name"] == "ann")["email"] == ""
    assert "email" not in client.get("/api/auth/accounts").json()["accounts"][0], "the public picker shows no addresses"


def test_require_login_locks_everything_but_the_sign_in(client, fresh, lib):
    assert client.post("/api/settings", json={"require_login": True}).status_code == 400, "no account yet: nobody could sign in"
    users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    assert client.post("/api/settings", json={"require_login": True}).status_code == 200
    assert client.get("/api/status").json()["requireLogin"] is True
    client.cookies.clear()
    # a visitor: the shell and the sign-in endpoints work, nothing else does
    for path in ("/", "/app.js", "/app.css", "/manifest.webmanifest", "/api/status", "/api/auth/me", "/api/auth/accounts"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/auth/me").json()["requireLogin"] is True
    pid = lib["ids"][0] if isinstance(lib, dict) and "ids" in lib else client.get("/api/photos", cookies={}).json().get("id", [None])[0]
    for path in ("/api/meta", "/api/photos", "/api/users", "/api/tags", "/api/concepts"):
        assert client.get(path).status_code == 401, path
    if pid:
        for path in (f"/thumb/{pid}", f"/preview/{pid}", f"/orig/{pid}", f"/api/photo/{pid}"):
            assert client.get(path).status_code == 401, path
    # the account picker's avatars: the face badge of a linked person is served, other faces are not
    _login(client, "alex", "correct horse")
    tags = client.get("/api/meta").json()["tags"]
    people = [t for t in tags if t.get("person") and t["full"] != "People"]
    if len(people) >= 2:
        assert client.post("/api/auth/person", json={"person": people[0]["id"]}).status_code == 200
        client.cookies.clear()
        assert client.get(f"/face/{people[0]['id']}").status_code != 401
        assert client.get(f"/face/{people[1]['id']}").status_code == 401
    client.cookies.clear()
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401, "MCP needs the admin token as a bearer header"
    from app import updates
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"accept": "application/json, text/event-stream", "authorization": "Bearer " + updates.admin_token()})
    assert r.status_code == 200
    # signed in again: everything is back
    _login(client, "alex", "correct horse")
    assert client.get("/api/meta").status_code == 200
    assert client.post("/api/settings", json={"require_login": False}).status_code == 200
    client.cookies.clear()
    assert client.get("/api/meta").status_code == 200, "open browsing again"


def test_backup_downloads_are_for_admins(client, fresh, lib):
    users.create("alex", "correct horse", "admin")
    users.create("vi", "", "viewer")
    client.cookies.clear()
    assert client.get("/api/backup/app").status_code == 401, "once accounts exist, a visitor gets nothing"
    _login(client, "vi", "")
    assert client.get("/api/backup/app").status_code == 403
    _login(client, "alex", "correct horse")
    r = client.get("/api/backup/app")
    assert r.status_code == 200 and r.content.startswith(b"PK") and r.headers["content-disposition"].endswith('.zip"')
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "viewer.db" in names and "README.txt" in names and "analysis.db" not in names, names
    r = client.get("/api/backup/digikam")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3") and "digikam4-" in r.headers["content-disposition"]
    assert client.get("/api/backup/other").status_code == 404


def test_an_account_can_be_left_off_the_sign_in_list(client, fresh):
    """Settings › Users › Sign-in list: an unticked account is not offered on the sign-in screen; "Other" with
    its name (and password) still signs it in."""
    users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    kid = client.post("/api/users", json={"name": "kid", "password": "", "role": "viewer"}).json()
    alex = next(x for x in client.get("/api/users").json()["users"] if x["name"] == "alex")
    assert alex["listed"] is True and kid["listed"] is True
    assert client.post(f"/api/users/{alex['id']}", json={"listed": False}).status_code == 200
    assert next(x for x in client.get("/api/users").json()["users"] if x["name"] == "alex")["listed"] is False
    client.cookies.clear()
    assert [a["name"] for a in client.get("/api/auth/accounts").json()["accounts"]] == ["kid"], "the picker leaves the account out"
    _login(client, "alex", "correct horse")  # "Other": the name and password still sign in
    assert client.get("/api/status").json()["user"]["name"] == "alex"
    assert client.post(f"/api/users/{alex['id']}", json={"listed": True}).status_code == 200
    client.cookies.clear()
    assert [a["name"] for a in client.get("/api/auth/accounts").json()["accounts"]] == ["alex", "kid"]


def test_a_session_in_use_is_renewed(client, fresh, monkeypatch):
    """A device that uses its session gets a fresh 30 days (at most once a day): the iPhone app stays signed in."""
    import time as _time
    u = users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    assert "set-cookie" not in client.get("/api/auth/me").headers, "a session from today is not renewed"
    now = _time.time()
    monkeypatch.setattr(users.time, "time", lambda: now - 20 * 86400)
    old = users.make_token(u["id"])  # signed 20 days ago: 10 days left
    monkeypatch.undo()
    client.cookies.clear(); client.cookies.set(users.COOKIE, old)
    r = client.get("/api/auth/me")
    assert r.json()["user"]["name"] == "alex"
    fresh_token = r.cookies.get(users.COOKIE)
    assert fresh_token and int(fresh_token.split(".")[1]) > now + 29 * 86400, r.headers.get("set-cookie")
