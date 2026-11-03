"""v1.43 restructured the viewer's AI tables: a database in the old shape is migrated the first time it is opened,
every row carried over, a dump kept in db-backups (remove with the migration, after v1.120)."""
import json

import mariadb
from app.data import vdb


def test_old_ai_tables_are_migrated_with_their_rows(tmp_path, monkeypatch):
    cfg = mariadb.fresh_database("iopix_test_143")
    monkeypatch.setattr(vdb.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vdb, "settings", lambda: dict(cfg))
    con = vdb.connect(cfg=cfg)
    try:
        con.executescript("""
            CREATE TABLE ai_quality (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), sharp DOUBLE, bright DOUBLE, clip_lo DOUBLE, clip_hi DOUBLE);
            INSERT INTO ai_quality VALUES (7, 'q7', 0.5, 0.4, 0, 0), (8, 'q8', 0.1, 0.9, 0, 0);
            CREATE TABLE ai_text_and_faces (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), text MEDIUMTEXT, nfaces INT, faceq DOUBLE);
            INSERT INTO ai_text_and_faces VALUES (7, 'v7', 'STOP', 2, 0.8);
            CREATE TABLE ai_objects_scanned (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191)); INSERT INTO ai_objects_scanned VALUES (8, 'o8');
            CREATE TABLE ai_objects (photo_id BIGINT, cls VARCHAR(64), score DOUBLE, x DOUBLE, y DOUBLE, w DOUBLE, h DOUBLE); INSERT INTO ai_objects VALUES (8, 'dog', 0.9, 0, 0, 1, 1);
            CREATE TABLE ai_descriptions (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), text TEXT); INSERT INTO ai_descriptions VALUES (7, 'c7', 'a dog');
            CREATE TABLE ai_faces_scanned (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), found INT, added INT, checked BIGINT);
            INSERT INTO ai_faces_scanned VALUES (7, 'f7', 3, 1, 1700000000);
            CREATE TABLE image_data_checksums (path VARCHAR(500) PRIMARY KEY, hash VARCHAR(128), algorithm VARCHAR(16), verified BIGINT);
            INSERT INTO image_data_checksums VALUES ('/lib/a.jpg', 'abc', 'SHA-256', 1), ('/lib/gone.jpg', 'zzz', 'SHA-256', 1);
            CREATE TABLE metadata_check (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), data MEDIUMTEXT, mismatch INT, has_sidecar INT, file_xmp INT, checked BIGINT);
            INSERT INTO metadata_check VALUES (7, 's', '{"big": "report"}', 0, 1, 0, 1), (8, 's', '{"big": "report"}', 1, 1, 0, 1), (9, 's', '{"big": "report"}', 0, 0, 0, 1);
            CREATE TABLE photo_identity (vid INT AUTO_INCREMENT PRIMARY KEY, dk_id BIGINT, hash VARCHAR(64), size BIGINT, path TEXT, name VARCHAR(255), first_seen BIGINT, last_seen BIGINT);
            INSERT INTO photo_identity (dk_id, hash, size, path, name, first_seen, last_seen) VALUES (7, 'h', 1, '/lib/a.jpg', 'a.jpg', 1, 1)
        """); con.commit()
        assert vdb._old_ai_shape(con)
        vdb.ensure_schema(con)
        tables = vdb.table_names(con)
        assert {"ai_scans", "ai_quality", "ai_text", "ai_face_count", "ai_descriptions", "image_data_checksums"} <= tables
        assert not ({"ai_text_and_faces", "ai_objects_scanned", "ai_faces_scanned", "ai_quality_old"} & tables), "the old shapes are gone"
        scans = {(r[0], r[1]): (r[2], r[3]) for r in con.execute("SELECT photo_id, job, stamp, detail FROM ai_scans")}
        assert scans[(7, "quality")] == ("q7", None) and scans[(8, "quality")][0] == "q8" and scans[(7, "vision")][0] == "v7"
        assert scans[(8, "objects")][0] == "o8" and scans[(7, "captions")][0] == "c7"
        assert scans[(7, "facedetect")][0] == "f7" and json.loads(scans[(7, "facedetect")][1]) == {"found": 3, "added": 1, "checked": 1700000000}
        assert con.execute("SELECT sharp FROM ai_quality WHERE photo_id = 7").fetchone()[0] == 0.5 and "stamp" not in vdb.columns(con, "ai_quality")
        assert con.execute("SELECT text FROM ai_text WHERE photo_id = 7").fetchone()[0] == "STOP"
        assert tuple(con.execute("SELECT nfaces, faceq FROM ai_face_count WHERE photo_id = 7").fetchone()) == (2, 0.8)
        assert con.execute("SELECT text FROM ai_descriptions WHERE photo_id = 7").fetchone()[0] == "a dog"
        assert con.execute("SELECT count(*) FROM ai_objects").fetchone()[0] == 1, "untouched"
        assert [tuple(r) for r in con.execute("SELECT photo_id, hash FROM image_data_checksums")] == [(7, "abc")], "keyed by photo; a path no photo has is dropped"
        assert [r[0] for r in con.execute("SELECT photo_id FROM metadata_check WHERE data IS NULL ORDER BY photo_id")] == [7], "the report stays only where copies disagree or no sidecar exists"
        if vdb.dump_tool():
            backups = list((tmp_path / "db-backups").glob("viewer-before-1.43-*.sql"))
            assert len(backups) == 1 and "ai_text_and_faces" in backups[0].read_text(errors="replace"), "the dump has the old shape"
        # a second open changes nothing
        assert not vdb._old_ai_shape(con)
        vdb.ensure_schema(con)
        if vdb.dump_tool():
            assert len(list((tmp_path / "db-backups").glob("viewer-before-1.43-*.sql"))) == 1
    finally:
        con.close()
