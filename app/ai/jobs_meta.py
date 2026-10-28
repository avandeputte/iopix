"""The background jobs the AI analysis page lists, one row each: key, label, what it does (detail), progress and
whether it is enabled and can be run again. The Maintenance page and the MCP server read the same table."""
from app import config, jobs as bgjobs
from app.ai import analysis, caption, detector, looks, models as dlmodels
from app.ai.semantic import enabled as semantic_enabled, ENGINES, semantic
from app.core import prewarmer
from app.faces import ages, facedetect, facevecs


def rows(total: int, stills: int, signed_in: bool) -> list:
    """One dict per job. total: photos in the index; stills: photos that are not videos; signed_in: whether the
    caller may see file paths (the thumbnail job's current files)."""
    cov = analysis.coverage()
    up = semantic.upgrade
    act = semantic.active if semantic_enabled() else None
    names = {k: dlmodels.REGISTRY[k]["label"] for k in ENGINES}
    mname = lambda group: dlmodels.REGISTRY[dlmodels.chosen(group)]["label"]  # noqa: E731
    jobs = [
        {"key": "thumbs", "label": "Thumbnails", "detail": "Small previews for the grid.",
         "done": total - (prewarmer.total - prewarmer.done if prewarmer.running else 0), "total": total,
         "running": prewarmer.running, "enabled": True, "rerun": False, "since": prewarmer.started, "last": prewarmer.last, "current": (list(prewarmer.current.values())[:3] if signed_in else [])},
        {"key": "embeddings", "label": "Content search", "enabled": act is not None, "rerun": True,
         "detail": (f"Model: {names[act.name]}." + (("" if act is up else f" Switching to {names[up.name]}: ") + f"{nf(len(up.ids) if up.ids is not None else 0)} of {nf(total)} indexed" + ("; results come from the photos indexed so far." if act is up else f"; search stays on {names[act.name]} until it finishes.") if up else "")
                    + (f" {nf(len(act.failed))} could not be embedded (no thumbnail; see the server log)." if act is not None and act.failed else "")) if act else "Not installed (see README).",
         "done": int(len(act.ids)) if act is not None and act.ids is not None else 0, "total": total, "running": semantic.running},
        {"key": "quality", "label": "Sharpness and exposure", "detail": "Used for the Blurry / Sharpest filters and for picking the best frame of a burst. Computed in the same pass as object detection (one decode per photo), so the two rows advance together.",
         "done": cov["quality"], "total": stills, "running": analysis.progress["quality"]["running"], "enabled": config.ANALYSIS, "rerun": True},
        {"key": "vision", "label": "Text in photos and face quality", "detail": "Text recognition (RapidOCR) makes signs, jerseys, documents and screenshots searchable; face quality is measured on each face region.",
         "done": cov["vision"], "total": stills, "running": analysis.progress["vision"]["running"],
         "enabled": config.ANALYSIS and analysis.vision_available(), "rerun": True},
        {"key": "objects", "label": "Object detection", "detail": f"{mname('object_model')}: people, balls, animals, vehicles and other everyday objects, with boxes. Computed in the same pass as sharpness and exposure, so the two rows advance together. After switching to another size, “Run again” redoes every photo with it; until then only new photos use it.",
         "done": cov["objects"], "total": stills, "running": analysis.progress["objects"]["running"],
         "enabled": config.ANALYSIS and detector.available(), "rerun": True},
        {"key": "faces", "label": "Face grouping and suggestions", "detail": f"An embedding ({mname('face_vectors')}) for every face that has no name yet, whoever marked it, and for the most recent faces of each named person, so the Faces page shows look-alikes together with a “looks like” suggestion and a one-click name. A suggestion is written nowhere until you accept it.",
         "done": facevecs.coverage()[0], "total": facevecs.coverage()[1], "running": facevecs.progress["running"], "enabled": config.ANALYSIS and facevecs.available(), "rerun": True},
        {"key": "facedetect", "label": "Face detection", "detail": f"Finds faces nothing has marked yet ({mname('face_detector')}) and adds them as unnamed faces, ready for grouping and naming. Existing face regions, from digiKam or any other tool, are kept: a face that overlaps one is not added again, even when the two rectangles differ. Runs only when you start it (here for the library, or from a folder or a photo); writes to digiKam's database, backed up first."
         + (f" Last run: {nf(facedetect.progress['added'])} new faces in {nf(facedetect.progress['photos_with_new'])} photos." if facedetect.progress.get("started") else ""),
         "done": facedetect.coverage(), "total": stills, "running": facedetect.progress["running"], "enabled": facedetect.available() and config.DB_WRITE, "rerun": True},
        {"key": "looks", "label": "Photo quality", "detail": "How sharp the sharpest part of each photo is (so a soft background is fine), "
         + ("an aesthetic score (" + dlmodels.REGISTRY["aesthetic"]["label"] + ")" if looks.aesthetic_available() else "an aesthetic score once its model is installed")
         + (" and closed eyes on faces seen from the front" if looks.eyes_available() else ", and closed eyes once the eye model and a face detection model are installed")
         + ". Picks the best frame of each burst and suggests likely rejects (Filters › Quality); nothing is marked until you do it. A model installed later fills in its part without redoing the rest.",
         "done": cov["looks"], "total": stills, "running": analysis.progress["looks"]["running"], "enabled": config.ANALYSIS, "rerun": True},
        {"key": "ages", "label": "Ages for date suggestions", "detail": "How old the named people look (" + dlmodels.REGISTRY["mivolo"]["label"] + ") in photos marked “Date unknown”, and in a sample of the same people’s photos with a known date, which calibrate it for each of them. Explore › Date suggestions then proposes a year for each undated photo; nothing is written until you use one.",
         "done": ages.coverage()[0], "total": ages.coverage()[1], "running": ages.progress["running"], "enabled": config.ANALYSIS and ages.available(), "rerun": True},
        {"key": "captions", "label": "Photo descriptions", "detail": f"A description of each photo ({mname('caption_model')}: one sentence with BLIP, a short paragraph with Florence-2), searchable from the search box and shown in the info panel. After switching to another model, “Run again” redoes every photo with it; until then only new photos use it.",
         "done": cov["captions"], "total": stills, "running": analysis.progress["captions"]["running"],
         "enabled": config.ANALYSIS and caption.available(), "rerun": True},
    ]
    queue_key = {"thumbs": "thumbs", "embeddings": "embeddings", "quality": "pixels", "objects": "pixels", "vision": "vision", "captions": "captions", "faces": "faces", "facedetect": "facedetect", "looks": "looks", "ages": "ages"}
    for j in jobs:
        j["queued"] = bgjobs.is_queued(queue_key[j["key"]])
    return jobs


def nf(n) -> str:
    return f"{n:,}"
