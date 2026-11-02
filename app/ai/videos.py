"""Videos: a better cover frame, chapter thumbnails, and what is said in them.

  cover     the frame that stands for the video in the grid. Instead of the frame a quarter of the way in, a
            handful of frames spread over the video are scored (sharp, not too dark or blown out, a face in it
            when the face detector is installed) and the best one is kept; the thumbnail and preview are made
            from it.
  chapters  where the video changes: the scene cuts ffmpeg finds, or, in a single take, moments spread evenly;
            each with a thumbnail (/video/<id>/frame?t=), shown under the player to jump to.
  speech    a transcript of what is said (onnx-asr: a speech model run with onnxruntime on the CPU), searchable
            from the search box and shown in the info panel with the moments it was said, to jump to. A model is
            downloaded from Maintenance (models.py, choice "speech_model"); without one this part is skipped.

One background job ("videos", jobs.py) does the three for every video not yet done, or done with an older file or
model; the results live in the viewer database (video_info). Nothing is written to any video or to digiKam.
"""
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time

from app import config, jobs, thumbs
from app.data import vdb
from app.digikam.dkdb import chunked

try:
    import numpy as np
except ImportError:  # optional: the AI packages
    np = None

log = logging.getLogger(__name__)
SAMPLES = 7          # frames scored for the cover
MAX_CHAPTERS = 12
MIN_CHAPTER_GAP = 3.0  # seconds between chapters
SCENE = 0.3          # ffmpeg scene-change score that counts as a cut
MAX_SPEECH_S = 20 * 60  # a transcript covers at most this much of a video
VERSION = 1          # bump when the cover or chapter rules change meaning
progress = jobs.Progress("videos", phase="")
_asr = {}
_lock = threading.Lock()
_PTS = re.compile(r"pts_time:([0-9.]+)")


def available() -> bool:
    return bool(thumbs.FFMPEG) and np is not None


def speech_available() -> bool:
    """A speech model is installed and the recogniser package with it."""
    import importlib.util
    from app.ai import models
    if importlib.util.find_spec("onnx_asr") is None:
        return False
    try:
        return models.installed(models.chosen("speech_model"))
    except Exception:
        return False


# --------------------------------------------------------------------------
# ffmpeg helpers (the file is only read)
# --------------------------------------------------------------------------

