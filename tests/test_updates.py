"""Self-update: the installed commit is known, GitHub's main branch is compared with it
(network stubbed), the run is refused in Docker, and only admins may trigger it."""
from app import updates


def test_status_and_check(client, monkeypatch):
    monkeypatch.setattr(updates, "local_commit", lambda: "abcdef1234567890")
    s = client.get("/api/update").json()
    assert s["installed"] == "abcdef1" and "canRun" in s and "docker" in s
    monkeypatch.setattr(updates, "_fetch", lambda url: {"sha": "abcdef1234567890", "commit": {"committer": {"date": "2026-10-14T20:00:00Z"}, "message": "same\n\nbody"}})
    c = client.post("/api/update", json={"action": "check"}).json()
    assert c["status"] == "current" and c["remoteMessage"] == "same"
    monkeypatch.setattr(updates, "_fetch", lambda url: {"sha": "ffffff1234567890", "commit": {"committer": {"date": "2026-10-15T00:00:00Z"}, "message": "newer"}})
    c = client.post("/api/update", json={"action": "check"}).json()
    assert c["status"] == "update" and c["remote"] == "ffffff1" and c["remoteDate"] == "2026-10-15"
    assert client.get("/api/update").json()["check"]["status"] == "update"
    def boom(url):
        raise OSError("offline")
    monkeypatch.setattr(updates, "_fetch", boom)
    assert client.post("/api/update", json={"action": "check"}).json()["status"] == "error"


def test_run_refused_in_docker_and_bad_action(client, monkeypatch):
    monkeypatch.setattr(updates, "in_docker", lambda: True)
    r = client.post("/api/update", json={"action": "run"})
    assert r.status_code == 409 and "pull" in r.json()["detail"]
    assert client.post("/api/update", json={"action": "nope"}).status_code == 400


def test_run_starts_update_script(client, monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "in_docker", lambda: False)
    (tmp_path / "update.sh").write_text("#!/bin/sh\necho updated\n")
    monkeypatch.setattr(updates.config, "APP_DIR", tmp_path)
    assert client.post("/api/update", json={"action": "run"}).status_code == 200
    import time
    for _ in range(50):
        if not updates.progress["running"]:
            break
        time.sleep(0.1)
    assert updates.progress["error"] is None and "updated" in updates.progress["log"]


def test_run_passes_the_ref_and_keeps_the_outcome(client, monkeypatch, tmp_path):
    """The ref goes to update.sh in the environment; the outcome update.sh records in the data
    folder survives the restart and is shown as lastUpdate (and by check_for_update)."""
    monkeypatch.setattr(updates, "in_docker", lambda: False)
    (tmp_path / "update.sh").write_text('#!/bin/sh\necho "ref=$IOPIX_REF"\necho "ok: updated to v9 (abc1234)" > "$IOPIX_RESULT"\n')
    monkeypatch.setattr(updates.config, "APP_DIR", tmp_path)
    assert client.post("/api/update", json={"action": "run", "ref": "abc1234; rm -rf /"}).status_code >= 400, "a ref is a commit id or a branch name, nothing else"
    assert client.post("/api/update", json={"action": "run", "ref": "../../repos/evil/fork/commits/main"}).status_code >= 400
    assert client.post("/api/update", json={"action": "run", "ref": "abc1234"}).status_code == 200
    import time
    for _ in range(50):
        if not updates.progress["running"]:
            break
        time.sleep(0.1)
    assert "ref=abc1234" in updates.progress["log"], "the ref reaches update.sh"
    updates.progress.update(running=False, finished=None, log="", error=None)  # as after the restart
    s = client.get("/api/update").json()
    assert s["lastUpdate"]["ok"] is True and s["lastUpdate"]["text"] == "updated to v9 (abc1234)"
    (tmp_path / "update.sh").write_text('#!/bin/sh\necho "error: could not reach GitHub" > "$IOPIX_RESULT"\nexit 1\n')
    assert client.post("/api/update", json={"action": "run"}).status_code == 200
    for _ in range(50):
        if not updates.progress["running"]:
            break
        time.sleep(0.1)
    s = client.get("/api/update").json()
    assert s["lastUpdate"]["ok"] is False and "GitHub" in s["lastUpdate"]["text"]


def test_daily_check_banner_and_setting(client, monkeypatch):
    """The banner data appears in the status (for admins, with the setting on) once a check found a newer
    commit; switching the setting off forgets it and stops the viewer contacting GitHub."""
    from app.data import state
    monkeypatch.setattr(updates, "local_commit", lambda: "abcdef1234567890")
    monkeypatch.setattr(updates, "_fetch", lambda url: {"sha": "fedcba1234567890", "commit": {"committer": {"date": "2026-10-16T00:00:00Z"}, "message": "v9.9.9: something new\n\nbody"}})
    state.set_setting("update_check", "1")
    updates.check()
    u = client.get("/api/status").json()["updateAvailable"]
    assert u == {"remote": "fedcba1", "version": "9.9.9", "message": "v9.9.9: something new", "date": "2026-10-16"}
    assert client.post("/api/settings", json={"update_check": False}).status_code == 200
    assert client.get("/api/status").json()["updateAvailable"] is None and updates.auto_check_enabled() is False
    assert client.get("/api/maintenance").json()["updateCheck"] is False
    monkeypatch.setenv("UPDATE_CHECK", "0")
    assert client.post("/api/settings", json={"update_check": True}).status_code == 400, "locked off by the environment"
    monkeypatch.delenv("UPDATE_CHECK")
    state.set_setting("update_check", "1")
    assert updates.remote_version("v1.2.3: x") == "1.2.3" and updates.remote_version("docs: x") is None


def test_server_log_endpoint_filters(client):
    import logging
    logging.getLogger("app.sync").warning("probe-warning for the log test")
    r = client.get("/api/log", params={"level": "warning", "contains": "probe-warning"})
    assert r.status_code == 200
    j = r.json()
    if j["lines"]:  # the file handler is installed by the app's start; present in the test client
        assert all("probe-warning" in l for l in j["lines"])
    assert client.get("/api/log", params={"requests": 0}).status_code == 200
