"""First-start model downloads (AUTO_MODELS): only missing models, one after
another, unknown keys ignored, and the completion hook sees what arrived."""
import threading
import time

from app.ai import models


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_auto_download_sequences_missing_models(monkeypatch):
    installed = {"yolox"}
    order = []
    monkeypatch.setattr(models, "local", lambda key: {"installed": key in installed})

    def fake_download(key):
        if models.progress["running"]:
            raise RuntimeError("busy")
        models.progress.update(running=key, error=None)
        order.append(key)

        def finish():
            time.sleep(0.05)
            installed.add(key)
            models.progress.update(running=None, finished=key)
        threading.Thread(target=finish, daemon=True).start()

    monkeypatch.setattr(models, "download", fake_download)
    done = []
    todo = models.auto_download(["yolox", "geonames", "nonsense", "clip"], on_done=done.append)
    assert todo == ["geonames", "clip"], "installed and unknown keys are skipped"
    assert _wait(lambda: bool(done))
    assert order == ["geonames", "clip"]
    assert done[0] == ["geonames", "clip"]


def test_auto_download_with_nothing_to_do_calls_hook_at_once(monkeypatch):
    monkeypatch.setattr(models, "local", lambda key: {"installed": True})
    done = []
    assert models.auto_download(["yolox"], on_done=done.append) == []
    assert done == [[]]
