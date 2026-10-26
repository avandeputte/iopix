"""The read side of the API against the scratch library: status, metadata,
photo lists and details, facets, map, zip planning, and the shapes the
front end relies on."""


def test_status_shape(client):
    s = client.get("/api/status").json()
    for k in ("version", "app", "editing", "dbWrite", "readOnly", "readOnlyLocked", "analysis", "jobs", "pendingEdits"):
        assert k in s, k
    assert s["jobs"]["running"] is None or isinstance(s["jobs"]["running"], str)


def test_meta_lists_tags_and_albums(client, lib):
    m = client.get("/api/meta").json()
    assert m["version"]
    assert any(t["full"].startswith("People") for t in m["tags"])
    assert len(m["albums"]) >= len(lib["albums"]) - 1


def test_photos_compact_columns(client, lib):
    d = client.get("/api/photos").json()
    assert d["count"] == len(d["id"]) == len(lib["ids"])
    assert set(d["id"]) == set(lib["ids"])
    for col in ("ts", "w", "h", "video", "geo", "faces"):
        assert col in d, col
    # video is {index: duration}, geo/faces are index arrays (compact form the grid consumes)
    assert len(d["video"]) == len(lib["videos"]) and all(isinstance(v, int) for v in d["video"].values())
    assert all(isinstance(i, int) for i in d["geo"] + d["faces"])


def test_photo_detail(client, lib):
    pid = lib["with_faces"][0]
    d = client.get(f"/api/photo/{pid}").json()
    assert d["id"] == pid and d["w"] > 0 and d["h"] > 0
    assert "faces" in d and "faces_other" in d and "tags" in d
    assert d["faces"] or d["faces_other"], "a photo chosen for its face regions has none in the detail"
    assert client.get("/api/photo/999999999").status_code == 404


def test_filters_narrow_results(client, lib):
    allp = client.get("/api/photos").json()["count"]
    vids = client.get("/api/photos?type=video").json()["count"]
    assert vids == len(lib["videos"]) < allp
    geo = client.get("/api/photos?geo=1").json()["count"]
    assert 0 < geo <= allp
    faces = client.get("/api/photos?faces=confirmed").json()["count"]
    assert faces <= len(lib["with_faces"])


def test_text_search(client, lib):
    m = client.get("/api/meta").json()
    person = next(t for t in m["tags"] if t.get("person") and t["full"] != "People")
    r = client.get("/api/photos", params={"q": person["name"]})
    d = r.json()
    assert r.status_code == 200 and isinstance(d["count"], int)  # may be zero when the name is not on any kept photo, but must not error


def test_facets_and_map(client):
    f = client.get("/api/facets").json()
    assert isinstance(f, dict) and f
    m = client.get("/api/map").json()
    assert isinstance(m, (dict, list))


def test_zip_refuses_whole_library_and_plans_parts(client):
    r = client.post("/api/zip/plan", data={})
    assert r.status_code in (400, 409, 422)
    r = client.post("/api/zip/plan", data={"type": "video"})
    assert r.status_code == 200
    assert r.json()["parts"]


