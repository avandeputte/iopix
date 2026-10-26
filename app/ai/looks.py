"""Photo quality: how appealing a photo looks, whether its subject is in focus, and closed eyes.

Three measures, each on its own so a missing model only leaves its own column empty:
  aesthetic  LAION's improved aesthetic predictor (a small MLP trained on human 1-10 ratings) on the
             CLIP ViT-L/14 image embedding: a score for light, colour, composition and clarity. It rates
             the picture, not the moment, and leans towards some subjects (scenery over a crowded gym).
  focus      the sharpest part of the frame rather than its average (analysis.measure): the variance
             of a Laplacian in each cell of a 6x6 grid, and the second-best cell kept. A portrait with
             a soft background is in focus; a photo blurred all over is not. No model needed.
  eyes       for each face seen from the front and large enough, the eyes are found with the chosen
             face detector's landmarks and each eye is classified open or closed by a ViT-base eye-state
             model (dima806). A face counts as closed only when both eyes are.

Everything runs on the CPU with onnxruntime. The models are downloaded from Maintenance (models.py,
keys "aesthetic" and "eyes"); analysis.py runs this as the "looks" job and stores the numbers in the
viewer database (ai_looks). Nothing is written to photos, sidecars or digiKam: the index turns the
numbers into filters, a sort and the best frame of each burst.
"""
import collections
import logging
import pickle
import threading
import zipfile

from app.ai import models

try:
    import numpy as np
except ImportError:  # optional: the AI packages
    np = None

log = logging.getLogger(__name__)
_lock = threading.Lock()
_sessions = {}  # "clip" / "eyes" -> onnxruntime session; "head" -> the MLP's layers
SIDE = 224      # CLIP ViT-L/14 input
MEAN = (0.48145466, 0.4578275, 0.40821073)
STD = (0.26862954, 0.26130258, 0.27577711)
EYE_CLOSED = 0.5    # both eyes below this "open" probability: closed
MIN_IOD = 16        # pixels between the eyes in the decoded image: smaller faces are not judged
VERSION = 1         # bump when the focus measure changes meaning, so stored values are computed again
EYES_VERSION = 2    # the same for the closed-eyes check (2: the ViT eye model)


def _ort_ok() -> bool:
    if np is None:
        return False
    import importlib.util
    return importlib.util.find_spec("onnxruntime") is not None


def aesthetic_available() -> bool:
    return _ort_ok() and all(p.exists() for p in models.files("aesthetic"))


def eyes_available() -> bool:
    from app.faces import facedetect
    return np is not None and models.path("eyes").exists() and facedetect.available()


def reset_sessions():
    with _lock:
        _sessions.clear()


def _session(key: str, path, threads: int):
    import onnxruntime as ort
    with _lock:
        s = _sessions.get(key)
        if s is None or s[1] != threads:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, threads)
            s = (ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"]), threads)
            _sessions[key] = s
        return s[0]


# ---- the aesthetic head: a plain PyTorch state dict, read without torch and without running pickled code

_STORAGE = {"FloatStorage": "<f4", "HalfStorage": "<f2", "DoubleStorage": "<f8"}


def read_state_dict(path) -> dict:
    """{name: float32 array} from a .pth zip. Only tensors are accepted: any other pickled object is refused."""
    with zipfile.ZipFile(path) as z:
        pkl = next(n for n in z.namelist() if n.endswith("/data.pkl") or n == "data.pkl")
        base = pkl[: -len("data.pkl")]
        blobs = {}

        def rebuild(storage, offset, size, stride, *_):
            dtype, key = storage
            if key not in blobs:
                blobs[key] = np.frombuffer(z.read(f"{base}data/{key}"), dtype=dtype)
            buf = blobs[key]
            size = tuple(size)
            a = np.lib.stride_tricks.as_strided(buf[offset:], shape=size, strides=[s * buf.itemsize for s in stride]) if size else buf[offset:offset + 1]
            return np.array(a, dtype=np.float32).reshape(size)

        class Reader(pickle.Unpickler):
            def find_class(self, module, name):
                if (module, name) == ("collections", "OrderedDict"):
                    return collections.OrderedDict
                if (module, name) == ("torch._utils", "_rebuild_tensor_v2"):
                    return rebuild
                if module == "torch" and name in _STORAGE:
                    return _STORAGE[name]
                raise pickle.UnpicklingError(f"{module}.{name} is not allowed in a model file")

            def persistent_load(self, pid):
                _kind, dtype, key, _where, _n = pid
                return dtype, key

        with z.open(pkl) as f:
            return dict(Reader(f).load())


def _head():
    with _lock:
        h = _sessions.get("head")
        if h is None:
            sd = read_state_dict(models.files("aesthetic")[1])
            h = [(sd[f"layers.{i}.weight"].T.copy(), sd[f"layers.{i}.bias"]) for i in (0, 2, 4, 6, 7)]  # Linear layers; dropout is off at inference
            _sessions["head"] = h
        return h


def clip_input(im):
    """CLIP's preprocessing: shortest side to 224 (bicubic), centre crop, normalised; (3, 224, 224) float32."""
    from PIL import Image
    im = im.convert("RGB")
    s = SIDE / min(im.size)
    im = im.resize((max(SIDE, round(im.width * s)), max(SIDE, round(im.height * s))), Image.Resampling.BICUBIC)
    left, top = (im.width - SIDE) // 2, (im.height - SIDE) // 2
    a = np.asarray(im.crop((left, top, left + SIDE, top + SIDE)), dtype=np.float32) / 255.0
    return ((a - np.array(MEAN, np.float32)) / np.array(STD, np.float32)).transpose(2, 0, 1)


def aesthetic(batch, threads: int) -> list:
    """Scores (about 1-10) for a list of clip_input arrays."""
    sess = _session("clip", models.path("aesthetic"), threads)
    emb = sess.run(None, {sess.get_inputs()[0].name: np.stack(batch).astype(np.float32)})[0].astype(np.float64)
    x = emb / np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9)
    with np.errstate(all="ignore"):  # macOS's Accelerate BLAS raises spurious matmul warnings; the result is checked below
        for w, b in _head():
            x = x @ w + b
    return [round(float(v), 3) if np.isfinite(v) else None for v in x[:, 0]]


