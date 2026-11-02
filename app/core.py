"""Compatibility shim: app.core was split into app.runtime (index connection, poll state, refresh
loop, prewarm), app.http (response helpers) and app.guards (the write gates) in v1.108. The old
names, private ones included, still resolve here for anything outside the app that imported
them; remove after v1.120."""
from app.runtime import (CACHES, THUMB_COLS, DB_ERRORS, state, prewarmer, db, resolve, start_prewarm, run_analysis_soon,
                         kick, tag_covers, tag_covers_from, compute_tag_covers, public_version, refresh, auto, auto_apply,
                         watch, models_arrived, setup_file_log, APP_NAME_DEFAULT, app_name, app_stamp, rebuild_after_write)
from app.http import IMG_CACHE, jresp, form_items, photo_row, cached_image, json_body
from app.guards import face_detect_allowed, organize_allowed, run_organize

_start_prewarm, _kick, _tag_covers, _tag_covers_from, _compute_tag_covers = start_prewarm, kick, tag_covers, tag_covers_from, compute_tag_covers
_public_version, _refresh, _auto, _auto_apply, _watch, _models_arrived = public_version, refresh, auto, auto_apply, watch, models_arrived
_setup_file_log, _app_stamp, _rebuild_after_write = setup_file_log, app_stamp, rebuild_after_write
_form_items, _photo_row, _cached_image = form_items, photo_row, cached_image
_face_detect_allowed, _organize_allowed, _run_organize = face_detect_allowed, organize_allowed, run_organize

__all__ = ["CACHES", "THUMB_COLS", "DB_ERRORS", "state", "prewarmer", "db", "resolve", "start_prewarm", "run_analysis_soon", "kick",
           "tag_covers", "tag_covers_from", "compute_tag_covers", "public_version", "refresh", "auto", "auto_apply", "watch",
           "models_arrived", "setup_file_log", "APP_NAME_DEFAULT", "app_name", "app_stamp", "rebuild_after_write",
           "IMG_CACHE", "jresp", "form_items", "photo_row", "cached_image", "json_body", "face_detect_allowed", "organize_allowed", "run_organize"]
