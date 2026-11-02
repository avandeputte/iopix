"""The gates the editing endpoints pass before they write: face detection needs its model and a
reachable digiKam database, organizing needs writes to be on, and an organize operation's
failures become the HTTP answers the pages expect (409 for digiKam's refusals, 500 for a
rolled-back change)."""
from fastapi import HTTPException

from app import config
from app.ai import models as dlmodels
from app.digikam import dbsync, dkdb
from app.faces import facedetect
from app.data import index, state as appstate
from app.runtime import DB_ERRORS


def face_detect_allowed():
    if not facedetect.available():
        raise HTTPException(409, f"Face detection needs the {dlmodels.REGISTRY[dlmodels.chosen('face_detector')]['label']} model: download it in Maintenance › AI analysis and models")
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    if not dkdb.no_digikam() and (not index.sync_enabled() or index.offline["since"]):  # no digiKam: they live in the library tables
        raise HTTPException(409, "Face regions live in digiKam's database, which is unreachable (or Sync with digiKam is off)")


def organize_allowed():
    if not config.DB_WRITE:
        raise HTTPException(403, "Writing to digiKam's database is turned off (DB_WRITE=0)")
    if index.sync_enabled() and index.offline["since"]:
        raise HTTPException(409, "digiKam's database is unreachable right now")


def run_organize(fn, *args):
    try:
        return fn(*args)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except DB_ERRORS as e:
        raise HTTPException(500, f"The change failed and was rolled back: {e}")
