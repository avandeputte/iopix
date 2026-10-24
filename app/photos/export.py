"""Export: copies of photos made for sharing, never the library files themselves.

prepare(path, opts) -> a context manager giving the path of the converted file (or None for the untouched original)
    None: the original goes into the archive as it is. Otherwise a copy: resized so the long edge is at most
    opts["size"] pixels, and/or converted to JPEG (HEIC, PNG, TIFF, WebP and RAW previews too), upright with the
    orientation flag reset; its metadata copied from the original by exiftool (dates, camera, keywords, captions,
    copyright); with opts["nogps"] every location tag removed (GPS, XMP location, video GPS atoms).
name_for(pattern, ts, name, used) -> the file name in the archive (or a library rename): "keep", "date"
    (2026-10-17_143205.jpg), "date-name" (2026-10-17_IMG_1234.jpg) or "date-seq" (2026-10-17_001.jpg).
"""
import datetime as _dt
import contextlib
import os
import subprocess
import tempfile

from PIL import Image, ImageOps

from app import config
from app.photos import edits

from app import thumbs

HEIC = thumbs.pillow_heif is not None  # importing thumbs registers HEIC decoding with Pillow

STILL = {".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".avif", ".bmp", ".gif"}
GPS_TAGS = ["-gps:all=", "-xmp:geotag=", "-XMP-exif:GPS*=", "-XMP-iptcExt:LocationShown*=", "-XMP-iptcExt:LocationCreated*=",
            "-Keys:GPSCoordinates=", "-UserData:GPSCoordinates=", "-ItemList:GPSCoordinates=", "-XMP-photoshop:City=", "-XMP-photoshop:State=",
            "-XMP-photoshop:Country=", "-XMP-iptcCore:Location=", "-IPTC:City=", "-IPTC:Province-State=", "-IPTC:Country-PrimaryLocationName=", "-IPTC:Sub-location="]


def wants_change(opts: dict) -> bool:
    return bool(opts.get("size") or opts.get("fmt") == "jpeg" or opts.get("nogps"))


def _exiftool(args: list, path: str):
    subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-charset", "filename=utf8", *args, path],
                   capture_output=True, text=True, timeout=600)


@contextlib.contextmanager
def prepare(path: str, opts: dict):
    """A context giving the path of the file to put in the archive (a converted copy in a temp folder, removed on
    exit), or None for the untouched original. The copy stays on disk and is streamed from there: a video with its
    location removed is not read whole into memory."""
    if not wants_change(opts):
        yield None
        return
    ext = os.path.splitext(path)[1].lower()
    size = int(opts.get("size") or 0)
    convert = opts.get("fmt") == "jpeg" and ext not in (".jpg", ".jpeg", ".jpe")
    with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
        if ext in STILL and (size or convert):
            out = os.path.join(tmp, "out.jpg" if convert or ext in (".jpg", ".jpeg", ".jpe") else "out" + ext)
            with Image.open(path) as im:
                icc = im.info.get("icc_profile")
                im = ImageOps.exif_transpose(im)
                if size and max(im.size) > size:
                    im.thumbnail((size, size), Image.Resampling.LANCZOS)
                if out.endswith(".jpg"):
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    im.save(out, "JPEG", quality=int(opts.get("quality") or 90), icc_profile=icc, optimize=True)
                else:
                    im.save(out, icc_profile=icc) if icc else im.save(out)
            # the original's metadata on the copy; it is upright now, and its size is the new one
            _exiftool(["-tagsfromfile", path, "-all:all", "-xmp:all", "-unsafe", "-Orientation#=1", "-IFD1:all=", "-ThumbnailImage=", "-PreviewImage="], out)
            w, h = Image.open(out).size
            _exiftool([f"-ExifImageWidth={w}", f"-ExifImageHeight={h}"], out)
        else:  # a video, or a still kept at its size and format: a copy, so only the location can change
            out = os.path.join(tmp, "out" + ext)
            with open(path, "rb") as src, open(out, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
        if opts.get("nogps"):
            _exiftool(GPS_TAGS, out)
        yield out


def out_ext(path: str, opts: dict) -> str:
    ext = os.path.splitext(path)[1]
    return ".jpg" if opts.get("fmt") == "jpeg" and ext.lower() in STILL else ext


def name_for(pattern: str, ts, name: str, used: set, ext: str = None) -> str:
    """A file name by pattern, unique within `used` (which it updates)."""
    base, own_ext = os.path.splitext(name)
    ext = ext or own_ext
    when = _dt.datetime.utcfromtimestamp(ts) if ts else None  # the index keeps the time as taken, stored as UTC
    if pattern == "date" and when:
        stem = when.strftime("%Y-%m-%d_%H%M%S")
    elif pattern == "date-name" and when:
        stem = when.strftime("%Y-%m-%d_") + base
    elif pattern == "date-seq" and when:
        day = when.strftime("%Y-%m-%d")
        n = 1
        while f"{day}_{n:03d}{ext}".lower() in used:
            n += 1
        stem = f"{day}_{n:03d}"
    else:
        stem = base
    cand, n = stem + ext, 2
    while cand.lower() in used:
        cand = f"{stem}-{n}{ext}"
        n += 1
    used.add(cand.lower())
    return cand
