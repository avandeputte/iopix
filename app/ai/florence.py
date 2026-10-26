"""Photo descriptions with Microsoft's Florence-2 base (the "-ft" checkpoint, an ONNX export by the
onnx-community), run with onnxruntime on the CPU: no PyTorch needed.

Florence-2 is a small vision-language model (0.23 B parameters) trained on far richer descriptions than
BLIP. Asked for its most detailed caption it writes a short paragraph ("Six people are sitting in a red
canoe on a body of water. The people are wearing white helmets. ..."), which makes far more of a photo
searchable than BLIP's single phrase. The paragraph can still get a small detail wrong (a colour, who
holds what), as any captioning model can.

The pipeline is the model's own: a 768x768 image through the vision encoder, the task prompt's token
embeddings appended, the text encoder over both, then greedy decoding with the merged decoder (its
key/value cache carried between steps), BART's start token, and no repeated 3-grams, as the model's
generation config asks. Text recognition stays with RapidOCR (analysis.py): Florence-2's own OCR task
runs words together.
"""
import logging
import threading

from app import config, jobs, thumbs
from app.ai import models

try:
    import numpy as np
except ImportError:  # optional: the AI packages
    np = None

log = logging.getLogger(__name__)
KEY = "florence2"
TASK = "Describe with a paragraph what is shown in the image."  # the prompt of <MORE_DETAILED_CAPTION>
SIDE = 768
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
START, BOS, EOS = 2, 0, 2   # decoder_start_token_id, forced_bos_token_id, eos_token_id
MAX_TOKENS = 160
PARTS = ("vision_encoder", "embed_tokens", "encoder_model", "decoder_model_merged")
FILES = ["*.json", "merges.txt"] + [f"onnx/{p}.onnx" for p in PARTS]  # what the download keeps of the repository
_lock = threading.Lock()
_state = {}


def _snapshot():
    repo = models.REGISTRY[KEY]["repos"][0]
    sha, _ = models._hf_local(repo)
    if not sha:
        return None
    return models.MODEL_DIR / f"models--{repo.replace('/', '--')}" / "snapshots" / sha


def available() -> bool:
    if np is None:
        return False
    snap = _snapshot()
    if snap is None or not all((snap / "onnx" / f"{p}.onnx").exists() for p in PARTS) or not (snap / "tokenizer.json").exists():
        return False
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("onnxruntime", "tokenizers"))


def download():
    """Runs in models.py's download process (the hub reachable): only the files the pipeline uses."""
    from huggingface_hub import snapshot_download
    models.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_download(models.REGISTRY[KEY]["repos"][0], cache_dir=str(models.MODEL_DIR), allow_patterns=FILES)


def reset():
    with _lock:
        _state.clear()


def _load() -> dict:
    with _lock:
        snap = _snapshot()
        threads = jobs.threads()
        if _state.get("snap") == snap and _state.get("threads") == threads:
            return dict(_state)
        import onnxruntime as ort
        from tokenizers import Tokenizer
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        sess = {p: ort.InferenceSession(str(snap / "onnx" / f"{p}.onnx"), sess_options=opts, providers=["CPUExecutionProvider"]) for p in PARTS}
        tok = Tokenizer.from_file(str(snap / "tokenizer.json"))
        dec = sess["decoder_model_merged"]
        _state.clear()
        _state.update(snap=snap, threads=threads, sess=sess, tok=tok, prompt=np.array([tok.encode(TASK).ids], np.int64),
                      dec_in=[i.name for i in dec.get_inputs()], dec_out=[o.name for o in dec.get_outputs()],
                      layers=sum(1 for i in dec.get_inputs() if i.name.endswith(".decoder.key")),
                      heads=dec.get_inputs()[3].shape[1], head_dim=dec.get_inputs()[3].shape[3])
        return dict(_state)


def _pixels(im):
    from PIL import Image
    a = np.asarray(im.convert("RGB").resize((SIDE, SIDE), Image.Resampling.BICUBIC), dtype=np.float32) / 255.0
    return ((a - np.array(MEAN, np.float32)) / np.array(STD, np.float32)).transpose(2, 0, 1)[None]


def _no_repeat(seq: list, logits):
    """Forbid the token that would repeat a 3-gram already in seq (no_repeat_ngram_size=3)."""
    if len(seq) >= 2:
        a, b = seq[-2], seq[-1]
        for j in range(len(seq) - 2):
            if seq[j] == a and seq[j + 1] == b:
                logits[seq[j + 2]] = -np.inf


def caption(im) -> str:
    st = _load()
    s = st["sess"]
    feats = s["vision_encoder"].run(None, {"pixel_values": _pixels(im)})[0]
    text = s["embed_tokens"].run(None, {"input_ids": st["prompt"]})[0]
    embeds = np.concatenate([feats, text], axis=1)
    mask = np.ones(embeds.shape[:2], np.int64)
    enc = s["encoder_model"].run(None, {"inputs_embeds": embeds, "attention_mask": mask})[0]
    empty = np.zeros((1, st["heads"], 0, st["head_dim"]), np.float32)
    past = {n: empty for n in st["dec_in"] if n.startswith("past_key_values.")}
    out = [START]
    for step in range(MAX_TOKENS):
        e = s["embed_tokens"].run(None, {"input_ids": np.array([[out[-1]]], np.int64)})[0]
        feed = {"encoder_attention_mask": mask, "encoder_hidden_states": enc, "inputs_embeds": e, "use_cache_branch": np.array([step > 0]), **past}
        res = dict(zip(st["dec_out"], s["decoder_model_merged"].run(None, feed)))
        for i in range(st["layers"]):
            for kv in ("key", "value"):
                past[f"past_key_values.{i}.decoder.{kv}"] = res[f"present.{i}.decoder.{kv}"]
                if step == 0:  # the encoder's keys and values are computed once, on the first step
                    past[f"past_key_values.{i}.encoder.{kv}"] = res[f"present.{i}.encoder.{kv}"]
        if step == 0:
            nxt = BOS
        else:
            logits = res["logits"][0, -1].astype(np.float32)
            _no_repeat(out[1:], logits)
            nxt = int(logits.argmax())
        out.append(nxt)
        if nxt == EOS:
            break
    return " ".join(st["tok"].decode(out, skip_special_tokens=True).split())


def describe(paths: list) -> list:
    """One description per path (None where the file could not be read or the model failed on it)."""
    out = []
    for p in paths:
        try:
            im, _ = thumbs._open_oriented(p, 1024, 1024)  # the model looks at 768 px
        except Exception as e:
            log.warning("captions: cannot read %s (%s)", p, e)
            out.append(None)
            continue
        out.append(caption(im))
    return out


def ready_for_config() -> bool:
    """Florence-2 needs no PyTorch: only that descriptions are on (CAPTIONS) and the files are here."""
    return config.CAPTIONS and available()
