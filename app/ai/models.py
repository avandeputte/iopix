"""The downloadable models and data the viewer uses, in one place: what is
installed (and which revision), whether the source has changed since, and
re-downloading from Settings.

"Changed since" is answered per source. Hugging Face repositories have a
commit id; the one on disk is in the cache's refs/main and the hub's API
gives the current one. Plain downloads (YOLOX, GeoNames) are compared by
Last-Modified. The OCR package is compared with PyPI. None of this can say
whether a *different, better* model exists; the page link on each row is
where to look for that.

Downloads run in a child process with HF_HUB_OFFLINE=0 (the server itself
never talks to the model hub), one at a time. Afterwards the in-process
model objects are dropped so the next use loads the new files.

Plain downloads are pinned to a commit or a release and carry a sha256, so a
file that changed upstream (or in transit) is refused rather than silently
used; moving to a newer file is a change here. installed_revision() names the
installed copy of a model, for data computed from it (embeddings, face
vectors) to notice when the model under it changed.
"""
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version as package_version

from app import config, jobs

MODEL_DIR = config.DATA_DIR / "models"
RECORD = MODEL_DIR / "downloads.json"
# OpenCV's model zoo at a fixed commit (main of 2026-10-20); the sha256 of each file is recorded with it
_ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models"

REGISTRY = {
    # Content search (choice "search_model"; the engines in semantic.py)
    "clip": {"label": "CLIP ViT-B/32", "kind": "hf", "repos": ["Qdrant/clip-ViT-B-32-vision", "Qdrant/clip-ViT-B-32-text"], "choice": "search_model",
             "page": "https://huggingface.co/Qdrant/clip-ViT-B-32-vision", "what": "Content search: the original model. The fastest to index and the smallest; good with short queries (a dog, a beach), weaker with longer descriptions.",
             "size": "~600 MB"},
    "siglip2": {"label": "SigLIP 2 base", "kind": "hf", "repos": ["onnx-community/siglip2-base-patch16-224-ONNX"], "choice": "search_model",
                "page": "https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX", "what": "Content search: Google's newer model, generally more accurate than CLIP, with results that differ from it by subject; about 4 times slower to index.",
                "size": "~1.5 GB"},
    "jinaclip": {"label": "Jina CLIP v1", "kind": "hf", "repos": ["jinaai/jina-clip-v1"], "choice": "search_model",
                 "page": "https://huggingface.co/jinaai/jina-clip-v1", "what": "Content search: a larger CLIP (ViT-B/16 images, a BERT text side) that understands longer, more natural queries.",
                 "size": "~900 MB"},
    # Photo descriptions (choice "caption_model")
    "blip": {"label": "BLIP base (captions)", "kind": "hf", "repos": ["Salesforce/blip-image-captioning-base"], "choice": "caption_model",
             "page": "https://huggingface.co/Salesforce/blip-image-captioning-base", "what": "Photo descriptions: short, plain sentences (\u201ca group of people standing on a beach\u201d). The fastest of the two.", "size": "~950 MB"},
    "blip_large": {"label": "BLIP large (captions)", "kind": "hf", "repos": ["Salesforce/blip-image-captioning-large"], "choice": "caption_model",
                   "page": "https://huggingface.co/Salesforce/blip-image-captioning-large", "what": "Photo descriptions: richer and more accurate sentences, about 3 times slower.", "size": "~1.9 GB"},
    "florence2": {"label": "Florence-2 base (captions)", "kind": "hf", "repos": ["onnx-community/Florence-2-base-ft"], "choice": "caption_model",
                  "page": "https://huggingface.co/microsoft/Florence-2-base-ft", "what": "Photo descriptions: Microsoft's Florence-2 writes a short paragraph about each photo (who is there, what they wear and do, the setting), so far more of it is searchable than with BLIP's single phrase. Runs on the CPU without PyTorch; several times slower than BLIP base, so a first run over a large library takes a long time. Like any captioning model it can get a small detail wrong.",
                  "size": "~1.1 GB"},
    # Object detection (choice "object_model"): the same YOLOX network at four sizes
    "yolox": {"label": "YOLOX-S", "kind": "url", "file": "yolox_s.onnx", "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx", "choice": "object_model",
              "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
              "page": "https://github.com/Megvii-BaseDetection/YOLOX/releases", "what": "Object detection (people, balls, animals, vehicles…): the small, fast model (40 AP on COCO). Misses more small or partly hidden objects than the larger sizes.", "size": "35 MB"},
    "yolox_m": {"label": "YOLOX-M", "kind": "url", "file": "yolox_m.onnx", "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_m.onnx", "choice": "object_model",
                "sha256": "21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f",
                "page": "https://github.com/Megvii-BaseDetection/YOLOX/releases", "what": "Object detection: medium model, noticeably more accurate (47 vs 40 AP on COCO), about 3 times slower.", "size": "101 MB"},
    "yolox_l": {"label": "YOLOX-L", "kind": "url", "file": "yolox_l.onnx", "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_l.onnx", "choice": "object_model",
                "sha256": "7860ae79de6c89a3c1eb72ae9a2756c0ccfbe04b7791bb5880afabd97855a411",
                "page": "https://github.com/Megvii-BaseDetection/YOLOX/releases", "what": "Object detection: large model (50 AP on COCO), about 6 times slower than YOLOX-S.", "size": "217 MB"},
    "yolox_x": {"label": "YOLOX-X", "kind": "url", "file": "yolox_x.onnx", "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_x.onnx", "choice": "object_model",
                "sha256": "c892d7aaf1c4746d8a4d675bec669a4db4f434b4ee1efb654bc9b353379c7c55",
                "page": "https://github.com/Megvii-BaseDetection/YOLOX/releases", "what": "Object detection: the largest model (51 AP on COCO), about 10 times slower than YOLOX-S.", "size": "396 MB"},
    # Face detection (choice "face_detector")
    "yunet": {"label": "YuNet", "kind": "url", "file": "face_detection_yunet_2023mar.onnx", "url": f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx", "choice": "face_detector",
              "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
              "page": "https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet", "what": "Face detection: OpenCV's tiny, fast detector (80% on the hard WIDER Face set). Good on clear, front-facing faces; misses more small, turned or partly hidden ones.", "size": "0.2 MB"},
    "scrfd": {"label": "SCRFD 10G", "kind": "zip", "file": "scrfd_10g.onnx", "url": "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip", "member": "det_10g.onnx", "choice": "face_detector",
              "sha256": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
              "page": "https://github.com/deepinsight/insightface/tree/master/detection/scrfd", "what": "Face detection: InsightFace's detector, clearly better on small, turned and partly hidden faces (95.2% on the hard WIDER Face set vs YuNet's 80%), about 10 times slower. Comes in a 289 MB pack of which this file is kept.", "size": "17 MB (289 MB download)"},
    "retinaface": {"label": "RetinaFace ResNet-50", "kind": "url", "file": "retinaface_resnet50.onnx", "url": "https://github.com/clibdev/Pytorch_Retinaface/releases/download/1.0.0/retinaface-resnet-50.onnx", "choice": "face_detector",
                   "sha256": "a903411b598d92ccc6af5660d875f38783a93031be13643ed4c87851ec165898",
                   "page": "https://github.com/clibdev/Pytorch_Retinaface", "what": "Face detection: the classic heavy detector (95.5% / 94.0% / 84.4% on WIDER Face easy, medium, hard), the slowest of the three.", "size": "104 MB"},
    # Face grouping (choice "face_vectors")
    "sface": {"label": "SFace", "kind": "url", "file": "face_recognition_sface_2021dec.onnx", "url": f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx", "choice": "face_vectors",
              "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
              "page": "https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface", "what": "Face grouping: groups look-alike unnamed faces and suggests who they are. Small and fast, but less sure telling similar-looking people apart (relatives, children) than ArcFace.", "size": "37 MB"},
    "arcface": {"label": "ArcFace ResNet-50", "kind": "zip", "file": "arcface_w600k_r50.onnx", "url": "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip", "member": "w600k_r50.onnx", "choice": "face_vectors",
                "sha256": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
                "page": "https://github.com/deepinsight/insightface/tree/master/model_zoo", "what": "Face grouping: InsightFace's recognition model (trained on WebFace600K), much better at telling people apart; each face is aligned on its eyes, nose and mouth first, with the chosen face detector. About 5 times slower. Comes in a 289 MB pack of which this file is kept.", "size": "174 MB (289 MB download)"},
    # Photo quality (looks.py): an aesthetic score, and open or closed eyes on faces
    "aesthetic": {"label": "LAION aesthetic predictor (CLIP ViT-L/14)", "kind": "url", "file": "clip_vit_l14_vision.onnx",
                  "url": "https://huggingface.co/Xenova/clip-vit-large-patch14/resolve/c307790166907339eed5a9a53a249af534102536/onnx/vision_model.onnx",
                  "sha256": "ff49f8aa57c7abfd26e382eb083e4dbf988505223a9bd3767dbfd4e729206709",
                  "extra": [{"file": "laion_aesthetic_l14.pth", "sha256": "21dd590f3ccdc646f0d53120778b296013b096a035a2718c9cb0d511bff0f1e0",
                             "url": "https://github.com/christophschuhmann/improved-aesthetic-predictor/raw/6934dd81792f086e613a121dbce43082cb8be85e/sac%2Blogos%2Bava1-l14-linearMSE.pth"}],
                  "page": "https://github.com/christophschuhmann/improved-aesthetic-predictor", "what": "Photo quality: a 1–10 score of how appealing a photo looks (light, colour, composition, clarity), from LAION's predictor trained on human ratings, on top of OpenAI's CLIP ViT-L/14 image model. Used to sort by look and to pick the best frame of a burst. It rates pictures, not memories: a dull snapshot of a moment you love can score low. A heavy model: expect a long first run.",
                  "size": "1.2 GB"},
    "eyes": {"label": "Eye state (ViT)", "kind": "url", "file": "eye_state_vit.safetensors",
             "url": "https://huggingface.co/dima806/closed_eyes_image_detection/resolve/8a2180a6c303b7cb42e5a42c43cb8155a6af30a0/model.safetensors",
             "sha256": "4c8ee02a898d927996cca335641a1031605297a0bbec21c69e5dc51baebfe911",
             "page": "https://huggingface.co/dima806/closed_eyes_image_detection", "what": "Photo quality: tells open from closed eyes on faces seen from the front, for the closed-eyes check and the best frame of a burst (a ViT-base classifier trained on eye crops, run without PyTorch). Needs a face detection model to find the eyes. Squints and downward glances can look closed to it.",
             "size": "343 MB"},
    # Year suggestions for undated photos (ages.py)
    "mivolo": {"label": "MiVOLO v2 (age)", "kind": "url", "file": "mivolo_v2_age.onnx",
               "url": "https://huggingface.co/Sam-Apostel/mivolo-v2-age-onnx/resolve/8eb4cd8f5dd4bd28df2c9a43b24bc54d6d139d36/mivolo_v2_age.onnx",
               "sha256": "2db5e05be33b3f120518a86f29a4a65eb0a01c2967219b8787a7dead9b796951",
               "page": "https://huggingface.co/iitolstykh/mivolo_v2", "what": "Year suggestions for photos whose date is unknown: estimates how old each named person looks (MiVOLO v2, one of the most accurate published age estimators; an ONNX export of its age output). Each person's photos with a known date calibrate it for that person. Ages from faces are rough, so a suggestion is a year with a range, and nothing is written until you use it.",
               "size": "118 MB"},
    # Data
    "geonames": {"label": "GeoNames places", "kind": "data", "url": "https://download.geonames.org/export/dump/cities1000.zip",
                 "page": "https://download.geonames.org/export/dump/", "what": "Offline reverse geocoding: town, region and country for each GPS position. GeoNames regenerates this export daily.",
                 "size": "~30 MB"},
    "ocr": {"label": "RapidOCR", "kind": "pip", "package": "rapidocr", "page": "https://pypi.org/project/rapidocr/",
            "what": "Text in photos (signs, jerseys, documents).", "size": "~60 MB"},
}

