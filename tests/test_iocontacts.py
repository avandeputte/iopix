"""ioContacts: birthdays of the people here, read from the suite's address book (app/iocontacts.py)."""
import base64
import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from iosuite_shared import apppasswords, suite

from app import iocontacts
from app.data import index


def _c(cid, fn, bday=None, pid=None):
    return {"id": cid, "fn": fn, "birthday": bday, "iopixPersonId": pid}


def test_only_the_links_made_in_iocontacts_count():
    persons = {1: "Ann Lee", 2: "Bob Stone"}
    contacts = [
        _c(10, "Annie", {"year": 1980, "month": 3, "day": 4}, pid=1),     # linked in ioContacts: its name does not matter
        _c(11, "Bob Stone", {"year": 1970, "month": 7, "day": 9}),        # the same name, but not linked: no match
        _c(12, "Cy", {"year": 1990, "month": 1, "day": 1}, pid=99),       # linked to a person no longer here
        _c(13, "Bob", None, pid=2),                                       # linked, no birthday
    ]
    m = iocontacts.match(contacts, persons, "https://contacts.example")
    assert m == {1: {"contactId": 10, "name": "Annie", "year": 1980, "month": 3, "day": 4, "url": "https://contacts.example/#/c/10"}}


@pytest.mark.parametrize("taken,dprec,age", [
    ("2020-03-04T10:00:00", None, "40"),      # on the birthday
    ("2020-03-03 10:00:00", None, "39"),      # the day before
    ("1980-08-01T00:00:00", None, "under 1"),
    ("1979-12-31T00:00:00", None, None),      # before they were born
    ("2020-03-01T00:00:00", 1, "39 or 40"),   # known to the month, the birthday's month
    ("2020-06-01T00:00:00", 1, "40"),         # known to the month, another month
    ("2020-01-01T00:00:00", 2, "39 or 40"),   # known to the year
    ("2020-01-01T00:00:00", 3, None),         # date unknown
    (None, None, None),
])
def test_age_in_a_photo(taken, dprec, age):
    assert iocontacts.age_text({"year": 1980, "month": 3, "day": 4}, taken, dprec) == age
    assert iocontacts.age_text({"year": None, "month": 3, "day": 4}, "2020-03-04", None) is None


def test_upcoming_birthdays_today_first_and_across_the_new_year(monkeypatch):
    monkeypatch.setattr(iocontacts, "people", lambda: {
        1: {"year": 1980, "month": 1, "day": 3, "url": None}, 2: {"year": None, "month": 12, "day": 28, "url": None},
        3: {"year": 1990, "month": 6, "day": 1, "url": None}, 4: {"year": 2000, "month": 2, "day": 29, "url": None}})
    up = iocontacts.upcoming(datetime.date(2026, 12, 28))
    assert [(x["tag"], x["in"], x["turns"]) for x in up] == [(2, 0, None), (1, 6, 47)]
    feb = iocontacts.upcoming(datetime.date(2027, 2, 20))
    assert [(x["tag"], x["date"], x["turns"]) for x in feb] == [(4, "2027-02-28", 27)]


class _Fake(BaseHTTPRequestHandler):
    contacts = []
    auth = None
    refuse = False

    def do_GET(self):  # noqa: N802
        _Fake.auth = self.headers.get("Authorization")
        assert "iopix_linked=true" in self.path
        name, _, pw = base64.b64decode((_Fake.auth or "Basic ")[6:]).decode().partition(":")
        if _Fake.refuse or name != "iopix-app" or not apppasswords.authenticate(name, pw, "iocontacts"):
            self.send_response(401); self.end_headers(); return
        body = json.dumps({"total": len(self.contacts), "items": self.contacts}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_contacts():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_settings_fetch_birthdays_and_ages(client, lib, fake_contacts):
    con = index.open_index()
    try:
        row = con.execute("SELECT pt.photo_id, t.id, t.name FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id JOIN photos p ON p.id = pt.photo_id "
                          "WHERE t.person = 1 AND p.taken IS NOT NULL AND p.taken > '1990' LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        pytest.skip("no dated photo with a person in the scratch library")
    photo, tag, name = row
    today = datetime.date.today()
    _Fake.contacts = [_c(7, "someone else", {"year": 1950, "month": today.month, "day": today.day}, pid=tag)]
    url = f"http://127.0.0.1:{fake_contacts.server_port}"
    assert iocontacts.public_settings()["enabled"] is False
    suite.register("iocontacts", name="ioContacts", url=url, version="1")
    try:
        _Fake.refuse = True  # every app password refused: nothing to show, no error
        assert not iocontacts.refresh()["ok"]
        assert client.get("/api/people/birthdays").json()["upcoming"] == []
        _Fake.refuse = False
        assert iocontacts.refresh() == {"ok": True, "contacts": 1, "matched": 1}
        s = iocontacts.public_settings()
        assert s["url"] == url and s["enabled"] and "password" not in str(s) and s["matched"] == 1
        up = client.get("/api/people/birthdays").json()["upcoming"]
        assert up == [{"tag": tag, "month": today.month, "day": today.day, "year": 1950, "turns": today.year - 1950, "in": 0,
                       "date": today.isoformat(), "url": f"{url}/#/c/7"}]
        d = client.get(f"/api/photo/{photo}").json()
        assert d["ages"][str(tag)] == iocontacts.age_text({"year": 1950, "month": today.month, "day": today.day}, d["taken"], d["dprec"])
        from app.faces import ages
        assert ages.known_births()[tag] == 1950
        # ioContacts down: the last good list stays in use, and the error is kept (the server log has it too)
        fake_contacts.shutdown(); fake_contacts.server_close()
        assert not iocontacts.refresh()["ok"]
        assert iocontacts.public_settings()["error"]
        assert client.get("/api/people/birthdays").json()["upcoming"][0]["tag"] == tag
    finally:
        suite.forget("iocontacts")
    assert client.get("/api/people/birthdays").json() == {"enabled": False, "upcoming": []}
    assert "ages" not in client.get(f"/api/photo/{photo}").json()
