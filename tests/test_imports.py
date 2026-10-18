"""Every import in the app and the tests resolves: top-level ones, and the ones inside functions that only run
when a feature is used (a broken one there passes every other test and fails in front of the user)."""
import ast
import importlib
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "tests").glob("*.py"))


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve(node: ast.ImportFrom, mod: str, is_pkg: bool) -> str:
    if not node.level:
        return node.module or ""
    base = mod.split(".") if is_pkg else mod.split(".")[:-1]
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def test_every_import_resolves():
    problems = []
    for path in FILES:
        mod = _module_name(path)
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve(node, mod, path.name == "__init__.py")
                if not target.startswith("app"):
                    continue
                try:
                    m = importlib.import_module(target)
                except Exception as e:
                    problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: cannot import {target} ({e})")
                    continue
                for a in node.names:
                    if a.name == "*" or hasattr(m, a.name) or importlib.util.find_spec(f"{target}.{a.name}") is not None:
                        continue
                    problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: {target} has no {a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("app") and importlib.util.find_spec(a.name) is None:
                        problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: no module {a.name}")
        # module paths written as strings: __import__("app.x"), "-m", "app.x"
        for m in re.finditer(r"""["'](app(?:\\.[a-z_]+)+)["']""", path.read_text()):
            name = m.group(1)
            if name.endswith((".js", ".css")) or ":" in name:
                continue
            if importlib.util.find_spec(name) is None:
                problems.append(f"{path.relative_to(ROOT)}: string module path {name} does not exist")
    assert not problems, "\n".join(problems)


def test_module_paths_in_scripts_and_docs_exist():
    """`python -m app.x` in the installers, the updater, the Dockerfile and the README must name real modules."""
    files = [ROOT / n for n in ("setup.sh", "update.sh", "run.sh", "README.md", "Dockerfile", "docker-compose.yml")] + \
        list((ROOT / "installer").glob("*.sh")) + list((ROOT / "proxmox").rglob("*.sh")) + list((ROOT / "app").rglob("*.py"))
    missing = []
    for f in files:
        if not f.exists():
            continue
        for m in re.finditer(r"-m (app(?:\.[a-z_]+)+)", f.read_text()):
            if importlib.util.find_spec(m.group(1)) is None:
                missing.append(f"{f.relative_to(ROOT)}: {m.group(1)}")
    assert not missing, missing
