"""The date report: every date in a file and its sidecar, set against the folder's dates."""
import os
import subprocess

import pytest

from app.photos import datecheck, edits

pytestmark = pytest.mark.skipif(not edits.EXIFTOOL, reason="exiftool not installed")


def test_dates_that_agree_with_the_folder_are_found(tmp_path):
    from PIL import Image
    p = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(p)
    subprocess.run([*edits.exiftool_cmd(), "-q", "-overwrite_original", "-DateTimeOriginal=2005:06:10 10:00:00", "-CreateDate=2019:01:02 10:00:00", str(p)], check=True)
    r = datecheck.report([{"photo": 1, "path": str(p), "taken": "2019-01-02T10:00:00", "start": "2005-06-09", "end": "2005-06-12"}], detail=True)
    assert r["summary"]["some embedded date matches the folder"] == 1
    assert r["summary"]["embedded dates disagree with each other"] == 1
    assert "ExifIFD:DateTimeOriginal" in r["tags_matching_the_folder"]
    assert "ExifIFD:CreateDate" in r["items"][0]["other"]
