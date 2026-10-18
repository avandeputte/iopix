"""People groups: the People page's own sections, in the viewer's database (digiKam is not touched)."""


def test_people_groups_create_fill_order_rename_delete(client, lib):
    con = __import__("app.data.index", fromlist=["open_index"]).open_index()
    try:
        people = [r[0] for r in con.execute("SELECT id FROM tags WHERE person = 1 ORDER BY n_all DESC LIMIT 3")]
        keyword = con.execute("SELECT id FROM tags WHERE person = 0 LIMIT 1").fetchone()[0]
    finally:
        con.close()
    assert len(people) >= 2
    fam = client.post("/api/people/groups", json={"name": "Family"}).json()["id"]
    fr = client.post("/api/people/groups", json={"name": "Friends"}).json()["id"]
    assert client.post("/api/people/groups", json={"name": "  "}).status_code == 400
    # members in the order given; a keyword tag is not a person and drops out; a person may be in two groups
    assert client.post(f"/api/people/groups/{fam}", json={"members": [people[1], keyword, people[0]]}).status_code == 200
    assert client.post(f"/api/people/groups/{fr}", json={"members": [people[0]], "name": "Friends & teammates"}).status_code == 200
    g = {x["id"]: x for x in client.get("/api/people/groups").json()["groups"]}
    assert g[fam]["members"] == [people[1], people[0]] and g[fr]["members"] == [people[0]] and g[fr]["name"] == "Friends & teammates"
    assert [x["id"] for x in client.get("/api/people/groups").json()["groups"]][-2:] == [fam, fr]
    client.post("/api/people/groups/order", json={"ids": [fr, fam]})
    order = [x["id"] for x in client.get("/api/people/groups").json()["groups"]]
    assert order.index(fr) < order.index(fam)
    assert client.post("/api/people/groups/99999", json={"name": "x"}).status_code == 404
    for gid in (fam, fr):
        assert client.delete(f"/api/people/groups/{gid}").status_code == 200
    assert not {fam, fr} & {x["id"] for x in client.get("/api/people/groups").json()["groups"]}
