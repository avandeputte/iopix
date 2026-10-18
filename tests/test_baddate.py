"""Metadata quality: dates that cannot be right (before 1901, in the future, a clock's starting point)."""
import time


def _listed(client, pid):
    return next((x for x in client.get("/api/quality/items?check=baddate&limit=500").json()["items"] if x["photo"] == pid), None)


def test_implausible_dates_are_listed_and_cleared(client, lib):
    pid = lib["ids"][1]
    assert _listed(client, pid) is None
    try:
        for when, reason in (("1900-01-01T00:00:00", "dated 1900"), ("2099-05-01T10:00:00", "in the future"), ("1970-01-01T00:00:00", "default date")):
            client.post("/api/edit", json={"ids": [pid], "changes": {"taken": when}})
            for _ in range(60):
                hit = _listed(client, pid)
                if hit and hit["taken"] == when:
                    break
                time.sleep(0.25)
            assert hit and hit["taken"] == when and reason in hit["reason"], (when, hit)
        q = client.get("/api/quality").json()["baddate"]
        assert client.post("/api/quality/accept", json={"photos": [pid], "check": "baddate"}).json()["accepted"] == 1
        assert client.get("/api/quality").json()["baddate"]["count"] == q["count"] - 1
        client.post("/api/quality/accept", json={"photos": [pid], "check": "baddate", "on": False})
    finally:
        client.post("/api/edit/revert", json={"ids": [pid]})
    for _ in range(60):
        if _listed(client, pid) is None:
            break
        time.sleep(0.25)
    assert _listed(client, pid) is None
