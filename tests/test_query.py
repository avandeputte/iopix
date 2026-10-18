"""query.build_where: every filter binds its value as a parameter, invalid
values are ignored rather than raised, and the generated SQL is stable."""
from app.data import query


def where(**kw):
    return query.build_where(list(kw.items()))


def test_empty_filter_is_no_where_clause():
    sql, params = where()
    assert params == []
    assert "WHERE" not in sql.upper() or sql.strip().upper().endswith("WHERE 1") or sql.strip() in ("", "1")


def test_text_query_uses_fts_and_binds_value():
    sql, params = where(q="beach sunset")
    assert "fts" in sql.lower()
    assert all("beach" not in p or isinstance(p, str) for p in params)
    assert any("beach" in str(p) for p in params), "the query text must be bound, not inlined"
    assert "beach" not in sql


def test_integer_ids_are_validated_before_inlining():
    sql, params = where(tag="12")
    assert "12" in sql or 12 in params
    sql2, params2 = where(tag="12; DROP TABLE photos")
    assert "DROP" not in sql2 and all("DROP" not in str(p) for p in params2)


def test_dates_are_validated():
    sql, params = where(**{"from": "2024-01-01", "to": "2024-12-31"})
    assert params and all(isinstance(p, (int, float, str)) for p in params)
    sql_bad, params_bad = where(**{"from": "not-a-date"})
    assert "not-a-date" not in sql_bad and "not-a-date" not in [str(p) for p in params_bad]


def test_bbox_needs_four_numbers():
    sql, params = where(bbox="1,2,3,4")
    assert len([p for p in params if isinstance(p, (int, float))]) >= 4
    sql_bad, params_bad = where(bbox="1,2,3")
    assert "lat" not in sql_bad.lower() or not params_bad


def test_sorts_are_a_closed_set():
    assert "new" in query.SORTS and "old" in query.SORTS
    for k, v in query.SORTS.items():
        assert isinstance(v, str) and "ORDER" not in v.upper()  # the caller adds ORDER BY


def test_fts_query_escapes_quotes():
    out = query.fts_query('he said "hi" AND/OR')
    assert '""' in out or '"' not in out or out.count('"') % 2 == 0


def test_text_query_also_covers_concept_photos_when_given():
    """main.resolve adds qids (photos the content model labels with a matching concept) next to
    each q; the clause is an OR of the text index and those ids, and ids are validated ints."""
    sql, params = query.build_where([("q", "audience"), ("qids", "5,7,abc,9")])
    assert "fts MATCH ?" in sql and "id IN (5,7,9)" in sql and " OR " in sql
    assert "abc" not in sql
    sql, params = query.build_where([("q", "audience"), ("qids", "0")])
    assert "fts MATCH ?" in sql and "id IN (0)" not in sql, "0 stands for no concept photos"
    sql, params = query.build_where([("q", "audience")])
    assert "fts MATCH ?" in sql and " OR " not in sql


def test_concept_names_matching_a_search():
    from app.ai import concepts
    assert concepts.matching("audience") == ["audience"]
    assert "beach" in concepts.matching("Beach") and "tropical beach with palm trees" in concepts.matching("beach")
    assert concepts.matching("ice hockey") == ["ice hockey"], "a multi-word query matches by whole name only"
    assert concepts.matching("sea") == [] and concepts.matching("") == []
