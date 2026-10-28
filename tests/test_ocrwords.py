"""Text in photos kept to what reads as words (ocrwords.py): misreadings go, numbers, real words, the library's own
names and words read in several photos stay; the stored text is untouched and search follows the cleaned text."""
import pytest

from app.ai import ocrwords


@pytest.fixture(scope="module")
def known():
    if not ocrwords.dictionary():
        pytest.skip("pyspellchecker not installed here")
    return ocrwords.known_words({"lebanon", "lebo"})


@pytest.mark.parametrize("raw, want", [
    ("scabeyes\nNIVHITY IOWA.\nCITY", "IOWA.\nCITY"),          # a curved sweatshirt
    ("rewheyes\nVNVY  IOIWNA\nCITY", "CITY"),
    ("LEBO\n35\nCANON\nOMIIIN", "LEBO\n35\nCANON"),            # the library's own word, a number, a brand in the lists
    ("MT. LEBANON SOCCER", "MT. LEBANON SOCCER"),               # a short word beside kept ones stays
    ("MT.LE\nSOCI", None),                                       # nothing reads as a word
    ("JACOB SARTORIUS\npepsi\n国", "JACOB SARTORIUS\npepsi"),  # a lone stray character goes
    ("2013/4/7\nOUTGOLN", "2013/4/7"),
    ("Physical Education\n..the best health insurance", "Physical Education\n..the best health insurance"),
])
def test_clean(known, raw, want):
    assert ocrwords.clean(raw, known) == want


def test_words_of_the_library_and_the_corpus():
    assert ocrwords.words_of(["Mt. Lebanon", "Peters Township", None]) == {"lebanon", "peters", "township"}
    assert ocrwords.corpus(["LEBO 35", "lebo", "Lebo!", "BLURB"]) == {"lebo"}


def test_the_index_shows_the_cleaned_text(client, lib):
    if not ocrwords.dictionary():
        pytest.skip("pyspellchecker not installed here")
    from app.ai import analysis
    from app.data import index
    con = index.open_index()
    pid = con.execute("SELECT id FROM photos WHERE video = 0 ORDER BY id LIMIT 1").fetchone()[0]
    con.close()
    db = analysis.connect()
    try:
        before = db.execute("SELECT text FROM ai_text WHERE photo_id = ?", (pid,)).fetchone()
        db.execute("INSERT OR REPLACE INTO ai_text VALUES (?, ?)", (pid, "NIVHITY IOWA.\nscabeyes\nCITY"))
        db.commit()
        index.build()
        assert client.get(f"/api/photo/{pid}").json()["ocr"] == "IOWA.\nCITY"
        assert pid in client.get("/api/photos?q=iowa").json()["id"]
        assert pid not in client.get("/api/photos?q=nivhity").json()["id"]
        assert db.execute("SELECT text FROM ai_text WHERE photo_id = ?", (pid,)).fetchone()[0].startswith("NIVHITY")  # stored as read
    finally:
        if before:
            db.execute("INSERT OR REPLACE INTO ai_text VALUES (?, ?)", (pid, before[0]))
        else:
            db.execute("DELETE FROM ai_text WHERE photo_id = ?", (pid,))
        db.commit()
        db.close()
        index.build()


def test_without_the_word_lists_nothing_is_dropped(monkeypatch):
    """An install without pyspellchecker keeps the text as read rather than dropping every word."""
    from types import SimpleNamespace
    from app.data import index
    monkeypatch.setattr(ocrwords, "dictionary", lambda: {})
    row = [None] * 64
    row[0], row[index._OCR_COL] = 1, "NIVHITY IOWA."
    out = SimpleNamespace(photos=[tuple(row)], fts_rows=[(1, *[None] * 8, "NIVHITY IOWA.", None)])
    index._clean_ocr(out, {}, {})
    assert out.photos[0][index._OCR_COL] == "NIVHITY IOWA." and out.fts_rows[0][9] == "NIVHITY IOWA."
