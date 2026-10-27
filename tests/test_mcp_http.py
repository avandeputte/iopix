"""The MCP server is part of the app: the streamable-HTTP endpoint at /mcp answers JSON-RPC
(initialize, tools/list, a tool call) on the viewer's own port, read-only, without sign-in."""
import json

import pytest

HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def _mcp_on():
    """The endpoint is off by default (Settings › MCP server); these tests switch it on."""
    from app.data import state
    state.set_setting("mcp_enabled", "1")
    yield
    state.set_setting("mcp_enabled", "0")


def test_the_endpoint_is_off_until_switched_on(client):
    from app.data import state
    state.set_setting("mcp_enabled", "0")
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=HEADERS)
    assert r.status_code == 404 and "switched off" in r.json()["detail"]
    assert client.get("/api/settings").json()["mcp_enabled"] is False
    assert client.post("/api/settings", json={"mcp_enabled": True}).status_code == 200
    assert client.get("/api/settings").json()["mcp_enabled"] is True
    assert _rpc(client, "tools/list")["result"]["tools"]


def _rpc(client, method, params=None, id=1):
    body = {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    r = client.post("/mcp", json=body, headers=HEADERS)
    assert r.status_code == 200, (method, r.status_code, r.text[:300])
    text = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):  # one event per response
        text = "\n".join(l[5:].strip() for l in text.splitlines() if l.startswith("data:"))
    return json.loads(text)


def test_initialize_and_tools(client):
    init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
    assert init["result"]["serverInfo"]["name"]
    assert "tools" in init["result"]["capabilities"]
    tools = _rpc(client, "tools/list", id=2)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert len(names) >= 5 and any("search" in n or "photos" in n for n in names), names


def test_a_read_only_tool_call_works_anonymously(client, lib):
    tools = _rpc(client, "tools/list", id=3)["result"]["tools"]
    name = next((t["name"] for t in tools if t["name"] in ("library_summary", "summary", "stats", "library_stats")), None)
    if name is None:
        name = next(t["name"] for t in tools if not t.get("inputSchema", {}).get("required"))
    out = _rpc(client, "tools/call", {"name": name, "arguments": {}}, id=4)
    assert "result" in out and not out["result"].get("isError"), out


def test_diagnostic_tools(client, lib):
    tok = client.post("/api/update", json={"action": "token"}).json()["token"]
    denied = _rpc(client, "tools/call", {"name": "diagnostics", "arguments": {"token": "nope"}}, id=9)["result"]
    assert denied.get("isError") and "token" in denied["content"][0]["text"].lower()
    d = _rpc(client, "tools/call", {"name": "diagnostics", "arguments": {"token": tok}}, id=10)["result"]
    assert not d.get("isError"), d
    text = json.loads(d["content"][0]["text"])
    assert text["version"] and "tools" in text and "jobs" in text and text["database"]["type"] in ("sqlite", "mysql")
    assert "***" in json.dumps(text["settings"]) or not any("password" in k for k in text["settings"]), "secrets are masked"
    q = json.loads(_rpc(client, "tools/call", {"name": "job_queue", "arguments": {"token": tok}}, id=11)["result"]["content"][0]["text"])
    assert "running" in q and "queued" in q
    lg = json.loads(_rpc(client, "tools/call", {"name": "server_log", "arguments": {"token": tok, "lines": 5}}, id=12)["result"]["content"][0]["text"])
    assert "lines" in lg
    pf = json.loads(_rpc(client, "tools/call", {"name": "photo_file", "arguments": {"token": tok, "photo_id": lib["ids"][0]}}, id=13)["result"]["content"][0]["text"])
    assert pf["exists"] is True and pf["readable"] is True and "sidecar" in pf
    bad = _rpc(client, "tools/call", {"name": "photo_file", "arguments": {"token": tok, "photo_id": 999999999}}, id=14)["result"]
    assert bad.get("isError")


def test_update_viewer_needs_the_admin_token(client, monkeypatch, tmp_path):
    from app import updates
    started = []
    monkeypatch.setattr(updates, "run", lambda ref="": started.append(ref or 1))
    bad = _rpc(client, "tools/call", {"name": "update_viewer", "arguments": {"token": "nope"}}, id=20)["result"]
    assert bad.get("isError") and "token" in bad["content"][0]["text"].lower() and not started
    tok = client.post("/api/update", json={"action": "token"}).json()["token"]
    assert len(tok) >= 24
    assert tok not in client.get("/api/update").text, "the token is never in the plain status"
    ok = _rpc(client, "tools/call", {"name": "update_viewer", "arguments": {"token": tok}}, id=21)["result"]
    assert not ok.get("isError") and started == [1]
    _rpc(client, "tools/call", {"name": "update_viewer", "arguments": {"token": tok, "ref": "abc1234"}}, id=24)
    assert started == [1, "abc1234"], "the ref reaches the updater"
    new = client.post("/api/update", json={"action": "regenerate_token"}).json()["token"]
    assert new != tok
    again = _rpc(client, "tools/call", {"name": "update_viewer", "arguments": {"token": tok}}, id=22)["result"]
    assert again.get("isError"), "the old token stops working"
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "from-env-token")
    assert updates.admin_token() == "from-env-token"
    assert client.post("/api/update", json={"action": "regenerate_token"}).status_code == 400
    chk = _rpc(client, "tools/call", {"name": "check_for_update", "arguments": {}}, id=23)["result"]
    assert "version" in json.loads(chk["content"][0]["text"])


