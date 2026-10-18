"""Map provider: OpenStreetMap by default; Google Maps when chosen with a key, with the tile
session created server-side and place search going to Google's geocoder; a failing session
falls back to OpenStreetMap with a message; the key is never shown back."""
from app.geo import geocode, maps
from app.data import state


def _reset():
    state.set_setting("map_provider", "osm"); state.set_setting("google_maps_key", ""); maps.reset_session(); geocode._cache.clear()


def test_default_is_openstreetmap(client):
    _reset()
    c = client.get("/api/maps").json()
    assert c["provider"] == "osm" and "openstreetmap" in c["url"] and c["linkLabel"] == "OpenStreetMap"


def test_google_needs_a_key_and_never_shows_it(client):
    _reset()
    assert client.post("/api/settings", json={"map_provider": "google"}).status_code == 200
    assert maps.provider() == "osm", "without a key the provider stays OSM"
    client.post("/api/settings", json={"google_maps_key": "AIza-secret"})
    s = client.get("/api/settings").json() if client.get("/api/settings").status_code == 200 else client.get("/api/maintenance").json()
    assert "AIza-secret" not in str(s) and (s.get("google_maps_key_set") or s.get("googleMapsKeySet"))
    assert client.post("/api/settings", json={"map_provider": "nope"}).status_code == 400
    _reset()


def test_google_tiles_use_a_server_created_session(client, monkeypatch):
    _reset()
    state.set_setting("map_provider", "google"); state.set_setting("google_maps_key", "AIza-secret")
    monkeypatch.setattr(maps, "_create_session", lambda k: {"session": "tok123", "expiry": 9999999999})
    c = client.get("/api/maps").json()
    assert c["provider"] == "google" and "session=tok123" in c["url"] and "key=AIza-secret" in c["url"] and c["linkLabel"] == "Google Maps"
    calls = []
    monkeypatch.setattr(maps, "_create_session", lambda k: calls.append(k) or {"session": "tok456"})
    client.get("/api/maps")
    assert calls == [], "the session is cached until it nears expiry"
    _reset()


def test_google_session_failure_falls_back_to_osm(client, monkeypatch):
    _reset()
    state.set_setting("map_provider", "google"); state.set_setting("google_maps_key", "AIza-secret")
    def boom(k):
        raise OSError("403 forbidden")
    monkeypatch.setattr(maps, "_create_session", boom)
    c = client.get("/api/maps").json()
    assert c["provider"] == "osm" and "403" in c["error"]
    _reset()


def test_place_search_uses_google_when_chosen(client, monkeypatch):
    _reset()
    state.set_setting("map_provider", "google"); state.set_setting("google_maps_key", "AIza-secret")
    monkeypatch.setattr(maps, "geocode", lambda q: [{"name": "Mt Lebanon High School", "detail": "Pittsburgh, PA", "lat": 40.37, "lon": -80.05, "zoom": 16}])
    monkeypatch.setattr(geocode, "_fetch", lambda q: (_ for _ in ()).throw(AssertionError("Nominatim must not be asked")))
    geocode._offline_until[0] = 0
    r = client.get("/api/geocode?q=Mt+Lebanon+High+School").json()
    assert r["source"] == "google" and r["results"][0]["lat"] == 40.37
    _reset()


def test_google_requests_carry_the_viewer_address_as_referer(client, monkeypatch):
    """A key restricted to HTTP referrers (e.g. *.iothing.net/*) rejects requests without one; the
    session and geocoding calls are made by the server, which sends the browser's address."""
    _reset()
    state.set_setting("map_provider", "google"); state.set_setting("google_maps_key", "AIza-secret")
    seen = []
    class R:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self.payload
    import io, json as _json
    def fake_urlopen(req, timeout=0):
        seen.append(dict(req.header_items()))
        body = {"session": "tok789", "expiry": 9999999999} if "createSession" in req.full_url else {"status": "OK", "results": []}
        return io.BytesIO(_json.dumps(body).encode())
    monkeypatch.setattr(maps.urllib.request, "urlopen", fake_urlopen)
    c = client.get("/api/maps", headers={"referer": "https://photo.example.net/"}).json()
    assert c["provider"] == "google" and seen and seen[0].get("Referer") == "https://photo.example.net/"
    maps.geocode("somewhere")
    assert seen[-1].get("Referer") == "https://photo.example.net/"
    _reset()