# Where a feature can run on one of several models: the setting that holds the choice, and the default.
# Switching is a setting; each model is downloaded once from Maintenance. The feature code asks
# chosen() / path_for() so a switch takes effect at the next use (or the next job).
CHOICES = {
    "search_model": {"label": "Content search", "default": "clip"},
    "caption_model": {"label": "Photo descriptions", "default": "blip"},
    "object_model": {"label": "Object detection", "default": "yolox"},
    "face_detector": {"label": "Face detection", "default": "yunet"},
    "face_vectors": {"label": "Face grouping", "default": "sface"},
}


def options(group: str) -> list:
    return [k for k, m in REGISTRY.items() if m.get("choice") == group]


def chosen(group: str) -> str:
    """The model key the owner picked for this feature (a setting), else the default."""
    try:
        from app.data import state
        key = state.get_setting(group, "")
    except Exception:
        key = ""
    return key if key in REGISTRY and REGISTRY[key].get("choice") == group else CHOICES[group]["default"]


def path(key: str):
    """Where a single-file model (kinds url and zip) lives."""
    return MODEL_DIR / REGISTRY[key]["file"]


def files(key: str) -> list:
    """Every file a url or zip model keeps: its main file, then the extra ones it comes with (a head on top of a backbone, say)."""
    return [path(key)] + [MODEL_DIR / x["file"] for x in REGISTRY[key].get("extra", [])]


