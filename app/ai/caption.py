"""One-sentence descriptions of photos with BLIP (Salesforce; base by default, large as an
option chosen in Maintenance, the setting caption_model), run locally through PyTorch (Apple
GPU when available, else CPU). The model lives in data/models (downloaded from the AI analysis
page or `python -m app.ai.models download blip|blip_large`) and is loaded offline; nothing leaves
the machine."""
import logging
import re
import threading

from app import config, jobs, thumbs
from app.ai import models

log = logging.getLogger(__name__)
MODEL_DIR = config.DATA_DIR / "models"


def repo() -> str:
    return models.REGISTRY[models.chosen("caption_model")]["repos"][0]

_lock = threading.Lock()
_state = {}  # model, processor, device and the torch module once loaded; models.py clears it after a re-download


def available() -> bool:
    """Whether the model files are on disk (the hub cache layout: models--org--name/snapshots/<rev>/)."""
    if not config.CAPTIONS:
        return False
    try:
        return any(MODEL_DIR.glob(f"models--{repo().replace('/', '--')}/snapshots/*/config.json"))
    except OSError:
        return False


def _load():
    with _lock:
        name = repo()
        if "model" in _state and _state.get("repo") == name:
            return dict(_state)  # a snapshot: a reset or switch meanwhile cannot pull the model from under a running describe()
        _state.clear()  # first load, or another size was chosen since
        import torch
        from transformers import BlipForConditionalGeneration, BlipProcessor
        proc = BlipProcessor.from_pretrained(name, cache_dir=str(MODEL_DIR), local_files_only=True)
        model = BlipForConditionalGeneration.from_pretrained(name, cache_dir=str(MODEL_DIR), local_files_only=True)
        torch.set_num_threads(jobs.threads())  # the CPU side of generation, and all of it without a GPU
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        _state.update(model=model.to(dev).eval(), proc=proc, dev=dev, torch=torch, repo=name)
        return dict(_state)


def describe(paths: list) -> list:
    """One caption per path (None where the file could not be read)."""
    st = _load()
    images, keep = [], []
    for i, p in enumerate(paths):
        try:
            im, _ = thumbs._open_oriented(p, 640, 640)  # the processor resizes to 384 px anyway
            images.append(im.convert("RGB"))
            keep.append(i)
        except Exception as e:
            log.warning("captions: cannot read %s (%s)", p, e)
    out = [None] * len(paths)
    if not images:
        return out
    with st["torch"].no_grad():
        inputs = st["proc"](images=images, return_tensors="pt").to(st["dev"])
        gen = st["model"].generate(**inputs, max_new_tokens=40, num_beams=3, no_repeat_ngram_size=3, repetition_penalty=1.3)
    for i, g in zip(keep, gen):
        out[i] = tidy(st["proc"].decode(g, skip_special_tokens=True))
    return out


# BLIP (large above all) wraps its answer in filler: "there is a man that is taking a selfie",
# "arafed woman", "this is a picture of a dog". The rules below keep the noun phrase and the
# participle; they run on new captions and on the stored ones when the index is built.
_ARTEFACT = re.compile(r"\b(?:ar+af+e?d?|ar+af+[a-z]*|arafly)\b\s*", re.I)  # BLIP large's "araffe", "arafed", "arafly"
_OPENING = re.compile(r"^(?:(?:there|here)(?: is|'s| are)|this is|it is|these are"
                      r"|(?:an? )?(?:image|picture|photo|photograph|view|shot) of"
                      r"|(?:they|there) are(?= (?:an?|the|two|three|four|five|six|several|many|some|\d+)\b))\s+", re.I)
_LOT = re.compile(r"^a lot of ", re.I)  # "a lot of people" -> "many people"
_THEY = re.compile(r"^(?:they|there) are (?=\w+ing\b)", re.I)  # "they are sitting on a couch" -> "people sitting on a couch"
_THAT = re.compile(r"\s(?:that|who|which) (?:is|are)\s(?=\w+ing\b|(?:in|on|at|with|by|near|next|under|over|inside|outside|behind|in front|on top)\b)", re.I)
_IS_ING = re.compile(r"\s(?:is|are)\s(?=\w+ing\b)", re.I)


def tidy(text: str) -> str:
    """A shorter description: "there is a man that is taking a selfie in the mountains" ->
    "a man taking a selfie in the mountains". Leaves anything it does not recognise alone."""
    t = " ".join((text or "").split())
    if not t:
        return t
    if _ARTEFACT.search(t):
        t = re.sub(r"^(?:an? )?" + _ARTEFACT.pattern + r"(?=\w+ing\b)", "someone ", t, flags=re.I)  # "araffe sitting on a bench"
        t = re.sub(r"\ban? " + _ARTEFACT.pattern + r"(?=\w)",
                   lambda m: "an " if t[m.end():m.end() + 1].lower() in "aeiou" else "a ", t, flags=re.I)  # "an arafed man" -> "a man"
        t = _ARTEFACT.sub("", t).strip()
    for _ in range(2):  # "there is an image of a dog"
        t = _OPENING.sub("", t, count=1)
    t = _LOT.sub("many ", t)
    t = _THEY.sub("people ", t)
    t = _THAT.sub(" ", t)
    if not re.match(r"^\w+ing\b", t):  # "a boy is climbing a wall" -> "a boy climbing a wall", once
        t = _IS_ING.sub(" ", t, count=1)
    t = " ".join(t.split())
    return t or " ".join((text or "").split())
