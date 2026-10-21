"""Search by what is in the picture, using an image-text model that runs locally.

Each photo's grid thumbnail is turned into an embedding; a text query is
embedded the same way and the closest photos win. Models and embeddings are
stored in data/ and nothing is sent anywhere.

Two models are supported, each with its own embeddings file. CLIP ViT-B/32 is
the default (./enable-search.sh). SigLIP 2 base (./enable-search.sh siglip2) is
an alternative the owner can pick on the AI analysis page: on this library it
scored only slightly better and indexes about four times slower. Once chosen it
indexes the library in the background and takes over when it has finished, so
search keeps working on the old index in the meantime.
"""
import importlib
import logging
import os
import threading
import time

from app import config, jobs, thumbs

log = logging.getLogger(__name__)
MODEL_DIR = config.DATA_DIR / "models"
BATCH = 32
ENGINES = {
    "clip": {"image": "Qdrant/clip-ViT-B-32-vision", "text": "Qdrant/clip-ViT-B-32-text",
             "file": "embeddings.npz", "dim": 512, "prompt": "a photo of {}"},
    "siglip2": {"image": "google/siglip2-base-patch16-224", "text": "google/siglip2-base-patch16-224",
                "file": "embeddings-siglip2.npz", "dim": 768, "prompt": "a photo of {}"},
    "jinaclip": {"image": "jinaai/jina-clip-v1", "text": "jinaai/jina-clip-v1",
                 "file": "embeddings-jinaclip.npz", "dim": 768, "prompt": "a photo of {}"},
}

if MODEL_DIR.exists():  # models are already here: never contact the model hub at runtime
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

try:
    import numpy as np
    importlib.import_module("fastembed")  # also proves onnxruntime and friends load on this machine
    _HAVE = True
except Exception:
    np = None
    _HAVE = False


def enabled() -> bool:
    """fastembed is installed, search is not switched off, and at least one model is on disk."""
    return _HAVE and config.SEMANTIC and any(_ready(k) for k in ENGINES)


def _ready(name: str) -> bool:
    """The model's files are in data/models: the marker written after a download, or (for
    clip, the first model, downloaded before markers existed) its hub cache folder."""
    if (MODEL_DIR / f"{name}.ready").exists():
        return True
    return name == "clip" and (MODEL_DIR / "models--Qdrant--clip-ViT-B-32-vision" / "refs" / "main").exists()


def download_models(name: str = "clip"):
    """Fetch a model into data/models. Offline mode is decided when the hub library is
    first imported, so this must run in a process started with HF_HUB_OFFLINE=0
    (enable-search.sh does that)."""
    import huggingface_hub.constants as hub
    if hub.HF_HUB_OFFLINE:
        raise RuntimeError("Start this with HF_HUB_OFFLINE=0 (use ./enable-search.sh) so the model can be downloaded.")
    from fastembed import ImageEmbedding, TextEmbedding
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ImageEmbedding(ENGINES[name]["image"], cache_dir=str(MODEL_DIR))
    TextEmbedding(ENGINES[name]["text"], cache_dir=str(MODEL_DIR))
    (MODEL_DIR / f"{name}.ready").write_text("ok\n")


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


