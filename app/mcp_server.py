"""Compatibility shim: the MCP server moved to the app.mcp package in v1.110 (base, read, files,
admin). `python -m app.mcp_server` still serves stdio, as the README says, and the server object and
the tool functions still resolve here for anything outside the app that imported them; remove after
v1.120 (`python -m app.mcp` is the new form)."""
from app.mcp import server, token_given, db
from app.mcp.read import (library_overview, search_photos, count_by, get_photo, list_people, list_tags, list_albums,
                          list_places, list_app_albums, find_duplicates, view_photo)
from app.mcp.files import (diagnostics, server_log, job_queue, repair_video, proxy_signin, inspect_file, photo_file,
                           check_for_update, update_viewer)
from app.mcp.admin import (consistency_report, edit_photos, consistency_scan, consistency_sync, rewrite_sidecars, face_quality,
                           rename_folder, tag_op, set_date_precision, date_report, date_suggestions, place_suggestions,
                           jersey_suggestions, clothing_suggestions, rotation_findings, set_date_hint, run_analysis, face_cleanup,
                           precision_job)

_precision_job = precision_job  # old private name
__all__ = ["server", "token_given", "db", "library_overview", "search_photos", "count_by", "get_photo", "list_people", "list_tags",
           "list_albums", "list_places", "list_app_albums", "find_duplicates", "view_photo", "diagnostics", "server_log", "job_queue",
           "repair_video", "proxy_signin", "inspect_file", "photo_file", "check_for_update", "update_viewer", "consistency_report",
           "edit_photos", "consistency_scan", "consistency_sync", "rewrite_sidecars", "face_quality", "rename_folder", "tag_op",
           "set_date_precision", "date_report", "date_suggestions", "place_suggestions", "jersey_suggestions", "clothing_suggestions",
           "rotation_findings", "set_date_hint", "run_analysis", "face_cleanup", "precision_job"]

if __name__ == "__main__":
    server.run()
