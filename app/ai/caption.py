"""One-sentence descriptions of photos with BLIP (Salesforce; base by default, large as an
option chosen in Maintenance, the setting caption_model), run locally through PyTorch (Apple
GPU when available, else CPU). The model lives in data/models (downloaded from the AI analysis
page or `python -m app.ai.models download blip|blip_large`) and is loaded offline; nothing leaves
the machine."""
import logging
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
        out[i] = st["proc"].decode(g, skip_special_tokens=True).strip()
    return out
