"""The MCP server is part of the app: the streamable-HTTP endpoint at /mcp answers JSON-RPC (initialize, tools/list,
a tool call) on the viewer's own port, to a client signed in with an app password (or a session), as in every app of
the suite. The library tools answer any account; the operator and maintenance tools take an admin."""
import json

import pytest

HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
_bearer = {}


def _login(client, name, password):
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text


def _app_password(client, label="claude") -> str:
    return client.post("/api/auth/app-passwords", json={"label": label}).json()["password"]


@pytest.fixture(autouse=True)
def _mcp_on(client):
    """The endpoint is off by default (Settings › MCP server); these tests switch it on, with an admin, alex, whose app
    password the calls carry (the tests run without accounts otherwise, and the endpoint always wants one)."""
    from app import users
    from app.data import state
    state.set_setting("mcp_enabled", "1")
    users.create("alex", "correct horse", "admin")
    _login(client, "alex", "correct horse")
    _bearer["alex"] = f"Bearer {_app_password(client)}"
    client.cookies.clear()
    yield
    state.set_setting("mcp_enabled", "0")


def _auth(who="alex"):
    return dict(HEADERS, authorization=_bearer[who])


def _viewer(client) -> str:
    """A viewer, vi, and the bearer header of an app password of hers."""
    from app import users
    users.create("vi", "viewer pass", "viewer")
    _login(client, "vi", "viewer pass")
    _bearer["vi"] = f"Bearer {_app_password(client, 'vi')}"
    client.cookies.clear()
    return "vi"


def test_the_endpoint_is_off_until_switched_on(client):
    from app.data import state
    state.set_setting("mcp_enabled", "0")
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=_auth())
    assert r.status_code == 404 and "switched off" in r.json()["detail"]
    _login(client, "alex", "correct horse")
    assert client.get("/api/settings").json()["mcp_enabled"] is False
    assert client.post("/api/settings", json={"mcp_enabled": True}).status_code == 200
    assert client.get("/api/settings").json()["mcp_enabled"] is True
    client.cookies.clear()
    assert _rpc(client, "tools/list")["result"]["tools"]