def _frame(src: str, t: float, height: int = 480):
    """One frame at `t` seconds as a PIL image (RGB), or None."""
    from PIL import Image
    import io
    cmd = [thumbs.FFMPEG, "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-frames:v", "1",
           "-vf", f"scale=-2:'min({height},ih)'", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return None
    if p.returncode or not p.stdout:
        return None
    try:
        return Image.open(io.BytesIO(p.stdout)).convert("RGB")
    except Exception:
        return None


def scene_cuts(src: str, duration: float) -> list:
    """Seconds at which ffmpeg sees a scene change, at most MAX_CHAPTERS, MIN_CHAPTER_GAP apart."""
    cmd = [thumbs.FFMPEG, "-v", "info", "-nostdin", "-i", src, "-vf", f"select='gt(scene,{SCENE})',showinfo", "-vsync", "vfr", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(120, int(duration or 60) * 2))
    except (subprocess.SubprocessError, OSError):
        return []
    out = []
    for m in _PTS.finditer(p.stderr or ""):
        t = float(m.group(1))
        if t >= 1.0 and (not out or t - out[-1] >= MIN_CHAPTER_GAP):
            out.append(round(t, 2))
    return out[:MAX_CHAPTERS]


def _score(im, faces) -> float:
    """How good a frame is as the cover: sharpness (log), a penalty for very dark or blown-out frames, and a face."""
    from app.ai.analysis import _laplacian_var
    g = np.asarray(im.convert("L").resize((max(16, im.width * 512 // max(im.width, im.height)), max(16, im.height * 512 // max(im.width, im.height)))), np.float32)
    sharp = float(np.log1p(_laplacian_var(g)))
    mean = float(g.mean() / 255)
    dark = max(0.0, 0.18 - mean) * 10 + float((g < 8).mean()) * 2
    blown = max(0.0, mean - 0.85) * 10 + float((g > 247).mean()) * 2
    n = 0
    if faces:
        try:
            n = len([f for f in faces(im) if f[2] * f[3] >= 0.004 * im.width * im.height])
        except Exception:
            n = 0
    return sharp - dark - blown + 1.5 * min(n, 2)


def pick_cover(src: str, duration: float, faces=None) -> tuple:
    """(seconds, score) of the best of SAMPLES frames spread over the video (never the first half second)."""
    d = duration or 0
    times = [max(0.5, d * (k + 0.5) / SAMPLES) for k in range(SAMPLES)] if d > 2 else [min(1.0, d / 2) if d else 1.0]
    best = None
    for t in times:
        im = _frame(src, t, 480)
        if im is None:
            continue
        s = _score(im, faces)
        if best is None or s > best[1]:
            best = (round(t, 2), round(s, 3))
    return best or (0.0, 0.0)


def chapters_for(src: str, duration: float) -> list:
    """[seconds]: the scene cuts, else moments spread evenly (none for a very short video)."""
    cuts = scene_cuts(src, duration)
    if len(cuts) >= 2:
        return cuts
    d = duration or 0
    if d < 12:
        return []
    n = min(MAX_CHAPTERS, max(3, int(d // 15)))
    return [round(d * (k + 0.5) / n, 2) for k in range(n)]


# --------------------------------------------------------------------------
# speech
# --------------------------------------------------------------------------

def reset_sessions():
    with _lock:
        _asr.clear()


def _recogniser():
    """The loaded speech model, once; None when it cannot be loaded."""
    from app.ai import models
    key = models.chosen("speech_model")
    with _lock:
        s = _asr.get("s")
        if s is not None and s[1] == key:
            return s[0]
        try:
            import onnxruntime as ort
            import onnx_asr
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = jobs.threads()
            entry = models.REGISTRY[key]
            m = onnx_asr.load_model(entry["asr"], str(models.asr_dir(key)), quantization=entry.get("quantization"),
                                    sess_options=opts, providers=["CPUExecutionProvider"]).with_timestamps()
            _asr["s"] = (m, key)
            return m
        except Exception as e:
            log.warning("videos: the speech model could not be loaded (%s)", e)
            _asr["s"] = (None, key)
            return None


def transcribe(src: str, duration: float) -> dict:
    """{text, segments: [{t, end, text}], lang} from the video's sound, or None without a model or sound."""
    rec = _recogniser()
    if rec is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        cmd = [thumbs.FFMPEG, "-v", "error", "-nostdin", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-t", str(MAX_SPEECH_S), "-f", "wav", wav]
        p = subprocess.run(cmd, capture_output=True, timeout=600)
        if p.returncode or not os.path.exists(wav) or os.path.getsize(wav) < 32000:  # no sound track, or under a second of it
            return {"text": "", "segments": [], "lang": None}
        r = rec.recognize(wav)
    except Exception as e:
        log.warning("videos: speech failed on %s (%s)", src, e)
        return None
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    segments = []
    text = getattr(r, "text", r if isinstance(r, str) else "") or ""
    ts, tokens = getattr(r, "timestamps", None), getattr(r, "tokens", None)
    if ts and tokens:  # word-level times from the recogniser, grouped into lines of about ten seconds
        cur, start, last = [], None, None
        for tok, t in zip(tokens, ts):
            if start is None:
                start = t
            if cur and (t - start > 10 or (last is not None and t - last > 2.0)):
                segments.append({"t": round(start, 2), "end": round(last, 2), "text": "".join(cur).strip()})
                cur, start = [], t
            cur.append(tok)
            last = t
        if cur:
            segments.append({"t": round(start, 2), "end": round(last, 2), "text": "".join(cur).strip()})
    text = clean_text(text)
    segments = [s for s in segments if clean_text(s["text"])]
    return {"text": text, "segments": segments, "lang": getattr(r, "lang", None)}


_REPEAT = re.compile(r"\b(\w+)(?:\W+\1\b){4,}", re.IGNORECASE)


def clean_text(text: str) -> str:
    """A transcript without the recogniser's loops on noise ("Oh Oh Oh Oh..."): a word repeated five times or more
    in a row is cut to one, and a transcript that is mostly such loops is dropped."""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    cleaned = _REPEAT.sub(r"\1", t)
    words = cleaned.split()
    if len(words) < 2 or len(set(w.lower() for w in words)) < max(2, len(words) * 0.25):
        return ""
    return cleaned


# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------

def _stamp(p: dict) -> str:
    from app.ai.analysis import stamp
    return stamp(p)


def _key(speech_model: str) -> str:
    return f"{VERSION}:{speech_model or '-'}"


def _todo(photos: list, key: str) -> list:
    with vdb.reader() as con:
        have = {r[0]: (r[1], r[2]) for r in con.execute("SELECT photo_id, stamp, detail FROM ai_scans WHERE job = 'videos'")}
    return [p for p in photos if p["video"] and have.get(p["id"]) != (_stamp(p), key)]


def coverage() -> tuple:
    try:
        with vdb.reader() as con:
            done = con.execute("SELECT count(*) FROM ai_scans WHERE job = 'videos'").fetchone()[0]
        return int(done), None
    except Exception:
        return 0, None


def _job(photos: list, gen: int):
    if not available():
        return
    from app.ai import models
    speech = speech_available()
    key = _key(models.chosen("speech_model") if speech else "")
    todo = _todo(photos, key)
    progress.update(running=bool(todo), done=0, total=len(todo), phase="")
    if not todo:
        return
    faces = None
    try:
        from app.faces import facedetect
        if facedetect.available():
            faces = lambda im: facedetect.detect_image(im)  # noqa: E731
    except Exception:
        faces = None
    try:
        for p in todo:
            if progress.cancelled(gen):
                return
            src = p["path"]
            if not os.path.isfile(src):
                progress.step()
                continue
            dur = p.get("duration") or 0
            with vdb.reader() as con:
                old = con.execute("SELECT stamp, cover_t, chapters, transcript, segments, lang, speech_model FROM video_info WHERE photo_id = ?", (p["id"],)).fetchone()
            same_file = old is not None and old[0] == _stamp(p)
            try:
                if same_file and old[1] is not None:
                    cover_t, chapters = old[1], json.loads(old[2] or "[]")
                else:
                    cover_t, _s = pick_cover(src, dur, faces)
                    chapters = chapters_for(src, dur)
                if speech and not (same_file and old[6] == models.chosen("speech_model")):
                    tr = transcribe(src, dur)
                elif same_file:
                    tr = {"text": old[3] or "", "segments": json.loads(old[4] or "[]"), "lang": old[5]}
                else:
                    tr = None
            except Exception as e:
                log.warning("videos: failed on %s (%s)", src, e)
                progress.step()
                continue
            if progress.cancelled(gen):
                return
            with vdb.writer() as con:
                con.execute("DELETE FROM video_info WHERE photo_id = ?", (p["id"],))
                con.execute("INSERT INTO video_info (photo_id, stamp, cover_t, chapters, transcript, segments, lang, speech_model, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                            (p["id"], _stamp(p), cover_t, json.dumps(chapters), (tr or {}).get("text") or None, json.dumps((tr or {}).get("segments") or []),
                             (tr or {}).get("lang"), models.chosen("speech_model") if (speech and tr is not None) else None, int(time.time())))
                con.execute("INSERT OR REPLACE INTO ai_scans VALUES (?,'videos',?,?)", (p["id"], _stamp(p), key))
            if not (same_file and old[1] == cover_t):
                thumbs.forget(p["id"])  # the thumbnail, preview and embedding are made again from the chosen frame
            progress.step()
    finally:
        progress.finish(gen)


def start(photos: list):
    if not config.ANALYSIS or not available():
        return
    gen = progress.cancel()
    jobs.submit("videos", lambda: _job([p for p in photos if p["video"]], gen))


def reset():
    progress.cancel()
    with vdb.writer() as con:
        con.execute("DELETE FROM video_info")
        con.execute("DELETE FROM ai_scans WHERE job = 'videos'")


# --------------------------------------------------------------------------
# what the rest of the app reads
# --------------------------------------------------------------------------

def cover_time(photo_id: int):
    """The chosen cover frame's time, or None (then thumbs.video_frame uses its own rule)."""
    try:
        with vdb.reader() as con:
            r = con.execute("SELECT cover_t FROM video_info WHERE photo_id = ?", (photo_id,)).fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def info(photo_id: int) -> dict:
    """{cover_t, chapters: [seconds], transcript, segments, lang} for the info panel and the player."""
    try:
        with vdb.reader() as con:
            r = con.execute("SELECT cover_t, chapters, transcript, segments, lang FROM video_info WHERE photo_id = ?", (photo_id,)).fetchone()
    except Exception:
        r = None
    if not r:
        return {}
    return {"cover_t": r[0], "chapters": json.loads(r[1] or "[]"), "transcript": r[2], "segments": json.loads(r[3] or "[]"), "lang": r[4]}


def transcripts() -> dict:
    """{photo id: transcript} for the search index."""
    out = {}
    try:
        with vdb.reader() as con:
            for pid, text in con.execute("SELECT photo_id, transcript FROM video_info WHERE transcript IS NOT NULL AND transcript != ''"):
                out[pid] = text
    except Exception as e:
        log.warning("videos: transcripts unreadable (%s)", e)
    return out


def signature() -> str:
    """Part of the index version: changes as transcripts arrive."""
    try:
        with vdb.reader() as con:
            n = con.execute("SELECT count(*) FROM video_info WHERE transcript IS NOT NULL").fetchone()[0]
        return "v%d" % (n // 200 * 200 if progress["running"] else n)  # in steps while the job runs: no rebuild per video
    except Exception:
        return "v0"


def forget(ids: list):
    with vdb.writer() as con:
        for part in chunked(ids, 500):
            con.execute(f"DELETE FROM video_info WHERE photo_id IN ({','.join(str(int(i)) for i in part)})")
