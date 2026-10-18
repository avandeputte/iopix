"""Documentation screenshots of the demo library (tools/demo/serve_demo.sh must be running).

    .venv/bin/python tools/demo/screenshots.py [--base http://127.0.0.1:8610] [--out docs/screenshots]
"""
import argparse
import io
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

SHOTS = [  # name, hash route, what to do before the shot, viewport
    ("explore", "#/explore", None, "desktop"),
    ("photos", "#/photos", None, "desktop"),
    ("search", "#/photos?q=temple", None, "desktop"),
    ("viewer", "#/photos?p={family}", "info", "desktop"),
    ("map", "#/map", "settle", "desktop"),
    ("places", "#/places", None, "desktop"),
    ("people", "#/people", None, "desktop"),
    ("folders", "#/folders", None, "desktop"),
    ("maintenance", "#/maintenance", "library", "desktop"),
    ("edit-log", "#/history?action=edit", "large", "desktop"),
    ("phone-explore", "#/explore", None, "phone"),
    ("phone-viewer", "#/photos?p={family}", "photo", "phone"),
]
DEMO = Path.home() / "iopix-demo"  # where build_demo.py put the library
FAMILY = "Leo Parker Mia Parker Daniel Parker Emma Parker"  # the photo shown in the viewer: the whole family


def get(base, path):
    return json.load(urllib.request.urlopen(base + path))


def wait_idle(base, limit=900):
    """Background analysis paints a progress line in the sidebar: wait for it to finish."""
    end = time.time() + limit
    while time.time() < end:
        st = get(base, "/api/status")
        if not (st.get("jobs") or {}).get("running") and not (st.get("thumbs") or {}).get("running"):
            return
        time.sleep(5)


VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "phone": {"width": 390, "height": 844}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8610")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "docs" / "screenshots"))
    ap.add_argument("--dark", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    st = get(a.base, "/api/status")
    print("demo server", st.get("appVersion"))
    family = (get(a.base, "/api/photos?q=" + urllib.parse.quote(FAMILY))["id"] or [""])[0]
    wait_idle(a.base)
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, route, action, vp in SHOTS:
            ctx = b.new_context(viewport=VIEWPORTS[vp], device_scale_factor=2, color_scheme="dark" if a.dark else "light",
                                is_mobile=vp == "phone", has_touch=vp == "phone")
            # the app keeps its own theme choice (light unless changed): set it before the page loads
            ctx.add_init_script(f"try {{ localStorage.setItem('theme', '{'dark' if a.dark else 'light'}'); }} catch (e) {{}}")
            pg = ctx.new_page()
            pg.goto(a.base + "/" + route.format(family=family))
            pg.wait_for_timeout(2500)
            if action in ("photo", "info"):
                pg.wait_for_selector("#lightbox .lb-frame img", timeout=15000)
                # the full-size image replaces the preview: wait for it to be decoded
                pg.wait_for_function("[...document.querySelectorAll('#lightbox .lb-frame img')].some((i) => i.complete && i.naturalWidth > 600)", timeout=30000)
                pg.wait_for_timeout(1500)
                if action == "info":
                    pg.keyboard.press("i")
                    pg.wait_for_timeout(2500)
                pg.mouse.move(700, 30)
                pg.wait_for_timeout(800)
            if action == "large":  # the edit log with bigger pictures
                pg.locator(".eseg button", has_text="Large").first.click()
                pg.wait_for_timeout(1200)
            if action == "library":  # past the paths and the "no users yet" note
                pg.evaluate("[...document.querySelectorAll('h2')].find((x) => x.textContent.trim() === 'Library')?.scrollIntoView()")
                pg.wait_for_timeout(500)
            if action == "settle":
                pg.wait_for_timeout(3000)
            # the demo's own folder shows in a few places (file paths): written as a generic install path instead
            pg.evaluate("""(home) => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                for (let n; (n = w.nextNode());) if (n.nodeValue.includes(home)) n.nodeValue = n.nodeValue.split(home).join('/srv/iopix'); }""", str(DEMO))
            # captured at 2x, kept as a JPEG 1920 px wide (a phone at 2x as is): sharp in the README, small in the repo
            im = Image.open(io.BytesIO(pg.screenshot()))
            if im.width > 1920:
                im = im.resize((1920, round(im.height * 1920 / im.width)), Image.LANCZOS)
            im.convert("RGB").save(out / f"{name}{'-dark' if a.dark else ''}.jpg", quality=84, optimize=True, progressive=True)
            print("saved", name, flush=True)
            ctx.close()
        b.close()


if __name__ == "__main__":
    main()
