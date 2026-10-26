"""No digiKam at all (DIGIKAM_DB_TYPE=none): a fresh library on a folder of photos nobody catalogued, filled by the
file scanner, with tags, a person and a face, a rating and a caption saved in iopix's own library and the sidecars.
Runs in a process of its own (nodigikam_run.py), since the app reads its environment once; needs no test library."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def run():
    if not shutil.which("exiftool"):
        pytest.skip("needs exiftool")
    tmp = tempfile.mkdtemp(prefix="iopix-nodk-")
    try:
        p = subprocess.run([sys.executable, str(HERE / "nodigikam_run.py"), tmp], capture_output=True, text=True, timeout=300)
        line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert line, f"no result (exit {p.returncode}):\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}"
        return json.loads(line[7:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_starts_a_new_library_and_finds_the_photos(run):
    assert run["error"] is None
    assert run["status"] == {"sync": False, "none": True}
    assert run["scanner_on"], "with no digiKam the file scanner is on by default"
    assert run["scan"] == {"added": 2, "error": None}
    assert run["photos"] == 2
    assert "Trips/Coast" in run["tags"], "keywords already in the files are read"
    assert run["digikam_files"] == [], "no digiKam database is created"


def test_people_faces_and_edits_work_without_digikam(run):
    assert (run["person"], run["face"], run["edit"], run["apply"]) == (200, 200, 200, 200)
    assert run["library"] == {"rating": 4, "caption": ["Low tide"], "faces": 1}
    assert run["pending"] == 0, "edits go into the library, not left waiting for a digiKam database"
    assert run["faces_in_index"] == [1]
    assert run["sidecar"] == {"person": True, "caption": True, "keyword": True}


def test_an_approximate_date_without_digikam(run):
    assert run["approx"] == 200
    day, tags = run["approx_library"]
    assert day == "1992-01-01" and "Year only" in tags, run["approx_library"]


def test_trash_and_restore_without_digikam(run):
    assert run["trash"] == [200, True]
    assert run["restore"] == [200, True, True]
