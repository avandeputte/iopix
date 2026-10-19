"""The viewer's own log (data/viewer.log, rotating; see main._setup_file_log), read for Maintenance ›
Server log and the MCP server_log tool."""
from app import config

PAGE_REQUEST = " uvicorn.access: "


def tail(lines: int = 300, contains: str = "", level: str = "any", requests: bool = True) -> dict:
    """The last `lines` lines. `contains` keeps lines with that text; `level` "warning" or "error" keeps
    only those, "auth" only sign-ins, failed attempts and sign-outs (tracebacks count as errors); `requests` False drops the page-request lines."""
    lines = max(1, min(int(lines), 5000))
    path = config.LOG_FILE
    if not path.exists():
        return {"file": str(path), "size_bytes": 0, "returned": 0, "lines": [], "note": "no log file yet (the server writes it from its next start)"}
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 4_000_000))
        text = f.read().decode("utf-8", "replace")
    out = text.splitlines()
    if size > 4_000_000:
        out = out[1:]  # the first line was cut in the middle
    if not requests:
        out = [l for l in out if PAGE_REQUEST not in l]
    if contains:
        c = contains.lower()
        out = [l for l in out if c in l.lower()]
    if level == "auth":  # sign-ins, failed attempts, sign-outs
        out = [l for l in out if " app.auth: " in l]
    if level == "warning":
        out = [l for l in out if " WARNING " in l or " ERROR " in l or " CRITICAL " in l or "Traceback" in l]
    elif level == "error":
        out = [l for l in out if " ERROR " in l or " CRITICAL " in l or "Traceback" in l]
    return {"file": str(path), "size_bytes": size, "returned": min(lines, len(out)), "lines": out[-lines:]}
