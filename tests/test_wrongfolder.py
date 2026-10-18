"""Metadata quality: photos whose date is outside the date their folder is named for."""
import time

from app.faces.facequality import folder_date


def test_folder_date_parsing():
    assert folder_date("/2009/200909") == (2009, 9, None)
    assert folder_date("/2024/2024-09-07 Birthday") == (2024, 9, 7)
    assert folder_date("/2020/202001") == (2020, 1, None)
    assert folder_date("/2019/Zoo") == (2019, None, None)
    assert folder_date("/2000s/Scans") is None and folder_date("/Phone/1355416_10201") is None


def test_date_outside_its_folder_is_listed_accepted_and_cleared(client, lib):
    con = __import__("app.data.index", fromlist=["open_index"]).open_index()
    try:
        rows = con.execute("SELECT p.id, a.path FROM photos p JOIN albums a ON a.id = p.album_id WHERE p.taken IS NOT NULL").fetchall()
    finally:
        con.close()
    pid, path = next((r[0], r[1]) for r in rows if folder_date(r[1]))
    before = {x["photo"] for x in client.get("/api/quality/items?check=wrongfolder&limit=500").json()["items"]}
    assert pid not in before, "a photo in a folder for its own date is not listed"
    assert client.post("/api/edit", json={"ids": [pid], "changes": {"taken": "1985-01-01T12:00:00"}}).json()["edited"] == 1
    try:
        for _ in range(60):  # the index takes the edit on its next rebuild
            items = client.get("/api/quality/items?check=wrongfolder&limit=500").json()["items"]
            hit = next((x for x in items if x["photo"] == pid), None)
            if hit:
                break
            time.sleep(0.25)
        assert hit and hit["folder"] == path and hit["taken"].startswith("1985-01-01") and hit["expect"][:4] == str(folder_date(path)[0]), hit
        q = client.get("/api/quality").json()["wrongfolder"]
        assert client.post("/api/quality/accept", json={"photos": [pid], "check": "wrongfolder"}).json()["accepted"] == 1
        q2 = client.get("/api/quality").json()["wrongfolder"]
        assert q2["count"] == q["count"] - 1 and q2["accepted"] == q["accepted"] + 1
        client.post("/api/quality/accept", json={"photos": [pid], "check": "wrongfolder", "on": False})
    finally:
        client.post("/api/edit/revert", json={"ids": [pid]})
    for _ in range(60):
        if pid not in {x["photo"] for x in client.get("/api/quality/items?check=wrongfolder&limit=500").json()["items"]}:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("back in its folder's date: off the list")