def path_for(group: str):
    """The file of the model chosen for this feature."""
    return path(chosen(group))


progress = jobs.Progress("models", running=None, finished=None, log="")  # running: the key being downloaded
_checked = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# what is on disk
# --------------------------------------------------------------------------

def _record() -> dict:
    try:
        return json.loads(RECORD.read_text())
    except (OSError, ValueError):
        return {}


def _remember(key: str, revision):
    rec = _record()
    rec[key] = {"at": int(time.time()), "revision": revision}
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(rec, indent=1))


def hf_local(repo: str):
    """(commit id, downloaded time) of a cached repository, or (None, None)."""
    ref = MODEL_DIR / f"models--{repo.replace('/', '--')}" / "refs" / "main"
    try:
        sha = ref.read_text().strip()
        if not any((ref.parent.parent / "snapshots" / sha).iterdir()):
            return None, None
        return sha, int(ref.stat().st_mtime)
    except OSError:
        return None, None


def local(key: str) -> dict:
    """{installed, revision, at}: revision is a commit id, a date or a version."""
    m = REGISTRY[key]
    if m["kind"] == "hf":
        shas, ats = [], []
        for repo in m["repos"]:
            sha, at = hf_local(repo)
            if sha is None:
                return {"installed": False}
            shas.append(sha[:7]); ats.append(at)
        return {"installed": True, "revision": "+".join(shas), "at": max(ats)}
    if m["kind"] in ("url", "zip", "data"):
        if m["kind"] == "data":
            from app.geo import places
            paths = [places.DIR / f for f in places.FILES]
        else:
            paths = files(key)
        if not all(p.exists() for p in paths):
            return {"installed": False}
        at = int(max(p.stat().st_mtime for p in paths))
        return {"installed": True, "revision": datetime.fromtimestamp(at).strftime("%Y-%m-%d"), "at": at}
    if m["kind"] == "pip":
        try:
            return {"installed": True, "revision": package_version(m["package"]), "at": _record().get(key, {}).get("at")}
        except PackageNotFoundError:
            return {"installed": False}
    return {"installed": False}


