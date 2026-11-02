"""parse_changes turns the body of POST /api/edit into the edit edits.apply takes: ranges clamped, texts cut,
dates and positions checked with the messages the editor shows, unknown keys ignored."""
import pytest
from fastapi import HTTPException

from app.routes.editing import MAX_TEMPLATE_FIELD, MAX_TEXT, parse_changes


def _error(raw: dict) -> str:
    with pytest.raises(HTTPException) as e:
        parse_changes(raw)
    assert e.value.status_code == 400
    return e.value.detail


def test_ranges_are_clamped_not_refused():
    assert parse_changes({"rating": 9, "pick": 7, "color": 12}) == {"rating": 5, "pick": 3, "color": 9}
    assert parse_changes({"rating": -1, "pick": -3, "color": "-2"}) == {"rating": 0, "pick": 0, "color": 0}
    assert parse_changes({"rating": "4"}) == {"rating": 4}, "numbers may come as strings"


def test_bad_types_raise():
    with pytest.raises(ValueError):  # the app's bad-input handler turns it into a 400
        parse_changes({"rating": "five"})
    with pytest.raises((TypeError, ValueError)):
        parse_changes({"pick": None})
    assert _error({"shift": "an hour"}) == "Shift must be a number of seconds"
    assert _error({"gps": {"lat": "north", "lon": 4}}) == "Location needs lat and lon"
    assert _error({"gps": {"lat": 51}}) == "Location needs lat and lon"
    assert _error({"gps": {"lat": 51, "lon": 4, "accuracy": "far"}}) == "accuracy is a number of metres"


def test_texts_are_trimmed_and_cut():
    ch = parse_changes({"title": "  Prom  ", "caption": None, "template": {"creator": " Me ", "nope": "x", "city": "y"}})
    assert ch == {"title": "Prom", "caption": "", "template": {"creator": "Me"}}, "unknown template fields are dropped"
    ch = parse_changes({"caption": "x" * (MAX_TEXT + 50), "template": {"creator": "y" * (MAX_TEMPLATE_FIELD + 5)}})
    assert len(ch["caption"]) == MAX_TEXT and len(ch["template"]["creator"]) == MAX_TEMPLATE_FIELD


def test_dates():
    assert parse_changes({"taken": "2015-04-04T12:37:05.123"}) == {"taken": "2015-04-04T12:37:05"}, "fractions are cut"
    assert _error({"taken": "2015-04-04"}) == "Date must be YYYY-MM-DDTHH:MM:SS"
    assert _error({"taken": "yesterday"}) == "Date must be YYYY-MM-DDTHH:MM:SS"
    assert parse_changes({"shift": "3600"}) == {"shift": 3600}
    assert parse_changes({"taken": "", "shift": 0}) == {}, "empty values change nothing"


def test_date_precision():
    assert parse_changes({"date_precision": "year", "taken": "1974"}) == {"date_precision": "year", "date_known": "1974"}
    assert parse_changes({"date_precision": "month", "taken": "1974-06-01T00:00:00"}) == {"date_precision": "month", "date_known": "1974-06-01T00:00:00"}
    assert parse_changes({"date_precision": "unknown"}) == {"date_precision": "unknown", "date_known": ""}
    assert _error({"date_precision": "roughly"}).startswith("date_precision is one of ")
    assert _error({"date_precision": "year", "shift": 60}) == "A date shift and a change of how well the date is known go in separate edits"
    assert _error({"date_precision": "year", "taken": "74"}) == "Date must be YYYY, YYYY-MM or YYYY-MM-DDTHH:MM:SS"


def test_gps():
    ch = parse_changes({"gps": {"lat": 51.123456789, "lon": 4.5, "alt": "12.34", "accuracy": 5e9}})
    assert ch == {"gps": {"lat": 51.1234568, "lon": 4.5, "alt": 12.3, "accuracy": 1_000_000.0}}, "rounded; the accuracy is capped"
    assert parse_changes({"gps": {"lat": 0, "lon": 0, "alt": "", "accuracy": 0}}) == {"gps": {"lat": 0.0, "lon": 0.0, "alt": None}}
    assert parse_changes({"gps": None}) == {"gps": None}, "an empty position removes it"
    assert _error({"gps": {"lat": 91, "lon": 0}}) == "Latitude or longitude out of range"
    assert _error({"gps": {"lat": 0, "lon": -181}}) == "Latitude or longitude out of range"


def test_unknown_keys_are_ignored():
    assert parse_changes({"foo": 1, "add_tags": [3], "remove_tags": [4], "ids": [1]}) == {}, "tags are resolved by the route, not here"
    assert parse_changes({}) == {}
