"""Text in photos, kept to what reads as words.

Text recognition on photos reads a lot that is not text, or reads real text wrong: a curved sweatshirt gives
"NIVHITY IOWA.", a pattern gives "OMIIIN" or a stray Chinese character. Shown in the info panel and indexed for
search, that is noise. What the recognition stored is kept as it is; when the search index is built, each line
is reduced to the tokens that read as something:

  numbers and codes      any token with a digit: jersey numbers, years, dates, scores
  dictionary words       English, French, Dutch and German word lists (pyspellchecker, when installed); a
                         short word must be a common one (three letters: very common, four: fairly), since
                         short misreadings often spell some rare word
  the library's words    names of people, tags, folders and places, titles and captions
  the library's text     a word read in three or more photos (a team's name, a school's initials)
  short words            one or two letters, only beside a word that was kept ("MT." in "MT. LEBANON")
  Chinese, Japanese...   a run of two or more different characters

A line with nothing left is dropped. Nothing is written back: a change of these rules (VERSION) rebuilds the
index from the stored text.
"""
import re
import threading
from collections import Counter

VERSION = 1
LANGS = ("en", "fr", "nl", "de")
MIN_FREQ = {3: 2000, 4: 100}  # how common a word of that length must be in its list (the lists count from 50)
CORPUS_MIN = 3  # photos a word must be read in to count as the library's own
_lock = threading.Lock()
_dict = None
_TOKEN = re.compile(r"\S+")
_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def dictionary() -> dict:
    """{word: highest count in any word list} (lower case), loaded once; empty when pyspellchecker is not installed."""
    global _dict
    with _lock:
        if _dict is None:
            words = {}
            try:
                from spellchecker import SpellChecker
                for lang in LANGS:
                    try:
                        for w, n in SpellChecker(language=lang, distance=1).word_frequency.dictionary.items():
                            if w.isalpha() and n > words.get(w, 0):
                                words[w] = n
                    except Exception:
                        continue
            except ImportError:
                pass
            _dict = words
        return _dict


def known_words(library: set) -> tuple:
    """(the word lists, the library's own words): what clean() takes. The lists are not copied (a million words)."""
    return dictionary(), set(library or ())


def words_of(texts) -> set:
    """Lower-case words (3+ letters) in names, titles, captions..."""
    out = set()
    for t in texts:
        if t:
            out.update(w.lower() for w in _LETTERS.findall(str(t)) if len(w) >= 3)
    return out


def corpus(ocr_texts) -> set:
    """Words (4+ letters) read in CORPUS_MIN or more different photos."""
    seen = Counter()
    for t in ocr_texts:
        if t:
            seen.update({w.lower() for w in _LETTERS.findall(t) if len(w) >= 4})
    return {w for w, n in seen.items() if n >= CORPUS_MIN}


def _known(letters: str, known: tuple) -> bool:
    lists, library = known
    w = letters.lower()
    if w in library:
        return True
    n = lists.get(w)
    if n is None and len(w) >= 5 and w.endswith("s"):  # plurals the lists lack
        n = lists.get(w[:-1])
    return n is not None and n >= MIN_FREQ.get(len(w), 0)


def clean(text: str, known: tuple):
    """The recognised text reduced to what reads as words (see the module notes); None when nothing is left."""
    if not text:
        return None
    lines = []
    for line in text.splitlines():
        kept, short = [], []
        for tok in _TOKEN.findall(line):
            if _CJK.search(tok):
                chars = _CJK.findall(tok)
                if len(chars) >= 2 and len(set(chars)) >= 2:
                    kept.append(tok)
                continue
            if any(c.isdigit() for c in tok):
                kept.append(tok)
                continue
            parts = _LETTERS.findall(tok)
            if not parts:
                continue
            long_parts = [p for p in parts if len(p) >= 3]
            if not long_parts:  # "MT." "of" "MT.LE": only beside a word that was kept
                short.append((len(kept), tok))
                continue
            if all(_known(p, known) for p in long_parts):
                kept.append(tok)
        if any(_LETTERS.search(k) for k in kept):  # short words only beside a word that was kept, in their place
            for at, tok in reversed(short):
                kept.insert(at, tok)
        if kept:
            lines.append(" ".join(kept))
    return "\n".join(lines) or None