# ---- focus

def focus(im) -> float:
    """The second-sharpest cell of a 6x6 grid over a 1024 px greyscale copy (Laplacian variance): high when
    something in the frame is crisp, even with a soft background; low only when the whole frame is soft."""
    from app.ai.analysis import _laplacian_var
    g = im.convert("L")
    scale = 1024 / max(g.size)
    if scale < 1:
        g = g.resize((max(1, round(g.width * scale)), max(1, round(g.height * scale))))
    a = np.asarray(g, dtype=np.float32)
    h, w = a.shape
    cells = []
    for i in range(6):
        for j in range(6):
            c = a[i * h // 6:(i + 1) * h // 6, j * w // 6:(j + 1) * w // 6]
            if c.shape[0] > 8 and c.shape[1] > 8 and float(c.std()) > 2:  # a flat patch (sky, a wall) says nothing about focus
                cells.append(_laplacian_var(c))
    if not cells:
        return 0.0
    cells.sort(reverse=True)
    return round(float(cells[1] if len(cells) > 1 else cells[0]), 2)


# ---- eyes: a ViT-base eye-state classifier (dima806/closed_eyes_image_detection), run in numpy from its
# safetensors weights so neither PyTorch nor an ONNX export is needed. Compared on hand-labelled face crops
# from real photos, it found about twice as many closed eyes as OpenVINO's open-closed-eye-0001 (used before
# EYES_VERSION 2), with fewer false alarms.

def read_safetensors(path) -> dict:
    """{name: float32 array} from a .safetensors file (a JSON header, then raw little-endian tensors)."""
    import json
    import struct
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        head = json.loads(f.read(n))
        data = f.read()
    kinds = {"F32": "<f4", "F16": "<f2", "BF16": None}
    out = {}
    for name, t in head.items():
        if name == "__metadata__":
            continue
        a, b = t["data_offsets"]
        if kinds.get(t["dtype"]) is None:
            raise ValueError(f"{name}: unsupported dtype {t['dtype']}")
        out[name] = np.frombuffer(data[a:b], dtype=kinds[t["dtype"]]).astype(np.float32).reshape(t["shape"])
    return out


def _vit():
    with _lock:
        w = _sessions.get("eyevit")
        if w is None:
            w = read_safetensors(models.path("eyes"))
            w["_patch"] = w["vit.embeddings.patch_embeddings.projection.weight"].reshape(768, -1).T.copy()
            _sessions["eyevit"] = w
        return w


def _ln(x, g, b, eps=1e-12):
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + eps) * g + b


def _gelu(x):
    """Exact GELU, 0.5 x (1 + erf(x / sqrt 2)), with erf from Abramowitz and Stegun 7.1.26 (error below 2e-7)."""
    z = np.abs(x) * 0.70710678
    t = 1.0 / (1.0 + 0.3275911 * z)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-z * z)
    return 0.5 * x * (1.0 + np.sign(x) * y)


