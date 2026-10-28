"""No front-end module declares a local name that hides one of its own imports. v1.89.0 shared an upload helper
`send` from core.js while Import and folder uploads kept local functions called send: each then called itself
instead of uploading, and Import failed with "Cannot read properties of undefined (reading 'token')"."""
import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "static" / "js"
# known and harmless: the local is the one meant everywhere in its scope, and the import is not used there
ALLOWED = {("pages.js", "date"), ("map.js", "go")}


def test_no_local_hides_an_import():
    found = []
    for f in sorted(JS.glob("*.js")):
        s = f.read_text()
        imported = set()
        for m in re.finditer(r"import \{([^}]*)\} from", s):
            imported |= {x.strip().split(" as ")[-1] for x in m.group(1).split(",") if x.strip()}
        for m in re.finditer(r"\b(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)\b", s):
            if m.group(1) in imported and (f.name, m.group(1)) not in ALLOWED:
                found.append(f"{f.name}:{s[:m.start()].count(chr(10)) + 1} {m.group(1)}")
    assert not found, "local names hiding imports: " + ", ".join(found)
