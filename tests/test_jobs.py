"""The background job queue: one job at a time, fixed order, resubmission
replaces a queued job, and the core budget stays within bounds."""
import os
import threading
import time

import pytest

from app import jobs


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_unknown_key_is_rejected():
    with pytest.raises(ValueError):
        jobs.submit("not-a-job", lambda: None)


def test_jobs_run_one_at_a_time_in_order():
    log, active, overlap = [], [0], [False]
    gate = threading.Event()

    def job(name, hold=0.05):
        def run():
            active[0] += 1
            if active[0] > 1:
                overlap[0] = True
            gate.wait(2)
            time.sleep(hold)
            log.append(name)
            active[0] -= 1
        return run

    # submit in reverse priority while the worker is held on the first job
    jobs.submit("captions", job("captions"))
    _wait(lambda: jobs.status()["running"] == "captions")
    jobs.submit("vision", job("vision"))
    jobs.submit("thumbs", job("thumbs"))
    jobs.submit("pixels", job("pixels"))
    assert jobs.status()["queued"] == ["thumbs", "pixels", "vision"]
    gate.set()
    assert _wait(lambda: len(log) == 4)
    assert log == ["captions", "thumbs", "pixels", "vision"]
    assert overlap[0] is False


def test_resubmitting_a_queued_key_replaces_it():
    gate = threading.Event()
    seen = []
    jobs.submit("captions", lambda: gate.wait(2))
    _wait(lambda: jobs.status()["running"] == "captions")
    jobs.submit("vision", lambda: seen.append("first"))
    jobs.submit("vision", lambda: seen.append("second"))
    assert jobs.status()["queued"].count("vision") == 1
    gate.set()
    assert _wait(lambda: jobs.status()["running"] is None and not jobs.status()["queued"])
    assert seen == ["second"]


def test_a_failing_job_does_not_stop_the_worker():
    done = []
    jobs.submit("pixels", lambda: 1 / 0)
    jobs.submit("vision", lambda: done.append(1))
    assert _wait(lambda: done == [1])


def test_thread_budget_is_bounded(monkeypatch):
    n = os.cpu_count() or 4
    assert 1 <= jobs.threads() <= max(1, n - 1)
    from app.data import state
    state.set_setting("bg_threads", "999")
    assert jobs.threads() == max(1, n - 1)
    state.set_setting("bg_threads", "0")
    assert jobs.threads() == max(1, min(jobs.config.BG_THREADS, n - 1))


def test_a_burst_of_submissions_is_picked_in_order(monkeypatch):
    """Startup submits several jobs within a second; the worker must not grab the first one
    that arrives (embeddings) ahead of thumbnails."""
    monkeypatch.setattr(jobs, "GRACE", 0.5)
    order = []
    jobs.submit("embeddings", lambda: order.append("embeddings"))
    time.sleep(0.1)
    jobs.submit("thumbs", lambda: order.append("thumbs"))
    jobs.submit("pixels", lambda: order.append("pixels"))
    assert _wait(lambda: len(order) == 3, timeout=5)
    assert order == ["thumbs", "pixels", "embeddings"]