_hashes = {}  # (path, mtime, size) -> sha256: the model files are large and asked about at every job start


def _sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_sha256(path) -> str:
    """sha256 of a file, remembered until the file changes."""
    st = os.stat(path)
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        if key in _hashes:
            return _hashes[key]
    digest = _sha256_of(path)
    with _lock:
        _hashes[key] = digest
    return digest


def installed_revision(key: str):
    """What identifies the installed copy of a model, for the data computed from it: the full hub commit
    id(s) of a Hugging Face model, the sha256 of a single-file one. None when it is not installed (or
    has no such identity, like the GeoNames export). Vectors of two revisions do not compare, so the
    embeddings file and face_vectors_built record this and are dropped when it no longer matches."""
    m = REGISTRY.get(key)
    if not m:
        return None
    if m["kind"] == "hf":
        shas = []
        for repo in m["repos"]:
            sha, _ = hf_local(repo)
            if sha is None:
                return None
            shas.append(sha)
        return "+".join(shas)
    if m["kind"] in ("url", "zip"):
        try:
            return "+".join(file_sha256(p) for p in files(key))
        except OSError:
            return None
    return None


# --------------------------------------------------------------------------
# what the source has now
# --------------------------------------------------------------------------

def _get(url: str, head: bool = False):
    req = urllib.request.Request(url, method="HEAD" if head else "GET", headers={"User-Agent": "iopix"})
    return urllib.request.urlopen(req, timeout=20)


