"""A change made here shows up fast: no full database backup for ordinary changes, the index is rebuilt
straight from digiKam's database, and the viewer's copy of the library follows in the background."""
import time

from app import config
from app.data import index
from app.digikam import library


def _backups():
    d = config.DATA_DIR / "db-backups"
    return sorted(d.glob("digikam4-*.db")) if d.exists() else []


def test_new_person_is_in_the_index_at_once_and_the_copy_follows(client, lib):
    before = _backups()
    index.ensure_fresh()  # start from an index and a copy that match digiKam
    for _ in range(50):
        if not index._copy_lock.locked():
            break
        time.sleep(0.1)
    r = client.post("/api/faces/person", json={"name": "Zz Fast Person", "parent": 0})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert _backups() == before, "creating a person takes no full database backup"
    t0 = time.time()
    index.ensure_fresh()
    assert any(t["id"] == tid for t in client.get("/api/meta").json()["tags"]), "the person is in the index"
    assert time.time() - t0 < 5
    # the copy of the library catches up by itself
    for _ in range(100):
        if not index._copy_lock.locked() and index.mirror_info()["source"] == index.last_source["version"]:
            break
        time.sleep(0.1)
    con = library.connect()
    try:
        assert con.execute("SELECT 1 FROM Tags WHERE id = ?", (tid,)).fetchone(), "the library copy has it too"
    finally:
        con.close()
    from app import main
    main._refresh(initial=True)  # the server's own view of the index, for the tests that follow
