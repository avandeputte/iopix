"""Updating the viewer itself: which commit is installed, whether GitHub's main branch is
newer, and running update.sh (next to setup.sh) from Maintenance. Docker installs are
updated by pulling the image, so there the check only reports."""
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.request

from app import __version__, config, jobs
from app.data import state

# IOPIX_*; the DIGIKAM_VIEWER_* names from before the rename are still honoured
REPO = os.environ.get("IOPIX_REPO") or os.environ.get("DIGIKAM_VIEWER_REPO") or "avandeputte/iopix"
BRANCH = os.environ.get("IOPIX_BRANCH") or os.environ.get("DIGIKAM_VIEWER_BRANCH") or "main"
progress = jobs.Progress("update", log="", finished=None)
_checked = {}


def forget_check():
    """Drop the remembered result of the last update check, so the next one asks again."""
    _checked.clear()
_commit = None  # local_commit() once known: it only changes with update.sh, which restarts the process
RESULT_FILE = "update-result.txt"  # in the data folder; update.sh writes the outcome there before restarting


def admin_token() -> str:
    """The token that lets an MCP client run the update (the endpoint itself has no sign-in).
    MCP_ADMIN_TOKEN in the environment wins; otherwise one is generated once and kept in settings."""
    env = os.environ.get("MCP_ADMIN_TOKEN", "").strip()
    if env:
        return env
    tok = state.get_setting("mcp_admin_token", "")
    if not tok:
        tok = secrets.token_urlsafe(24)
        state.set_setting("mcp_admin_token", tok)
    return tok


def regenerate_token() -> str:
    tok = secrets.token_urlsafe(24)
    state.set_setting("mcp_admin_token", tok)
    return tok


def token_ok(given: str) -> bool:
    return bool(given) and hmac.compare_digest(str(given), admin_token())


def in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def local_commit() -> str:
    """The installed commit (cached: every status poll asks, and it cannot change while this process runs)."""
    global _commit
    if _commit is None:
        found = _read_commit()
        if found == "unknown":
            return found  # not cached: a transient git failure gets another try
        _commit = found
    return _commit


def _read_commit() -> str:
    git = config.APP_DIR / ".git"
    if git.exists():
        try:
            return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(config.APP_DIR), capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"
    try:
        return (config.APP_DIR / "COMMIT").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "iopix"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


DAILY = 24 * 3600


def auto_check_enabled() -> bool:
    """Settings › Viewer › Check for updates daily (on unless switched off; UPDATE_CHECK=0 locks it off)."""
    if os.environ.get("UPDATE_CHECK", "").strip() == "0":
        return False
    return state.get_setting("update_check", "1") == "1"


def remote_version(message: str):
    """The version a commit announces in its first line ("v1.12.2: ..."), if it does."""
    m = re.match(r"\s*v(\d+\.\d+\.\d+)\b", message or "")
    return m.group(1) if m else None


def available() -> dict:
    """What the banner needs, from the last check: {version, remote, message, date} or None."""
    c = dict(_checked)
    if c.get("status") != "update":
        return None
    return {"remote": c.get("remote"), "version": c.get("remoteVersion") or remote_version(c.get("remoteMessage")), "message": c.get("remoteMessage"), "date": c.get("remoteDate")}


def start_daily():
    """Ask GitHub once a day (the first time a minute after start) whether main has moved on,
    while the setting is on. Only the commit list is fetched; nothing about this install is sent."""
    def loop():
        time.sleep(60)
        while True:
            try:
                if auto_check_enabled():
                    check()
            except Exception:
                pass
            time.sleep(DAILY)
    threading.Thread(target=loop, name="update-check", daemon=True).start()


def _remote_app_version(sha: str):
    """The app version at that commit (app/__init__.py), so a commit that only touches the workflow or the docs is
    not announced as a new build."""
    req = urllib.request.Request(f"https://raw.githubusercontent.com/{REPO}/{sha}/app/__init__.py", headers={"User-Agent": "iopix"})
    with urllib.request.urlopen(req, timeout=10) as r:
        m = re.search(r'^__version__ = "([^"]+)"', r.read().decode("utf-8", "replace"), re.M)
    return m.group(1) if m else None