def check(key: str) -> dict:
    """Ask the source for its current revision and compare with what is installed."""
    m, loc = REGISTRY[key], local(key)
    out = {"key": key, "checked": int(time.time()), "status": "unknown", "remote": None}
    try:
        if m["kind"] == "hf":
            remote, changed = [], False
            for repo in m["repos"]:
                with _get(f"https://huggingface.co/api/models/{repo}") as r:
                    info = json.load(r)
                sha = info.get("sha", "")
                remote.append(sha[:7])
                mine, _ = hf_local(repo)
                changed |= bool(mine) and sha != mine
                out["remote_at"] = info.get("lastModified")
            out["remote"] = "+".join(remote)
            out["status"] = "missing" if not loc["installed"] else ("update" if changed else "current")
        elif m["kind"] in ("url", "zip", "data"):
            with _get(m["url"], head=True) as r:
                lm = r.headers.get("Last-Modified")
            if lm:
                t = parsedate_to_datetime(lm)
                out["remote"] = t.astimezone(timezone.utc).strftime("%Y-%m-%d")
                out["remote_at"] = t.isoformat()
                if not loc["installed"]:
                    out["status"] = "missing"
                elif m["kind"] == "data":
                    out["status"] = "newer" if t.timestamp() > loc["at"] + 86400 else "current"
                else:
                    out["status"] = "update" if t.timestamp() > loc["at"] else "current"
        elif m["kind"] == "pip":
            with _get(f"https://pypi.org/pypi/{m['package']}/json") as r:
                info = json.load(r)
            out["remote"] = info["info"]["version"]
            out["status"] = "missing" if not loc["installed"] else ("update" if _vtuple(out["remote"]) > _vtuple(loc["revision"]) else "current")
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)[:200]
    with _lock:
        _checked[key] = out
    return out


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def check_all() -> list:
    return [check(k) for k in REGISTRY]


def listing() -> dict:
    with _lock:
        checked = dict(_checked)
    rows = []
    for k, m in REGISTRY.items():
        rows.append({"key": k, "label": m["label"], "what": m["what"], "page": m["page"], "size": m["size"], "kind": m["kind"],
                     **local(k), "check": checked.get(k)})
    choices = {g: {"label": c["label"], "chosen": chosen(g), "options": options(g)} for g, c in CHOICES.items()}
    return {"models": rows, "progress": dict(progress), "choices": choices}


# --------------------------------------------------------------------------
# downloading (child process; the server stays offline)
# --------------------------------------------------------------------------

