"""Florence-2 descriptions (florence.py): the ONNX pipeline runs without PyTorch and writes a paragraph,
the caption job's tidy-up leaves paragraphs alone, and the model is one of the caption choices."""
from pathlib import Path

import pytest

from app.ai import caption, florence, models

# a Hugging Face cache holding onnx-community/Florence-2-base-ft (the files florence.FILES names); skipped when absent
HF = Path("/private/tmp/claude-501/-Users-alex-Pictures/6567d42b-2a2e-47a5-a2fb-61b7e132b951/scratchpad/hf")
np = pytest.importorskip("numpy")


def test_florence_is_a_caption_choice():
    assert "florence2" in models.options("caption_model")
    assert models.REGISTRY["florence2"]["kind"] == "hf" and all(f.endswith((".json", ".txt", ".onnx")) for f in florence.FILES)


def test_no_repeated_trigrams():
    logits = np.zeros(10, np.float32)
    florence._no_repeat([1, 2, 3, 4, 1, 2], logits)  # "1 2" was followed by 3 once: 3 may not follow it again
    assert logits[3] == -np.inf and np.isfinite(np.delete(logits, 3)).all()


def test_tidy_leaves_paragraphs_alone():
    para = "A group of men are playing soccer on a field. There is a large net behind them."
    assert caption.tidy(para) == para
    assert caption.tidy("there is a man that is taking a selfie") == "a man taking a selfie"


def test_florence_describes_a_photo(client, lib, monkeypatch):
    monkeypatch.setattr(models, "MODEL_DIR", HF)
    if not florence.available():
        pytest.skip("Florence-2 files not available here")
    florence.reset()
    from app.data import index
    con = index.open_index()
    path = con.execute("SELECT path FROM photos WHERE video = 0 AND fmt = 'JPG' ORDER BY id LIMIT 1").fetchone()[0]
    con.close()
    text = florence.describe([path])[0]
    assert text and len(text.split()) >= 6 and text[0].isupper()
    assert florence.describe(["/no/such/file.jpg"]) == [None]
    florence.reset()