def test_thumbnail_and_preview_are_served(client, lib):
    pid = next(i for i in lib["ids"] if i not in lib["videos"])
    r = client.get(f"/thumb/{pid}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    r = client.get(f"/preview/{pid}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")


def test_models_and_analysis_endpoints(client):
    assert set(m["key"] for m in client.get("/api/models").json()["models"]) >= {"clip", "yolox", "geonames"}
    a = client.get("/api/analysis").json()
    assert [j["key"] for j in a["jobs"]][:2] == ["thumbs", "embeddings"]
    assert "queue" in a


def test_pwa_files_are_served(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone" and len(m.json()["icons"]) == 3
    assert client.get("/sw.js").status_code == 200 and "javascript" in client.get("/sw.js").headers["content-type"]
    for p in ("/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-512-maskable.png", "/icons/apple-touch-icon.png", "/icons/mark.svg"):
        assert client.get(p).status_code == 200, p
    html = client.get("/").text
    assert 'rel="manifest"' in html and "theme-color" in html
    assert "serviceWorker" in client.get("/app.js").text  # the registration lives in app.js: the only inline script is the theme one (content security policy)


def test_job_rows_expose_progress_visibility(client):
    """A running job can be told from a stuck one: start time, last progress, current file."""
    s = client.get("/api/status").json()
    for k in ("since", "last", "current"):
        assert k in s["thumbs"], k
    a = client.get("/api/analysis").json()
    thumbs = next(j for j in a["jobs"] if j["key"] == "thumbs")
    assert "since" in thumbs and "last" in thumbs and isinstance(thumbs["current"], list)


def test_version_is_single_sourced_and_shown(client):
    import re
    from app import __version__
    toml = re.search(r'^version = "([^"]+)"', open("pyproject.toml").read(), re.M).group(1)
    assert __version__ == toml, "bump app/__init__.py and pyproject.toml together"
    s = client.get("/api/status").json()
    assert s["appVersion"] == __version__ and "commit" in s
    assert client.get("/api/update").json()["version"] == __version__


def test_smart_albums_are_saved_searches(client):
    """A saved search is listed with the viewer's albums and can be renamed and deleted."""
    assert client.post("/api/searches", json={"name": "", "query": "year=2024"}).status_code == 400
    r = client.post("/api/searches", json={"name": "Videos of 2024", "query": "?type=video&year=2024"})
    assert r.status_code == 200
    sid = r.json()["id"]
    s = next(x for x in client.get("/api/state").json()["searches"] if x["id"] == sid)
    assert s["name"] == "Videos of 2024" and s["query"] == "type=video&year=2024", "the leading ? is dropped"
    assert client.post(f"/api/searches/{sid}", json={"name": "Clips"}).status_code == 200
    assert next(x for x in client.get("/api/state").json()["searches"] if x["id"] == sid)["name"] == "Clips"
    assert client.post(f"/api/searches/{sid}", json={"delete": True}).status_code == 200
    assert all(x["id"] != sid for x in client.get("/api/state").json()["searches"])


def test_then_and_now_pairs_people_years_apart(client, lib):
    r = client.get("/api/thenandnow?n=3").json()
    assert "pairs" in r
    for p in r["pairs"]:
        assert p["years"] >= 3 and p["then"]["ts"] <= p["now"]["ts"] and len(p["then"]["rect"]) == 4
        one = client.get(f"/api/thenandnow?person={p['tag']}").json()["pairs"]
        assert one and one[0]["tag"] == p["tag"]


def test_quick_filter_counts_match_the_filters(client):
    """The filter panel's six counts (one pass over the selection) equal what each quick filter then shows, for the
    whole library and within a narrower selection; photos not analysed yet count in none."""
    import sqlite3
    from app import config
    con = sqlite3.connect(config.INDEX_DB)
    ids = [r[0] for r in con.execute("SELECT id FROM photos ORDER BY id LIMIT 12")]
    old = con.execute(f"SELECT id, sharpn, ocr, bright, clip_lo, clip_hi FROM photos WHERE id IN ({','.join(map(str, ids))})").fetchall()
    # analysis results on some photos (the scratch library has none): each value on either side of the thresholds
    for i, pid in enumerate(ids[:10]):
        con.execute("UPDATE photos SET sharpn = ?, ocr = ?, bright = ?, clip_lo = ?, clip_hi = ? WHERE id = ?",
                    ([0.05, 0.9, 0.5][i % 3], ["", "STOP", None][i % 3], [0.1, 0.9, 0.5, None][i % 4], [0.6, 0, None][i % 3], [0.4, 0, None][i % 3], pid))
    con.commit()
    try:
        _quick_counts(client)
    finally:
        con.executemany("UPDATE photos SET sharpn = ?, ocr = ?, bright = ?, clip_lo = ?, clip_hi = ? WHERE id = ?", [(*r[1:], r[0]) for r in old])
        con.commit(); con.close()


def _quick_counts(client):
    years = [y for y, _n in client.get("/api/facets").json()["years"]]
    for base in ("", f"year={years[0]}"):
        f = client.get(f"/api/facets?{base}").json()
        want = {k: client.get(f"/api/photos?{base}&{q}").json()["count"] for k, q in (
            ("blurry", "blurry=1"), ("sharpest", "sharpest=1"), ("hastext", "hastext=1"), ("bursts", "bursts=1"),
            ("dark", "exposure=dark"), ("bright", "exposure=bright"), ("rejects", "rejects=1"), ("looks", "looks=top"), ("eyes", "eyes=closed"),
            *((f"reject_{w}", f"reject={w}") for w in ("blur", "eyes", "burst", "exposure")))}
        assert f["extras"] == want, (base, f["extras"], want)
        if not base:
            assert all(f["extras"][k] for k in ("blurry", "sharpest", "hastext", "dark", "bright")), f["extras"]