def download(key: str):
    """Start the download in the background. Raises if one is already running."""
    if key not in REGISTRY:
        raise ValueError(key)
    with _lock:
        if progress["running"]:
            raise RuntimeError(f"Already downloading {REGISTRY[progress['running']]['label']}")
        progress.update(running=key, error=None, finished=None, log="")

    def work():
        env = dict(os.environ, HF_HUB_OFFLINE="0", PYTHONUNBUFFERED="1")
        try:
            res = subprocess.run([sys.executable, "-m", "app.ai.models", "download", key], cwd=str(config.APP_DIR), env=env,
                                 capture_output=True, text=True, timeout=7200)
            progress["log"] = (res.stdout + res.stderr)[-2000:]
            if res.returncode != 0:
                raise RuntimeError((res.stderr or res.stdout).strip().splitlines()[-1][:300] if (res.stderr or res.stdout).strip() else f"exit code {res.returncode}")
            _remember(key, local(key).get("revision"))
            reset(key)
            with _lock:
                _checked.pop(key, None)
            if REGISTRY[key].get("choice") in ("search_model", "face_vectors"):
                _recompute(key)
            elif key in ("aesthetic", "eyes", "yunet", "scrfd", "retinaface"):  # photo quality picks up what it could not do before
                _recompute_looks()
            elif key == "mivolo":
                from app.faces import ages
                ages.start()
        except Exception as e:
            progress.fail(e)
        finally:
            progress.update(running=None, finished=key)

    threading.Thread(target=work, name="model-download", daemon=True).start()


def _recompute(key: str):
    """A model whose output is stored (face vectors, embeddings) was downloaded again, possibly another
    revision: start the job that recomputes what the revision check drops, the way core starts it after
    a library change."""
    group = REGISTRY[key].get("choice")
    try:
        if group == "face_vectors":
            from app.faces import facevecs
            facevecs.start()
        elif group == "search_model":
            from app import core
            from app.ai.semantic import semantic
            con = core.db()
            try:
                rows = [dict(r) for r in con.execute(f"SELECT {core.THUMB_COLS} FROM photos ORDER BY ts DESC")]
            finally:
                con.close()
            semantic.start(rows)
    except Exception as e:  # no index yet (a first start): the watcher starts the jobs later
        logging.getLogger(__name__).warning("models: the %s job was not restarted after the download: %s", group, e)


def _recompute_looks():
    """A photo quality model arrived: queue the analysis, which fills in the scores it could not compute before."""
    try:
        from app import core
        core.run_analysis_soon()
    except Exception as e:
        logging.getLogger(__name__).warning("models: photo quality was not restarted after the download: %s", e)


def reset(key: str):
    """Drop the loaded model objects so the next use reads the new files (after a download or a switch)."""
    group = REGISTRY.get(key, {}).get("choice")
    try:
        if group == "search_model":
            from app.ai.semantic import semantic
            e = semantic.engines.get(key)
            if e is not None:
                e.unload()
        elif group == "caption_model":
            from app.ai import caption, florence
            caption.reset()
            florence.reset()
        elif group == "object_model":
            from app.ai import detector
            detector.reset()
        elif group == "face_vectors":
            from app.faces import facevecs
            facevecs.reset_session()
        elif group == "face_detector":
            from app.faces import facedetect
            facedetect.reset_session()
        elif key in ("aesthetic", "eyes"):
            from app.ai import looks
            looks.reset_sessions()
        elif key == "mivolo":
            from app.faces import ages
            ages.reset_session()
        elif key == "geonames":
            from app.geo import places
            places.reset()  # loaded again from the new files on the next lookup
    except Exception:  # the download itself succeeded; the old objects simply stay until a restart
        traceback.print_exc()


