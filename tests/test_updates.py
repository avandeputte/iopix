"""Self-update through iosuite_shared's server_router (/api/server): the installed commit is known, GitHub's main branch
is compared with it (network stubbed), the run is refused in Docker, the ref reaches update.sh, the daily check feeds
the banner, and the server log answers. The updater itself is tested in iosuite-shared."""
import json
import time

from app.auth import updater


def _github(sha, date, message):
    def get(url, accept="application/vnd.github+json"):
        if "raw.githubusercontent" in url:
            return b'__version__ = "9.9.9"\n'
        return json.dumps({"sha": sha, "commit": {"committer": {"date": date}, "message": message}}).encode()
    return get


def _wait():
    for _ in range(50):
        if not updater.progress["running"]:
            break
        time.sleep(0.1)


def test_status_and_check(client, monkeypatch):
    monkeypatch.setattr(updater, "local_commit", lambda: "abcdef1234567890")
    s = client.get("/api/server").json()
    assert s["installed"] == "abcdef1" and "canRun" in s and "docker" in s and "mcpEnabled" in s
    monkeypatch.setattr(updater, "_get_url", _github("abcdef1234567890", "2026-10-14T20:00:00Z", "same\n\nbody"))
    c = client.post("/api/server", json={"action": "check"}).json()
    assert c["status"] == "current" and c["remoteMessage"] == "same"
    monkeypatch.setattr(updater, "_get_url", _github("ffffff1234567890", "2026-10-15T00:00:00Z", "newer"))
    c = client.post("/api/server", json={"action": "check"}).json()
    assert c["status"] == "update" and c["remote"] == "ffffff1" and c["remoteDate"] == "2026-10-15"
    assert client.get("/api/server").json()["check"]["status"] == "update"

    def boom(url, accept=""):
        raise OSError("offline")
    monkeypatch.setattr(updater, "_get_url", boom)
    assert client.post("/api/server", json={"action": "check"}).json()["status"] == "error"
    updater.forget_check()


def test_run_refused_in_docker(client, monkeypatch):
    monkeypatch.setattr(updater, "in_docker", lambda: True)
    r = client.post("/api/server", json={"action": "update"})
    assert r.status_code == 409 and "pull" in r.json()["detail"]


def test_run_starts_update_script(client, monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "in_docker", lambda: False)
    (tmp_path / "update.sh").write_text("#!/bin/sh\necho updated\n")
    (tmp_path / ".venv").mkdir()
    monkeypatch.setattr(updater, "root", tmp_path)
    assert client.post("/api/server", json={"action": "update"}).status_code == 200
    _wait()
    assert updater.progress["error"] is None and "updated" in updater.progress["log"]


def test_run_passes_the_ref_and_keeps_the_outcome(client, monkeypatch, tmp_path):
    """The ref goes to update.sh in the environment; the outcome update.sh records survives the restart and is
    shown as lastUpdate (and by check_for_update)."""
    monkeypatch.setattr(updater, "in_docker", lambda: False)
    (tmp_path / "update.sh").write_text('#!/bin/sh\necho "ref=$IOPIX_REF"\necho "ok: updated to v9 (abc1234)" > "$IOPIX_RESULT"\n')
    (tmp_path / ".venv").mkdir()
    monkeypatch.setattr(updater, "root", tmp_path)
    monkeypatch.setattr(updater, "result_file", tmp_path / ".update-result")
    assert client.post("/api/server", json={"action": "update", "ref": "abc1234; rm -rf /"}).status_code >= 400, "a ref is a commit id or a branch name, nothing else"
    assert client.post("/api/server", json={"action": "update", "ref": "../../repos/evil/fork/commits/main"}).status_code >= 400
    assert client.post("/api/server", json={"action": "update", "ref": "abc1234"}).status_code == 200
    _wait()
    assert "ref=abc1234" in updater.progress["log"], "the ref reaches update.sh"
    updater.progress.update(running=False, finished=None, log="", error=None)  # as after the restart
    s = client.get("/api/server").json()
    assert s["lastUpdate"]["ok"] is True and s["lastUpdate"]["text"] == "updated to v9 (abc1234)"
    (tmp_path / "update.sh").write_text('#!/bin/sh\necho "error: could not reach GitHub" > "$IOPIX_RESULT"\nexit 1\n')
    assert client.post("/api/server", json={"action": "update"}).status_code == 200
    _wait()
    s = client.get("/api/server").json()
    assert s["lastUpdate"]["ok"] is False and "GitHub" in s["lastUpdate"]["text"]


def test_daily_check_banner_and_setting(client, monkeypatch):
    """The banner data appears in the status (for admins, with the setting on) once a check found a newer
    commit; switching the setting off forgets it and stops the viewer contacting GitHub."""
    from app.data import state
    monkeypatch.setattr(updater, "local_commit", lambda: "abcdef1234567890")
    monkeypatch.setattr(updater, "_get_url", _github("fedcba1234567890", "2026-10-16T00:00:00Z", "v9.9.9: something new\n\nbody"))
    monkeypatch.setattr(updater, "check_locked", False)
    state.set_setting("update_check", "1")
    updater.check()
    u = client.get("/api/status").json()["updateAvailable"]
    assert u == {"remote": "fedcba1", "version": "9.9.9", "message": "v9.9.9: something new", "date": "2026-10-16"}
    assert client.post("/api/server", json={"autoCheck": False}).status_code == 200
    assert client.get("/api/status").json()["updateAvailable"] is None and updater.auto_check_enabled() is False
    assert client.get("/api/server").json()["autoCheck"] is False
    monkeypatch.setattr(updater, "check_locked", True)
    assert client.post("/api/server", json={"autoCheck": True}).status_code == 400, "locked off by the environment"
    monkeypatch.setattr(updater, "check_locked", False)
    state.set_setting("update_check", "1")
    updater.forget_check()


def test_server_log_endpoint_filters(client):
    import logging
    logging.getLogger("app.sync").warning("probe-warning for the log test")
    r = client.get("/api/log", params={"level": "warning", "contains": "probe-warning"})
    assert r.status_code == 200
    j = r.json()
    if j["lines"]:  # the file handler is installed by the app's start; present in the test client
        assert all("probe-warning" in l for l in j["lines"])
    assert client.get("/api/log", params={"requests": 0}).status_code == 200