def check() -> dict:
    """{installed, remote, remoteDate, remoteMessage, remoteVersion, status: current|update|error}"""
    mine = local_commit()
    out = {"installed": mine[:7], "checked": int(time.time())}
    try:
        c = _fetch(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}")
        out.update(remote=c["sha"][:7], remoteDate=c["commit"]["committer"]["date"][:10], remoteMessage=c["commit"]["message"].splitlines()[0][:100])
        if c["sha"].startswith(mine) and mine != "unknown":
            out["status"] = "current"
        else:
            try:
                rv = _remote_app_version(c["sha"])
            except Exception:
                rv = None
            out["remoteVersion"] = rv
            # a newer build is one with another app version; the same version under a new commit (a workflow or
            # documentation change) is nothing to install
            out["status"] = "current" if rv == __version__ else "update"
    except Exception as e:
        out.update(status="error", error=str(e)[:160])
    _checked.clear(); _checked.update(out)
    return out


def last_result() -> dict | None:
    """The outcome of the last update.sh run, from the file it writes before the service restart
    (the viewer's own record dies with the restart): {ok, text, at}."""
    f = config.DATA_DIR / RESULT_FILE
    try:
        text = f.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    return {"ok": text.startswith("ok:"), "text": text.split(":", 1)[-1].strip()[:300], "at": int(f.stat().st_mtime)}


def _app_writable() -> bool:
    """update.sh writes the app folder, the Python environment and (for a checkout) the git metadata."""
    paths = [config.APP_DIR] + [p for p in (config.APP_DIR / ".venv", config.APP_DIR / ".git") if p.exists()]
    return all(os.access(str(p), os.W_OK) for p in paths)


def status() -> dict:
    return {"version": __version__, "installed": local_commit()[:7], "docker": in_docker(), "canRun": (config.APP_DIR / "update.sh").exists() and not in_docker() and _app_writable(),
            "check": dict(_checked) if _checked else None, "progress": dict(progress), "lastUpdate": last_result()}


def run(ref: str = ""):
    """update.sh in a child process; the service manager restarts the viewer when it finishes.
    ref: a commit or branch to install instead of the tip of main (GitHub can still report the
    previous commit for a short while after a push). update.sh does nothing when that commit is
    already installed, and records its outcome in the data folder (see last_result)."""
    if in_docker():
        raise RuntimeError("In Docker, update by pulling the image: docker compose pull && docker compose up -d")
    if progress["running"]:
        raise RuntimeError("An update is already running")
    ref = (ref or "").strip()[:80]
    if ref and not re.fullmatch(r"[0-9a-f]{7,40}|[A-Za-z0-9][A-Za-z0-9._-]*", ref):  # a commit id or a plain branch name, nothing git could read as an option or a path
        raise RuntimeError("ref must be a commit id or a branch name")
    progress.start(log="")
    env = dict(os.environ, IOPIX_RESULT=str(config.DATA_DIR / RESULT_FILE), IOPIX_REPO=REPO, IOPIX_BRANCH=BRANCH)
    if ref:
        env["IOPIX_REF"] = ref
    try:
        (config.DATA_DIR / RESULT_FILE).unlink()
    except OSError:
        pass

    def work():
        try:
            res = subprocess.run(["sh", str(config.APP_DIR / "update.sh")], cwd=str(config.APP_DIR), capture_output=True, text=True, timeout=1800, env=env)
            progress["log"] = (res.stdout + res.stderr)[-3000:]
            if res.returncode != 0:
                tail = re.sub(r"\x1b\[[0-9;]*m", "", (res.stderr or res.stdout)).strip()
                progress.fail(tail.splitlines()[-1] if tail else f"exit {res.returncode}")
        except Exception as e:
            progress.fail(e)
        finally:
            progress.finish()
            _checked.clear()

    threading.Thread(target=work, name="self-update", daemon=True).start()