def test_consistency_tools_need_the_token(client):
    from app import updates
    bad = _rpc(client, "tools/call", {"name": "consistency_report", "arguments": {"token": "nope"}}, id=40)["result"]
    assert bad.get("isError")
    tok = updates.admin_token()
    ok = _rpc(client, "tools/call", {"name": "consistency_report", "arguments": {"token": tok}}, id=41)["result"]
    assert not ok.get("isError") and "summary" in ok["content"][0]["text"]
    r = _rpc(client, "tools/call", {"name": "consistency_sync", "arguments": {"token": tok, "photo_ids": [], "source": "db", "targets": ["db"], "fields": ["title"]}}, id=42)["result"]
    assert r.get("isError"), "the source cannot also be a target"


def test_rewrite_sidecars_tool(client, lib):
    """rewrite_sidecars needs the admin token and rewrites the given photos' sidecars from digiKam's database."""
    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError
    from app import mcp_server, updates
    with _pytest.raises(ToolError):
        mcp_server.rewrite_sidecars("wrong", [lib["ids"][0]])
    r = mcp_server.rewrite_sidecars(updates.admin_token(), lib["ids"][:2])
    assert r["photos"] == 2 and r["tags_failed"] == [] and set(r["regions"]) == {str(i) for i in lib["ids"][:2]}, r


def test_edit_photos_tool_sets_the_date_in_sidecar_and_database(client, lib):
    import sqlite3
    from app import mcp_server, updates
    from app.photos import edits
    pid = lib["ids"][-1]
    con = sqlite3.connect(lib["db"]); before = con.execute("SELECT creationDate FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()[0]; con.close()
    r = mcp_server.edit_photos(updates.admin_token(), [pid], taken="2015-04-04T12:37:05")
    assert r["edited"] == 1 and r["database"]["applied"] == 1, r
    con = sqlite3.connect(lib["db"])
    assert con.execute("SELECT creationDate FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()[0].startswith("2015-04-04T12:37:05")
    con.close()
    mcp_server.edit_photos(updates.admin_token(), [pid], taken=str(before)[:19].replace(" ", "T"))
    edits.forget([pid])


def test_inspect_file_reads_a_video(client, lib):
    from app import mcp_server, updates
    r = mcp_server.inspect_file(updates.admin_token(), lib["videos"][0])
    assert r["size"] > 0 and r["boxes"] and r["complete"] and "exiftool" in r, r


def test_tag_op_marks_a_person_and_merges_with_the_token(client, lib):
    """tag_op changes tags as the Tags page does: refused without the admin token; person marks a keyword tag as a
    person (an Edit log entry per photo); merge folds a second entry of that person into it."""
    import sqlite3
    from app.photos import tagmanager
    tok = client.post("/api/update", json={"action": "token"}).json()["token"]
    fam = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-Boyd"}, check_running=False)["id"]
    a = tagmanager.apply({"op": "add", "parent": fam, "name": "zz-Steve Boyd"}, check_running=False)["id"]
    b = tagmanager.apply({"op": "add", "parent": 0, "name": "zz-steve Boyd"}, check_running=False)["id"]
    dk = sqlite3.connect(lib["db"])
    try:
        img = dk.execute("SELECT id FROM Images WHERE status = 1 LIMIT 1").fetchone()[0]
        dk.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (img, b))
        dk.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', 'zz-steve Boyd')", (b,)); dk.commit()
        call = lambda args, i: _rpc(client, "tools/call", {"name": "tag_op", "arguments": args}, id=i)["result"]
        assert call({"token": "nope", "op": "person", "tag_id": a}, 30).get("isError")
        r = call({"token": tok, "op": "person", "tag_id": a}, 31)
        assert not r.get("isError"), r
        assert dk.execute("SELECT value FROM TagProperties WHERE tagid = ? AND property = 'person'", (a,)).fetchone()[0] == "zz-Steve Boyd"
        r = call({"token": tok, "op": "merge", "tag_id": b, "into": a}, 32)
        assert not r.get("isError"), r
        assert not dk.execute("SELECT 1 FROM Tags WHERE id = ?", (b,)).fetchone()
        assert dk.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (img, a)).fetchone()
        again = call({"token": tok, "op": "person", "tag_id": a}, 33)
        assert again.get("isError") and "already a person" in again["content"][0]["text"]
    finally:
        dk.close()
        import time
        for op in ({"op": "notperson", "id": a}, {"op": "delete", "id": fam}):
            while tagmanager.pending_jobs():
                time.sleep(0.2)
            tagmanager.apply(op, check_running=False)


def test_the_first_setting_keeps_existing_installs_on_and_is_kept_after(client):
    from app import main
    from app.data import state
    for upgraded, want in ((True, "1"), (False, "0")):
        state.set_setting("mcp_enabled", "")  # never set: the first start with the switch
        main._mcp_first_setting(upgraded)
        assert state.get_setting("mcp_enabled", "") == want
    state.set_setting("mcp_enabled", "1")  # chosen once: later starts (updates) keep it
    main._mcp_first_setting(False)
    assert state.get_setting("mcp_enabled", "") == "1"