class Engine:
    def __init__(self, name: str):
        self.name = name
        self.cfg = ENGINES[name]
        self.file = config.DATA_DIR / self.cfg["file"]
        self.complete_marker = config.DATA_DIR / (self.cfg["file"] + ".complete")
        self.ids = None       # int32 [n]
        self.vecs = None      # float32 [n, dim], unit length
        self.gen = 0          # bumped whenever the embeddings change
        self.done = self.total = 0
        self.running = False
        self.on_finish = None
        self._text = self._image = None
        self._image_threads = 0
        self._todo_ids = set()
        self.failed = set()   # ids that could not be embedded (no thumbnail, unreadable file); they never block a switch-over
        self._lock = threading.Lock()
        self._run = 0
        self._text_cache = {}

    # ---- storage
    def load(self):
        if self.ids is not None or not enabled():
            return
        if self.file.exists():
            with np.load(self.file) as z:
                self.ids, self.vecs = z["ids"].astype(np.int32), z["vecs"].astype(np.float32)
        else:
            self.ids, self.vecs = np.zeros(0, np.int32), np.zeros((0, self.cfg["dim"]), np.float32)

    def _save(self):
        """Caller holds self._lock. float16 on disk halves the file; vectors are unit length so nothing is lost."""
        tmp = self.file.with_suffix(".tmp.npz")
        np.savez(tmp, ids=self.ids, vecs=self.vecs.astype(np.float16))
        tmp.replace(self.file)

    def _text_model(self):
        if self._text is None:
            from fastembed import TextEmbedding
            self._text = TextEmbedding(self.cfg["text"], cache_dir=str(MODEL_DIR))
        return self._text

    # ---- indexing
    def start(self, photos: list):
        """Embed every photo that has no embedding yet, as a job in the shared background queue."""
        if not enabled():
            return
        self.load()
        self._run += 1
        run = self._run
        with self._lock:  # drop any id embedded twice by overlapping runs
            _, keep = np.unique(self.ids, return_index=True)
            if len(keep) != len(self.ids):
                keep.sort()
                self.ids, self.vecs = self.ids[keep], self.vecs[keep]
                self.gen += 1
        have = set(self.ids.tolist())
        todo = [p for p in photos if p["id"] not in have]
        if self.running and {p["id"] for p in todo} <= self._todo_ids:
            self._run -= 1  # nothing new to embed: let the running job carry on instead of restarting it
            return
        self._todo_ids = {p["id"] for p in todo}
        self.total, self.done = len(todo), 0
        if not todo:
            self.running = False
            if self.on_finish:
                self.on_finish(self)
            return
        self.running = True

        def work():
            try:
                _work()
            finally:
                if run == self._run and self.running and not self._finished_ok:
                    self.running = False  # a job that died must not look alive (the next start would wait for it)

        def _work():
            self._finished_ok = False
            from fastembed import ImageEmbedding
            n = jobs.threads()
            if self._image is None or self._image_threads != n:  # the thread budget is a session option
                self._image = ImageEmbedding(self.cfg["image"], cache_dir=str(MODEL_DIR), threads=n)
                self._image_threads = n
            last_save = time.time()
            for i in range(0, len(todo), BATCH):
                if run != self._run:
                    return
                batch, paths = [], []
                for p in todo[i:i + BATCH]:
                    try:
                        paths.append(str(thumbs.make("t", p)))  # the grid thumbnail is plenty for a 224 px model
                        batch.append(p["id"])
                    except Exception as e:
                        self.failed.add(p["id"])
                        log.warning("embeddings: no thumbnail for %s (%s)", p["path"], e)
                if batch:
                    try:
                        vecs = _unit(list(self._image.embed(paths, batch_size=BATCH)))
                    except Exception as e:
                        # one unreadable image must not cost the whole batch: embed them one by one
                        log.warning("embeddings: batch of %d failed (%s); retrying one at a time", len(batch), e)
                        good, vl = [], []
                        for pid, path in zip(batch, paths):
                            try:
                                vl.append(_unit(list(self._image.embed([path], batch_size=1))))
                                good.append(pid)
                            except Exception as e1:
                                self.failed.add(pid)
                                log.warning("embeddings: cannot embed %s (%s)", path, e1)
                        batch, vecs = good, (np.concatenate(vl) if vl else None)
                    if batch:
                        with self._lock:
                            if run != self._run:
                                return
                            self.ids = np.concatenate([self.ids, np.asarray(batch, np.int32)])
                            self.vecs = np.concatenate([self.vecs, vecs])
                            self.gen += 1
                self.done = min(len(todo), i + BATCH)
                if time.time() - last_save > 60:
                    with self._lock:
                        self._save()
                    last_save = time.time()
            with self._lock:
                self._save()
            if run == self._run:
                self._finished_ok = True
                self.running = False
                if self.on_finish:
                    self.on_finish(self)

        jobs.submit("embeddings", work)  # one background job at a time

    # ---- search
    def _rank(self, query_vec, k: int, exclude=None) -> list:
        with self._lock:
            ids, vecs = self.ids, self.vecs
        if ids is None or not len(ids):
            return []
        with np.errstate(all="ignore"):  # Accelerate emits spurious float warnings on matmul
            scores = vecs @ query_vec
        k = min(k, len(ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [int(ids[i]) for i in top if int(ids[i]) != exclude]

    def search_text(self, text: str, k: int = 300) -> list:
        """Photo ids best matching a description, best first."""
        return self._rank(self.text_vector(text), k)

    def text_vector(self, text: str):
        """Unit embedding of one query, cached: the same searches come back as the user pages."""
        self.load()
        key = text.strip().lower()
        if key not in self._text_cache:
            if len(self._text_cache) > 2000:
                self._text_cache.clear()
            self._text_cache[key] = _unit(next(iter(self._text_model().embed([key]))))
        return self._text_cache[key]

    def text_matrix(self, texts: list):
        """Unit embeddings for many texts at once."""
        return _unit(list(self._text_model().embed([t.lower() for t in texts], batch_size=64)))

    def reset(self):
        """Forget every embedding so the library is indexed again from scratch."""
        self._run += 1
        with self._lock:
            self.ids, self.vecs = np.zeros(0, np.int32), np.zeros((0, self.cfg["dim"]), np.float32)
            self.gen += 1
            self.file.unlink(missing_ok=True)
            self.complete_marker.unlink(missing_ok=True)

    def similar(self, photo_id: int, k: int = 200) -> list:
        """Photo ids that look most like the given photo, most similar first."""
        self.load()
        with self._lock:
            pos = np.nonzero(self.ids == photo_id)[0] if self.ids is not None else []
            vec = self.vecs[pos[0]] if len(pos) else None
        return [photo_id] + self._rank(vec, k + 1, exclude=photo_id) if vec is not None else []


class Semantic:
    """What the rest of the app talks to. Searches go to the active engine; a newly
    installed better engine is filled in the background and then takes over."""

    def __init__(self):
        self.engines = {n: Engine(n) for n in ENGINES}
        self._switch_lock = threading.Lock()

    @staticmethod
    def chosen() -> str:
        """Which model the owner picked. Installing SigLIP 2 does not switch to it:
        that is a deliberate choice (it indexes several times slower than CLIP)."""
        try:
            from app.data import state as appstate
            name = appstate.get_setting("search_model", "clip")
        except Exception:
            name = "clip"
        return name if name in ENGINES and _ready(name) else "clip"

    def _complete(self, e: Engine) -> bool:
        """An index that can answer searches: finished, or CLIP's (the first model, from before markers existed)."""
        return e.complete_marker.exists() or (e.name == "clip" and e.file.exists())

    @property
    def active(self) -> Engine:
        """Searches go to the chosen engine once its index is complete. Until then the newest complete
        index answers, when there is one: on an install that never had another index, the new one
        answers from the start, growing as it is built, rather than nothing at all."""
        new = self.engines[self.chosen()]
        if self._complete(new):
            return new
        others = [e for n, e in self.engines.items() if n != new.name and _ready(n) and self._complete(e)]
        if not others:
            return new
        def age(e):
            try:
                return e.file.stat().st_mtime
            except OSError:  # reset() removed it between the checks
                return 0
        return max(others, key=age)

    def _has_clip(self) -> bool:
        return _ready("clip") and self.engines["clip"].file.exists()

    @property
    def upgrade(self):
        """The newly chosen engine while it is still indexing, else None."""
        new = self.engines[self.chosen()]
        return None if self._complete(new) else new

    def __getattr__(self, name):  # ids, vecs, gen, load, search_text, similar, text_vector, _lock, ...
        return getattr(self.active, name)

    @property
    def key(self) -> str:
        e = self.active
        return f"{e.name}:{e.gen}"

    @property
    def running(self) -> bool:
        return any(e.running for e in self.engines.values())

    @property
    def done(self) -> int:
        e = next((e for e in self.engines.values() if e.running), self.active)
        return e.done

    @property
    def total(self) -> int:
        e = next((e for e in self.engines.values() if e.running), self.active)
        return e.total

    def start(self, photos: list):
        if not enabled():
            return
        up = self.upgrade
        if up is not None:
            def finished(engine):
                engine.load()
                have = set(engine.ids.tolist())
                missing = {p["id"] for p in photos} - have
                # every photo that can be embedded is: switch over. Files that have no thumbnail
                # (a missing or unreadable video, say) would otherwise block the switch forever.
                if missing <= engine.failed:
                    engine.complete_marker.write_text("ok\n")
                    engine.gen += 1
                    log.info("embeddings: %s index complete (%d photos, %d could not be embedded); search uses it now", engine.name, len(have), len(missing))
            up.on_finish = finished
            up.start(photos)
            old = self.active
            if old is not up:
                old.load()      # keep searching the old index meanwhile, without extending it
            return
        self.active.start(photos)


semantic = Semantic()


def remove_ids(ids: list):
    """Deleted photos: drop their embeddings from every engine's file."""
    if not _HAVE or not ids:
        return
    gone = np.array(sorted({int(i) for i in ids}), np.int32)
    for eng in semantic.engines.values():
        with eng._lock:
            if not eng.file.exists():
                continue
            if eng.ids is None:
                with np.load(eng.file) as z:
                    eids, vecs = z["ids"].astype(np.int32), z["vecs"].astype(np.float32)
            else:
                eids, vecs = eng.ids, eng.vecs
            keep = ~np.isin(eids, gone)
            if keep.all():
                continue
            eng.ids, eng.vecs = eids[keep], vecs[keep]
            eng._save()


def remap_ids(remap: dict):
    """digiKam renumbered photos (index.sync_photo_ids): move their embeddings to the new ids in every
    engine's file, so nothing is recomputed. Ids that now belong to another photo are dropped first."""
    if not _HAVE or not remap:
        return
    for eng in semantic.engines.values():
        with eng._lock:
            if not eng.file.exists():
                continue
            if eng.ids is None:
                with np.load(eng.file) as z:
                    ids, vecs = z["ids"].astype(np.int32), z["vecs"].astype(np.float32)
            else:
                ids, vecs = eng.ids, eng.vecs
            targets = np.array(sorted(set(remap.values()) - set(remap)), np.int32)
            keep = ~np.isin(ids, targets) if len(targets) else np.ones(len(ids), bool)
            ids, vecs = ids[keep], vecs[keep]
            lut = {int(k): int(v) for k, v in remap.items()}
            ids = np.array([lut.get(int(i), int(i)) for i in ids], np.int32)
            eng.ids, eng.vecs = ids, vecs
            eng._save()