def test_place_search_falls_back_from_google_to_openstreetmap(monkeypatch):
    """A Google key restricted to browser referrers is refused by Google's server-side APIs: the search
    then answers from OpenStreetMap and says why, instead of finding nothing."""
    from app.geo import geocode, maps
    geocode._cache.clear()
    monkeypatch.setattr(maps, "provider", lambda: "google")
    monkeypatch.setattr(maps, "geocode", lambda q: (_ for _ in ()).throw(RuntimeError("REQUEST_DENIED: API keys with referer restrictions cannot be used with this API.")))
    monkeypatch.setattr(maps, "places_search", lambda q: [])
    monkeypatch.setattr(geocode, "_nominatim", lambda q: [{"name": "Pittsburgh International Airport", "detail": "aerodrome", "lat": 40.49, "lon": -80.23, "zoom": 14}])
    r = geocode.search("Pittsburgh International Airport")
    assert r["source"] == "osm" and r["results"][0]["name"] == "Pittsburgh International Airport"
    assert "referer restrictions" in r["note"]
    geocode._cache.clear()
    monkeypatch.setattr(maps, "geocode", lambda q: [])
    monkeypatch.setattr(maps, "places_search", lambda q: [{"name": "PIT", "detail": "airport", "lat": 1, "lon": 2, "zoom": 15}])
    assert geocode.search("pit airport")["source"] == "google", "Google Places answers named places the geocoder misses"
    geocode._cache.clear()


def test_elevation_endpoint(client, monkeypatch):
    from app.geo import maps
    monkeypatch.setattr(maps, "elevation", lambda lat, lon: {"alt": 367.0, "source": "Open-Meteo", "note": None})
    r = client.get("/api/elevation", params={"lat": 40.49, "lon": -80.23})
    assert r.status_code == 200 and r.json()["alt"] == 367.0
    assert client.get("/api/elevation", params={"lat": 140, "lon": 0}).status_code == 400


def test_location_suggestions_come_from_photos_nearest_in_time(client, lib):
    from app.data import index
    con = index.open_index()
    rows = con.execute("SELECT id, ts FROM photos WHERE lat IS NOT NULL ORDER BY ts").fetchall()
    con.close()
    pid = rows[len(rows) // 2][0]
    r = client.get("/api/location/suggest", params={"id": pid, "hours": 72}).json()
    assert all(s["id"] != pid for s in r["suggestions"])
    mins = [abs(s["minutes"]) for s in r["suggestions"]]
    assert mins == sorted(mins), "nearest in time first"
    spots = [(round(s["lat"], 3), round(s["lon"], 3)) for s in r["suggestions"]]
    assert len(spots) == len(set(spots)), "one suggestion per place"


def test_landmark_endpoint(client, lib, monkeypatch):
    from app.geo import maps
    monkeypatch.setattr(maps, "key", lambda: "k")
    monkeypatch.setattr(maps, "landmarks", lambda jpeg: [{"name": "Eiffel Tower", "score": 0.93, "lat": 48.8584, "lon": 2.2945}] if jpeg[:2] == b"\xff\xd8" else [])
    r = client.post("/api/location/landmark", json={"id": lib["ids"][0]})
    assert r.status_code == 200 and r.json()["landmarks"][0]["name"] == "Eiffel Tower", r.text


def test_maps_config_offers_satellite_and_street_view(client):
    c = client.get("/api/maps").json()
    assert c["satellite"]["url"] and c["satellite"]["attribution"]
    assert "{lat}" in c["streetview"]["link"] and "map_action=pano" in c["streetview"]["link"]