def test_every_request_needs_an_account(client, lib):
    """Require sign-in off: /mcp still refuses a request without an app password (or session), tools/list included,
    and a wrong one is refused the same way; the web app's own API stays open as before."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    r = client.post("/mcp", json=body, headers=HEADERS)
    assert r.status_code == 401 and "app password" in r.json()["detail"]
    r = client.post("/mcp", json=body, headers=dict(HEADERS, authorization="Bearer nope"))
    assert r.status_code == 401
    assert client.get("/api/status").status_code == 200
    assert _rpc(client, "tools/list")["result"]["tools"]


def _rpc(client, method, params=None, id=1, who="alex"):
    body = {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    r = client.post("/mcp", json=body, headers=_auth(who))
    assert r.status_code == 200, (method, r.status_code, r.text[:300])
    text = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):  # one event per response
        text = "\n".join(l[5:].strip() for l in text.splitlines() if l.startswith("data:"))
    return json.loads(text)


def _call(client, name, args=None, id=1, who="alex"):
    return _rpc(client, "tools/call", {"name": name, "arguments": args or {}}, id=id, who=who)["result"]


def test_initialize_and_tools(client):
    init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
    assert init["result"]["serverInfo"]["name"]
    assert "tools" in init["result"]["capabilities"]
    tools = _rpc(client, "tools/list", id=2)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert len(names) >= 5 and any("search" in n or "photos" in n for n in names), names


def test_a_read_only_tool_call_works_for_a_viewer(client, lib):
    vi = _viewer(client)
    tools = _rpc(client, "tools/list", id=3)["result"]["tools"]
    name = next((t["name"] for t in tools if t["name"] in ("library_summary", "summary", "stats", "library_stats")), None)
    if name is None:
        name = next(t["name"] for t in tools if not t.get("inputSchema", {}).get("required"))
    out = _rpc(client, "tools/call", {"name": name, "arguments": {}}, id=4, who=vi)
    assert "result" in out and not out["result"].get("isError"), out
    photo = json.loads(_call(client, "get_photo", {"photo_id": lib["ids"][0]}, id=5, who=vi)["content"][0]["text"])
    assert "path" not in photo, "the file's location is for admins"
    assert "path" in json.loads(_call(client, "get_photo", {"photo_id": lib["ids"][0]}, id=6)["content"][0]["text"])


def test_diagnostic_tools(client, lib):
    vi = _viewer(client)
    denied = _call(client, "diagnostics", id=9, who=vi)
    assert denied.get("isError") and "admin" in denied["content"][0]["text"].lower()
    d = _call(client, "diagnostics", id=10)
    assert not d.get("isError"), d
    text = json.loads(d["content"][0]["text"])
    assert text["version"] and "tools" in text and "jobs" in text and text["database"]["type"] in ("sqlite", "mysql")
    assert "***" in json.dumps(text["settings"]) or not any("password" in k for k in text["settings"]), "secrets are masked"
    q = json.loads(_call(client, "job_queue", id=11)["content"][0]["text"])
    assert "running" in q and "queued" in q
    lg = json.loads(_call(client, "server_log", {"lines": 5}, id=12)["content"][0]["text"])
    assert "lines" in lg
    pf = json.loads(_call(client, "photo_file", {"photo_id": lib["ids"][0]}, id=13)["content"][0]["text"])
    assert pf["exists"] is True and pf["readable"] is True and "sidecar" in pf
    bad = _call(client, "photo_file", {"photo_id": 999999999}, id=14)
    assert bad.get("isError")


def test_update_viewer_needs_an_admin(client, monkeypatch, tmp_path):
    from app.auth import updater
    started = []
    monkeypatch.setattr(updater, "run", lambda ref="": started.append(ref or 1))
    vi = _viewer(client)
    bad = _call(client, "update_viewer", id=20, who=vi)
    assert bad.get("isError") and "admin" in bad["content"][0]["text"].lower() and not started
    ok = _call(client, "update_viewer", id=21)
    assert not ok.get("isError") and started == [1]
    _call(client, "update_viewer", {"ref": "abc1234"}, id=24)
    assert started == [1, "abc1234"], "the ref reaches the updater"
    chk = _call(client, "check_for_update", id=23, who=vi)
    assert "version" in json.loads(chk["content"][0]["text"])


def test_consistency_tools_need_an_admin(client):
    vi = _viewer(client)
    bad = _call(client, "consistency_report", id=40, who=vi)
    assert bad.get("isError")
    ok = _call(client, "consistency_report", id=41)
    assert not ok.get("isError") and "summary" in ok["content"][0]["text"]
    r = _call(client, "consistency_sync", {"photo_ids": [], "source": "db", "targets": ["db"], "fields": ["title"]}, id=42)
    assert r.get("isError"), "the source cannot also be a target"


def _as(role):
    """The tools called in-process act as this account (over HTTP the gate sets it from the app password)."""
    from app.mcp import current_user
    return current_user.set({"id": 0, "name": "test", "role": role})


def test_rewrite_sidecars_tool(client, lib):
    """rewrite_sidecars takes an admin and rewrites the given photos' sidecars from digiKam's database."""
    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError
    from app.mcp import admin
    _viewer(client)  # accounts exist, so a role counts
    _as("viewer")
    with _pytest.raises(ToolError):
        admin.rewrite_sidecars([lib["ids"][0]])
    _as("admin")
    r = admin.rewrite_sidecars(lib["ids"][:2])
    assert r["photos"] == 2 and r["tags_failed"] == [] and set(r["regions"]) == {str(i) for i in lib["ids"][:2]}, r


def test_edit_photos_tool_sets_the_date_in_sidecar_and_database(client, lib):
    import sqlite3
    from app.mcp import admin
    from app.photos import edits
    _as("admin")
    pid = lib["ids"][-1]
    con = sqlite3.connect(lib["db"]); before = con.execute("SELECT creationDate FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()[0]; con.close()
    r = admin.edit_photos([pid], taken="2015-04-04T12:37:05")
    assert r["edited"] == 1 and r["database"]["applied"] == 1, r
    con = sqlite3.connect(lib["db"])
    assert con.execute("SELECT creationDate FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()[0].startswith("2015-04-04T12:37:05")
    con.close()
    admin.edit_photos([pid], taken=str(before)[:19].replace(" ", "T"))
    edits.forget([pid])


def test_inspect_file_reads_a_video(client, lib):
    from app.mcp import files
    _as("admin")
    r = files.inspect_file(lib["videos"][0])
    assert r["size"] > 0 and r["boxes"] and r["complete"] and "exiftool" in r, r


def test_tag_op_marks_a_person_and_merges_as_an_admin(client, lib):
    """tag_op changes tags as the Tags page does: refused for a viewer; person marks a keyword tag as a person (an Edit
    log entry per photo); merge folds a second entry of that person into it."""
    import sqlite3
    from app.photos import tagmanager
    vi = _viewer(client)
    fam = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-Boyd"}, require_digikam_closed=False)["id"]
    a = tagmanager.apply({"op": "add", "parent": fam, "name": "zz-Steve Boyd"}, require_digikam_closed=False)["id"]
    b = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-steve Boyd"}, require_digikam_closed=False)["id"]
    dk = sqlite3.connect(lib["db"])
    try:
        img = dk.execute("SELECT id FROM Images WHERE status = 1 LIMIT 1").fetchone()[0]
        dk.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (img, b))
        dk.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', 'zz-steve Boyd')", (b,)); dk.commit()
        call = lambda args, i, who="alex": _call(client, "tag_op", args, id=i, who=who)
        assert call({"op": "person", "tag_id": a}, 30, vi).get("isError")
        r = call({"op": "person", "tag_id": a}, 31)
        assert not r.get("isError"), r
        assert dk.execute("SELECT value FROM TagProperties WHERE tagid = ? AND property = 'person'", (a,)).fetchone()[0] == "zz-Steve Boyd"
        r = call({"op": "merge", "tag_id": b, "into": a}, 32)
        assert not r.get("isError"), r
        assert not dk.execute("SELECT 1 FROM Tags WHERE id = ?", (b,)).fetchone()
        assert dk.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (img, a)).fetchone()
        again = call({"op": "person", "tag_id": a}, 33)
        assert again.get("isError") and "already a person" in again["content"][0]["text"]
    finally:
        dk.close()
        import time
        for op in ({"op": "notperson", "id": a}, {"op": "delete", "id": fam}):
            while tagmanager.pending_jobs():
                time.sleep(0.2)
            tagmanager.apply(op, require_digikam_closed=False)
