"""Repairing a video with a damaged wrapper: an iPhone-style file with a stray, broken "hoov" header at the start and
the real moov at the end is rewrapped without re-encoding and plays; the original is kept."""
import os
import shutil
import subprocess

from helpers import need_tool
from app.photos import repair, edits

FF = shutil.which("ffmpeg")
need_tool("ffmpeg", FF, module_level=True)


def _broken(dst):
    good = str(dst) + ".good.mov"
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30", "-t", "2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", good], check=True)
    data = bytearray(open(good, "rb").read())
    _, off, n = next(b for b in repair._boxes(good) if b[0] == "moov")
    real = bytes(data[off:off + n])
    data[off + 4:off + 8] = b"hoov"
    for i in range(off + 40, off + n, 97):
        data[i] ^= 0x5A
    open(dst, "wb").write(data + real)
    os.remove(good)


def test_rewrap_fixes_a_stray_header(tmp_path):
    f = tmp_path / "live.mov"
    _broken(f)
    assert not repair._decodes(FF, str(f)), "the damaged file does not play"
    out, label = repair.rewrap(str(f), str(tmp_path), FF)
    assert repair._decodes(FF, out) and "hoov" in label, label


def test_repairing_a_library_video_in_place(client, lib):
    import sqlite3
    from app.digikam import dbsync
    vid = lib["videos"][0]
    path = dict(dbsync._photo_paths([vid]))[vid]
    keep = path + ".keep"
    shutil.copy2(path, keep)
    try:
        _broken(path)
        r = repair.video(vid, check_running=False)
        assert r["fixed_by"] and repair._decodes(FF, path), r
        assert edits.file_backup_path(path).exists(), "the damaged original is kept"
        con = sqlite3.connect(lib["db"])
        assert con.execute("SELECT fileSize FROM Images WHERE id = ?", (vid,)).fetchone()[0] == os.path.getsize(path)
        con.close()
        assert repair.video(vid, check_running=False)["fixed_by"] is None, "a video that plays is left alone"
    finally:
        shutil.copy2(keep, path)
        os.remove(keep)
        from app.photos import scanner
        from app.digikam import dkdb
        st = dkdb.file_stamp(path)
        con = sqlite3.connect(lib["db"])
        con.execute("UPDATE Images SET uniqueHash = ?, fileSize = ?, modificationDate = ? WHERE id = ?",
                    (scanner.unique_hash(path, scanner.hash_version()), st[1], st[0], vid))
        con.commit(); con.close()
        edits.file_backup_path(path).unlink(missing_ok=True)
