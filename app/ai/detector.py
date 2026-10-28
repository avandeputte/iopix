"""Object detection with YOLOX (Apache-2.0, 80 everyday object types), run locally with
onnxruntime. Four sizes of the same network (S, M, L, X; chosen in Maintenance, the setting
object_model) share one decoder. load() returns None until the chosen model's file exists in
data/models, so the app works without it."""
import logging
import threading

from PIL import Image

from app import config
from app.ai import models

log = logging.getLogger(__name__)
SIZE = 640
MIN_SCORE = 0.45
NMS_IOU = 0.45
CLASSES = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
           "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
           "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
           "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
           "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
           "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
           "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
           "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
           "scissors", "teddy bear", "hair drier", "toothbrush"]
_lock = threading.Lock()
_instance = None


def reset():
    """Drop the loaded detector so the next use reads the model files again."""
    global _instance
    with _lock:
        _instance = None


def chosen() -> str:
    return models.chosen("object_model")


def model_path():
    return models.path_for("object_model")


def available() -> bool:
    return model_path().exists()


def download():
    models.fetch_file(models.REGISTRY["yolox"]["url"], models.path("yolox"))


class Detector:
    """One ONNX session plus the anchor grid YOLOX's raw output is decoded against."""

    def __init__(self, threads: int = 2, path=None):
        import numpy as np
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, threads)  # several photos are analysed at once; keep each run small
        self.path = path or model_path()
        self.session = ort.InferenceSession(str(self.path), sess_options=opts, providers=["CPUExecutionProvider"])
        grids, strides = [], []
        for stride in (8, 16, 32):
            n = SIZE // stride
            ys, xs = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
            grids.append(np.stack((xs, ys), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), stride))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.stride = np.concatenate(strides).astype(np.float32)

    def detect(self, im) -> list:
        """im: an upright PIL image. Returns [(class, score, x, y, w, h)] with the box as
        fractions of the image (top-left corner and size), at most 60, best first."""
        import numpy as np
        rgb = im.convert("RGB")
        r = min(SIZE / rgb.width, SIZE / rgb.height)
        nw, nh = max(1, round(rgb.width * r)), max(1, round(rgb.height * r))
        canvas = np.full((SIZE, SIZE, 3), 114, dtype=np.uint8)
        canvas[:nh, :nw] = np.asarray(rgb.resize((nw, nh), Image.Resampling.BILINEAR))
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32)  # BGR, 0-255, as YOLOX expects
        out = self.session.run(None, {"images": blob})[0][0]
        xy = (out[:, :2] + self.grid) * self.stride
        wh = np.exp(np.clip(out[:, 2:4], -10, 10)) * self.stride
        scores = out[:, 4:5] * out[:, 5:]
        cls = scores.argmax(1)
        conf = scores.max(1)
        keep = conf >= MIN_SCORE
        if not keep.any():
            return []
        xy, wh, cls, conf = xy[keep], wh[keep], cls[keep], conf[keep]
        x1, y1 = xy[:, 0] - wh[:, 0] / 2, xy[:, 1] - wh[:, 1] / 2
        x2, y2 = x1 + wh[:, 0], y1 + wh[:, 1]
        order = conf.argsort()[::-1]
        picked = []
        while len(order):  # greedy per-class NMS; a few hundred candidates at most, so a Python loop is fine
            i = order[0]
            picked.append(i)
            rest = order[1:]
            iw = np.clip(np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0, None)
            ih = np.clip(np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0, None)
            inter = iw * ih
            iou = inter / ((x2[i] - x1[i]) * (y2[i] - y1[i]) + (x2[rest] - x1[rest]) * (y2[rest] - y1[rest]) - inter + 1e-9)
            order = rest[(iou < NMS_IOU) | (cls[rest] != cls[i])]
        res = []
        for i in picked[:60]:
            bx1, by1 = max(0.0, x1[i] / nw), max(0.0, y1[i] / nh)
            bx2, by2 = min(1.0, x2[i] / nw), min(1.0, y2[i] / nh)
            if bx2 > bx1 and by2 > by1:
                res.append((CLASSES[int(cls[i])], round(float(conf[i]), 3), round(float(bx1), 4), round(float(by1), 4),
                            round(float(bx2 - bx1), 4), round(float(by2 - by1), 4)))
        return res


def load(threads: int = 2):
    """The shared detector, created on first use with this thread count; None when the
    model is not installed or fails to load (tried again on the next job)."""
    global _instance
    path = model_path()
    if not path.exists() or not config.ANALYSIS:
        return None
    with _lock:
        if _instance is None or _instance.path != path:  # another size was chosen since
            try:
                _instance = Detector(threads, path)
            except Exception as e:
                log.warning("objects: cannot load %s (%s)", path, e)
                return None
    return _instance


if __name__ == "__main__":
    download()
    print("Model saved to", models.path("yolox"))
