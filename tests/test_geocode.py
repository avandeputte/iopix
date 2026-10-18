"""Place search for the location dialog: Nominatim results are reshaped and cached,
a dead connection falls back to the offline towns, and the endpoint validates input."""

from app.geo import geocode


def test_short_query_is_empty(client):
    assert client.get("/api/geocode?q=a").json() == {"results": [], "source": None}


def test_nominatim_results_are_reshaped_and_cached(client, monkeypatch):
    calls = []

    def fake(q):
        calls.append(q)
        return [{"display_name": "Mt. Lebanon High School, 155, Cochran Road, Pittsburgh", "type": "school", "category": "amenity", "lat": "40.3746", "lon": "-80.0515"}]
    monkeypatch.setattr(geocode, "_fetch", fake)
    geocode._cache.clear(); geocode._offline_until[0] = 0  # a failed real lookup earlier in the process must not route us offline
    r = client.get("/api/geocode?q=Mt+Lebanon+High+School").json()
    assert r["source"] == "osm" and r["results"][0]["name"] == "Mt. Lebanon High School"
    assert abs(r["results"][0]["lat"] - 40.3746) < 1e-6 and r["results"][0]["zoom"] == 16
    client.get("/api/geocode?q=mt+lebanon+high+school")
    assert calls == ["Mt Lebanon High School"], "the second, same query comes from the cache"


def test_offline_fallback_uses_geonames_towns(client, monkeypatch, tmp_path):
    def dead(q):
        raise OSError("no network")
    monkeypatch.setattr(geocode, "_fetch", dead)
    monkeypatch.setattr(geocode.places, "DIR", tmp_path)
    (tmp_path / "cities1000.txt").write_text("1\tPittsburgh\tPittsburgh\t\t40.44\t-79.99\tP\tPPL\tUS\t\tPA\t\t\t\t302971\n2\tPittsfield\tPittsfield\t\t42.45\t-73.25\tP\tPPL\tUS\t\tMA\t\t\t\t44737\n", encoding="utf-8")
    geocode._cache.clear(); geocode._offline_until[0] = 0
    r = client.get("/api/geocode?q=Pitts").json()
    assert r["source"] == "offline" and [x["name"] for x in r["results"]] == ["Pittsburgh", "Pittsfield"]
    assert r["results"][0]["detail"] == "PA, US"
    geocode._offline_until[0] = 0