def fetch_file(url: str, dst, member: str = None, sha256: str = None):
    """Download one file to dst (through a .part file), or one member of a zip at url. The archive is
    untrusted data: only the named member is read, and only into dst. The file kept (the member, for a
    zip) must have the given sha256; otherwise it is removed and ValueError raised. Without one the
    download is kept unverified, with a warning."""
    import tempfile
    import zipfile
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part")
    try:
        if not member:
            urllib.request.urlretrieve(url, tmp)
        else:
            with tempfile.TemporaryDirectory(dir=str(dst.parent)) as d:
                archive = os.path.join(d, "download.zip")
                urllib.request.urlretrieve(url, archive)
                with zipfile.ZipFile(archive) as z:
                    names = [n for n in z.namelist() if os.path.basename(n) == member and not n.endswith("/")]
                    if not names:
                        raise FileNotFoundError(f"{member} is not in {url}")
                    with z.open(names[0]) as src, open(tmp, "wb") as out:
                        shutil.copyfileobj(src, out)
        if sha256:
            got = _sha256_of(tmp)
            if got != sha256.lower():
                raise ValueError(f"checksum mismatch for {url}: expected {sha256[:12]}…, got {got[:12]}…; the file was not kept")
        else:
            logging.getLogger(__name__).warning("models: no checksum recorded for %s; the download is kept unverified", url)
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _download_here(key: str):
    """Runs in the child process, with the hub reachable."""
    import socket
    socket.setdefaulttimeout(120)  # a stalled connection ends instead of holding the download for the parent's two hours
    m = REGISTRY[key]
    if m.get("choice") == "search_model":
        from app.ai import semantic
        semantic.download_models(key)
    elif key == "florence2":
        from app.ai import florence
        florence.download()
    elif m.get("choice") == "caption_model":
        from transformers import BlipForConditionalGeneration, BlipProcessor
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        BlipProcessor.from_pretrained(m["repos"][0], cache_dir=str(MODEL_DIR))
        BlipForConditionalGeneration.from_pretrained(m["repos"][0], cache_dir=str(MODEL_DIR))
    elif m["kind"] in ("url", "zip"):
        fetch_file(m["url"], path(key), m.get("member"), m.get("sha256"))
        for x in m.get("extra", []):
            fetch_file(x["url"], MODEL_DIR / x["file"], None, x["sha256"])
    elif key == "geonames":
        from app.geo import places
        places.fetch()
    elif key == "ocr":
        # a uv-created venv has no pip; try uv first, then pip (Docker image)
        uv = shutil.which("uv")
        cmd = [uv, "pip", "install", "--upgrade", "-p", sys.executable, "rapidocr"] if uv else [sys.executable, "-m", "pip", "install", "--upgrade", "rapidocr"]
        subprocess.run(cmd, check=True)
    print("done", key)


def auto_download(keys: list, on_done=None):
    """First start of a container or a fresh install: fetch the listed models that are
    missing, one after another, in the background; `on_done(installed_keys)` runs at the
    end so the server can start the jobs that need them. Unknown keys are ignored."""
    todo = [k for k in keys if k in REGISTRY and not local(k)["installed"]]
    if not todo:
        if on_done:
            on_done([])
        return []

    def work():
        got = []
        for key in todo:
            if REGISTRY[key].get("choice") == "caption_model" and key != "florence2" and importlib.util.find_spec("torch") is None:
                continue  # BLIP needs the optional PyTorch install (Florence-2 runs on onnxruntime)
            try:
                download(key)
            except RuntimeError:
                time.sleep(5)  # another download is running (the user clicked one): wait for it
                while progress["running"]:
                    time.sleep(2)
                try:
                    download(key)
                except RuntimeError:
                    continue
            while progress["running"] == key:
                time.sleep(2)
            if not progress["error"] and local(key)["installed"]:
                got.append(key)
        if on_done:
            try:
                on_done(got)
            except Exception as e:
                logging.getLogger(__name__).warning("after the model download: %s", e)

    threading.Thread(target=work, name="auto-models", daemon=True).start()
    return todo


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "download":
        _download_here(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "check":
        for row in check_all():
            print(row)
    else:
        print("usage: python -m app.ai.models download <" + "|".join(REGISTRY) + "> | check")
        sys.exit(2)