def eye_open(patches: list) -> list:
    """P(open) for each eye patch (PIL images), from the ViT eye-state model: 224 px, normalised to [-1, 1]."""
    from PIL import Image
    w = _vit()
    x = np.stack([(np.asarray(p.convert("RGB").resize((224, 224), Image.Resampling.BILINEAR), dtype=np.float32) / 127.5 - 1.0) for p in patches])
    n = len(patches)
    # 16x16 patches, flattened channel-first as the conv weight is laid out
    t = x.reshape(n, 14, 16, 14, 16, 3).transpose(0, 1, 3, 5, 2, 4).reshape(n, 196, 768)
    with np.errstate(all="ignore"):  # macOS's Accelerate BLAS raises spurious matmul warnings
        t = t @ w["_patch"] + w["vit.embeddings.patch_embeddings.projection.bias"]
        t = np.concatenate([np.repeat(w["vit.embeddings.cls_token"], n, 0), t], 1) + w["vit.embeddings.position_embeddings"]
        for i in range(12):
            L = f"vit.encoder.layer.{i}."
            hdn = _ln(t, w[L + "layernorm_before.weight"], w[L + "layernorm_before.bias"])
            q, k, v = (hdn @ w[L + f"attention.attention.{m}.weight"].T + w[L + f"attention.attention.{m}.bias"] for m in ("query", "key", "value"))
            q, k, v = (a.reshape(n, 197, 12, 64).transpose(0, 2, 1, 3) for a in (q, k, v))
            att = q @ k.transpose(0, 1, 3, 2) / 8.0
            att = np.exp(att - att.max(-1, keepdims=True))
            att /= att.sum(-1, keepdims=True)
            ctx = (att @ v).transpose(0, 2, 1, 3).reshape(n, 197, 768)
            t = t + ctx @ w[L + "attention.output.dense.weight"].T + w[L + "attention.output.dense.bias"]
            hdn = _ln(t, w[L + "layernorm_after.weight"], w[L + "layernorm_after.bias"])
            hdn = _gelu(hdn @ w[L + "intermediate.dense.weight"].T + w[L + "intermediate.dense.bias"])
            t = t + hdn @ w[L + "output.dense.weight"].T + w[L + "output.dense.bias"]
        cls = _ln(t[:, 0], w["vit.layernorm.weight"], w["vit.layernorm.bias"])
        logits = cls @ w["classifier.weight"].T + w["classifier.bias"]
    e = np.exp(logits - logits.max(-1, keepdims=True))
    return [float(p) for p in (e / e.sum(-1, keepdims=True))[:, 1]]  # labels: 0 closeEye, 1 openEye


def eyes(im, faces: list, full_w: int, full_h: int) -> tuple:
    """(faces judged, faces with both eyes closed) over the given face regions (upright full-size pixels).
    A face is judged only when the detector finds it again, seen from the front, with the eyes far enough
    apart to see; the others are left out of both counts."""
    from app.faces import facedetect
    sx, sy = im.width / max(full_w or im.width, 1), im.height / max(full_h or im.height, 1)
    judged = closed = 0
    for x, y, w, h in faces:
        m = 0.35
        box = (max(0, int((x - m * w) * sx)), max(0, int((y - m * h) * sy)),
               min(im.width, int((x + w + m * w) * sx)), min(im.height, int((y + h + m * h) * sy)))
        if box[2] - box[0] < 40 or box[3] - box[1] < 40:
            continue
        crop = im.crop(box)
        found = facedetect.detect_image(crop, landmarks=True)
        cx, cy = crop.width / 2, crop.height / 2
        near = [f for f in found if abs(f[0] + f[2] / 2 - cx) < crop.width * 0.25 and abs(f[1] + f[3] / 2 - cy) < crop.height * 0.25]
        if not near:
            continue
        f = max(near, key=lambda f: f[2] * f[3])
        (lx, ly), (rx, ry), nx = f[5][0], f[5][1], f[5][2][0]
        iod = ((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5
        # from the front: the eyes well apart for the face's width and the nose between them
        if iod < MIN_IOD or iod < 0.33 * f[2] or not (min(lx, rx) + 0.2 * iod < nx < max(lx, rx) - 0.2 * iod):
            continue
        judged += 1
        r = iod * 0.3
        both = [crop.crop((int(ex - r), int(ey - r), int(ex + r), int(ey + r))) for ex, ey in ((lx, ly), (rx, ry))]
        if max(eye_open(both)) < EYE_CLOSED:
            closed += 1
    return judged, closed
