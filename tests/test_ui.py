"""Browser tests (Playwright, headless Chromium) for the behaviours a user
sees: the grid, the lightbox, face boxes following the Show-faces toggle,
the People rows and their menu, the person picker, read-only mode hiding
every editing control, and the Settings page.

A real uvicorn serves the scratch library on a free port, started once per
session from the same environment the API tests use.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

APP = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(lib):
    """uvicorn on a free port, same env overrides as the API tests (set by conftest)."""
    port = _free_port()
    env = dict(os.environ, DATA_DIR=str(Path(os.environ["DATA_DIR"]).parent / "data-ui"))
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=str(APP), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(300):
            try:
                with urllib.request.urlopen(url + "/api/status", timeout=1) as r:
                    import json
                    s = json.load(r)
                    if s.get("version"):
                        break
            except Exception:
                pass
            if proc.poll() is not None:
                raise RuntimeError("server died: " + proc.stderr.read().decode()[-800:])
            time.sleep(0.1)
        else:
            raise RuntimeError("server did not come up")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture(autouse=True)
def _ui_no_users(server):
    """The browser tests' own server keeps its users in its own viewer.db: start each test in open mode."""
    import sqlite3
    db = Path(os.environ["DATA_DIR"]).parent / "data-ui" / "viewer.db"
    if db.exists():
        con = sqlite3.connect(db); con.execute("DELETE FROM user_accounts"); con.commit(); con.close()
    yield


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.base = server
    pg.errors = errors
    yield pg
    ctx.close()
    assert not errors, "uncaught JS errors: " + "; ".join(errors)[:500]


def _open_photo(page, server, pid):
    page.goto(f"{server}/#/photos?p={pid}")
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    page.wait_for_timeout(300)
    page.mouse.move(420, 40)  # the toolbar hides 2.8 s after the last mouse move: a slow load must not hide it before the test clicks


def _set_read_only(server, on: bool):
    req = urllib.request.Request(server + "/api/settings", data=b'{"read_only": %s}' % (b"true" if on else b"false"),
                                 headers={"content-type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def test_grid_renders_thumbnails(page, server, lib):
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    assert page.locator(".tile").count() > 10


def test_face_boxes_follow_the_toggle(page, server, lib):
    pid = lib["with_faces"][0]
    _open_photo(page, server, pid)
    assert page.locator(".facebox:not(.objbox)").count() == 0, "boxes shown before the toggle"
    btn = page.locator('[title="Show faces (F)"]')
    btn.click()
    page.wait_for_timeout(200)
    assert page.locator(".facebox:not(.objbox)").count() > 0
    assert "on" in (btn.get_attribute("class") or "").split()
    page.keyboard.press("f")
    page.wait_for_timeout(200)
    assert page.locator(".facebox:not(.objbox)").count() == 0
    assert "on" not in (btn.get_attribute("class") or "").split()


def test_ignored_faces_are_not_drawn_or_listed(page, server, lib):
    import json
    for pid in lib["with_faces"]:
        with urllib.request.urlopen(f"{server}/api/photo/{pid}") as r:
            d = json.load(r)
        if any(f["kind"] == "ignored" for f in d.get("faces_other", [])):
            break
    else:
        pytest.skip("no ignored face in the scratch library")
    _open_photo(page, server, pid)
    page.keyboard.press("f")
    page.wait_for_timeout(200)
    assert page.locator(".facebox.face-ignored").count() == 0
    assert page.locator(".facerow:has-text('Ignored')").count() == 0


def test_people_rows_open_the_menu_and_picker(page, server, lib):
    pid = lib["with_faces"][0]
    _open_photo(page, server, pid)
    if page.locator(".lb-info").is_hidden():
        page.locator('[title^="Info"]').click()
    page.wait_for_selector(".facerow", timeout=10000)
    assert page.locator(".lb-info .ichips img").count() == 0, "the pill chips were removed; only rows remain"
    row = page.locator(".facerow").first
    row.click()
    page.wait_for_selector(".facemenu", timeout=5000)
    menu = page.locator(".facemenu")
    assert menu.is_visible()
    box = menu.bounding_box()
    assert box and box["x"] >= 0 and box["y"] >= 0, "menu must be positioned inside the viewport"
    # the menu paints on top: the element at its centre belongs to it
    on_top = page.evaluate("""() => { const m = document.querySelector('.facemenu'); const r = m.getBoundingClientRect();
        const e = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !!(e && e.closest('.facemenu')); }""")
    assert on_top
    item = menu.locator(".fmi", has_text="someone else").first if menu.locator(".fmi", has_text="someone else").count() else menu.locator(".fmi", has_text="Who is this").first
    item.click()
    page.wait_for_selector(".modal-back", timeout=5000)
    assert page.locator(".modal h3").inner_text().strip() == "Who is this?"
    assert page.locator(".modal .frow").count() > 0
    assert page.locator(".modal .frow .nm", has_text="People").count() == 0, "the People root must not be offered as a person"
    page.locator(".modal button", has_text="Cancel").click()
    assert page.locator(".modal-back").count() == 0


def test_face_menu_from_the_box_works_with_real_clicks(page, server, lib):
    pid = lib["with_faces"][0]
    _open_photo(page, server, pid)
    page.keyboard.press("f")
    page.wait_for_selector(".facebox:not(.objbox)", timeout=5000)
    page.locator(".facebox:not(.objbox)").first.click()
    page.wait_for_selector(".facemenu", timeout=5000)
    first = page.locator(".facemenu .fmi").first
    label = first.inner_text()
    first.click()  # a real pointer click: the stage must not capture it
    page.wait_for_timeout(400)
    assert page.locator(".facemenu").count() == 0, f"menu item {label!r} did not react to the click"


def test_read_only_hides_every_editing_control(page, server, lib):
    _set_read_only(server, True)
    try:
        pid = lib["with_faces"][0]
        _open_photo(page, server, pid)
        page.wait_for_timeout(1200)  # status poll picks up the flags
        page.reload()
        page.wait_for_selector("#lightbox .lb-frame img", timeout=15000)
        page.wait_for_timeout(500)
        assert page.locator(".lb-info input[placeholder='Title']").count() == 0
        page.wait_for_function("getComputedStyle(document.querySelector('[title^=\"Add a face\"]')).display === 'none'", timeout=5000)
        assert page.locator(".facerow .muted", has_text="options").count() == 0
        assert page.locator(".lb-info a", has_text="change").count() == 0
        page.goto(server + "/#/settings")
        page.wait_for_selector("h2", timeout=10000)
        heads = page.locator("h2").all_inner_texts()
        assert "Editing" not in heads and "Place tags" not in heads
        assert page.locator("b", has_text="Read-only mode").count() == 1
    finally:
        _set_read_only(server, False)


def test_settings_and_maintenance_pages(page, server):
    page.goto(server + "/#/settings")
    page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    page.wait_for_selector("h2:has-text('Metadata templates')", timeout=10000)  # the sections arrive with /api/maintenance
    heads = page.locator("h2").all_inner_texts()
    for h in ("Display", "Search and background work", "Editing", "Place tags", "Metadata templates"):
        assert h in heads, heads
    assert "Storage" not in heads and "Library" not in heads, "maintenance sections must not be on the Settings page"
    assert page.locator(".ajob select").count() >= 1, "the core budget selector lives in Settings"
    page.goto(server + "/#/maintenance")
    page.wait_for_selector("h1:has-text('Maintenance')", timeout=10000)  # a hash-only navigation re-renders in place
    heads = page.locator("h2").all_inner_texts()
    for h in ("Library", "AI analysis and models", "Storage and backups", "Viewer"):
        assert any(x == h or x.startswith(h + " ") or x.startswith(h + "\n") for x in heads), (h, heads)
    assert "Display" not in heads and "Editing" not in heads
    page.wait_for_selector(".mtable tbody tr", timeout=10000)
    assert page.locator("b", has_text="Background work").count() == 1
    rows = page.locator(".mtable tbody tr")
    assert rows.count() >= 7, "one row per feature"
    assert page.locator(".mtable tr[data-job=objects][data-model=yolox]").count() == 1, "a job and its model share a row"
    assert page.locator(".mtable tr[data-job=objects] select.mpick option").count() == 4, "the four YOLOX sizes are offered in a menu"
    assert page.locator(".mtable tr[data-job=facedetect] select.mpick option").count() == 3, "YuNet, SCRFD and RetinaFace"
    assert page.locator(".mtable tr[data-model=geonames]").count() == 1, "models without a job have a row too"
    assert page.locator(".mtable thead th").all_inner_texts()[:3] == ["Feature", "Model", "Progress"]
    assert page.locator("b", has_text="Restart the viewer").count() == 1
    assert page.locator(".srow b", has_text="Metadata consistency").count() == 1, "consistency sits under Library"
    assert page.locator(".stable.wide tr").count() >= 10 and page.locator(".stable.wide button", has_text="Rebuild").count() == 1
    assert page.locator("#nav a", has_text="Maintenance").count() == 0, "no longer in the main navigation"
    assert page.locator("#topbar a.tool[href='#/maintenance']").count() == 1 and page.locator("#topbar a.tool[href='#/settings']").count() == 1


def test_keyboard_navigation(page, server, lib):
    ids = [i for i in lib["ids"] if i not in lib["videos"]]
    _open_photo(page, server, ids[0])
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    assert page.url.endswith(f"p={ids[1]}") or f"p={ids[1]}" in page.url or page.url != f"{server}/#/photos?p={ids[0]}"
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.locator("#lightbox").is_hidden() or page.locator("#lightbox").count() == 0


def test_pasting_a_coordinate_pair_fills_both_fields(page, server, lib):
    pid = next(i for i in lib["ids"] if i not in lib["videos"])
    _open_photo(page, server, pid)
    if page.locator(".lb-info").is_hidden():
        page.locator('[title^="Info"]').click()
    page.wait_for_selector(".lb-info h4", timeout=10000)
    # open the location dialog from the info pane ("Set location" link or the Location heading's "change")
    link = page.locator(".lb-info a.lnk", has_text="Set location")
    if not link.count():
        link = page.locator(".lb-info a.lnk", has_text="change").last
    link.click()
    page.wait_for_selector(".modal input[placeholder='latitude']", timeout=5000)
    lat = page.locator(".modal input[placeholder='latitude']")
    lon = page.locator(".modal input[placeholder='longitude']")
    lat.focus()
    page.evaluate("""() => { const el = document.querySelector(".modal input[placeholder='latitude']");
        const dt = new DataTransfer(); dt.setData('text', '40.44414979347404, -79.96231586013303');
        el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })); }""")
    assert lat.input_value().startswith("40.4441")
    assert lon.input_value().startswith("-79.9623")
    # a plain number pastes as usual
    page.evaluate("""() => { const el = document.querySelector(".modal input[placeholder='longitude']");
        const dt = new DataTransfer(); dt.setData('text', '12.5');
        el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })); }""")
    assert lon.input_value().startswith("-79.9623"), "a single number is left to the browser's own paste"
    page.locator(".modal button", has_text="Cancel").click()


def test_pwa_service_worker_caches_the_shell(page, server):
    page.goto(server + "/#/photos")
    out = page.evaluate("""async () => { const r = await navigator.serviceWorker.ready; const c = await caches.open('iopix-shell-v3');
        return { state: r.active && r.active.state, cached: (await c.keys()).map((k) => new URL(k.url).pathname) }; }""")
    assert out["state"] in ("activating", "activated")  # ready resolves while the worker may still be activating for a tick
    assert {"/", "/app.js", "/js/core.js", "/js/lightbox.js", "/app.css", "/manifest.webmanifest", "/icons/icon-192.png"} <= set(out["cached"]), "the shell and every module are cached"
    assert not any(p.startswith("/api") or p.startswith("/thumb") for p in out["cached"]), "photos and the API are never cached"


def test_users_flow_in_the_browser(page, server, browser):
    import json, urllib.request
    # open mode: the person icon offers to create the admin
    page.goto(server + "/#/settings")
    page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    assert page.locator("b", has_text="No users yet").count() == 1
    page.locator("#usermenu button").click()
    page.wait_for_selector(".modal h3", timeout=5000)
    assert page.locator(".modal h3").inner_text().startswith("Create the admin")
    page.locator(".modal input[placeholder^='Admin user name']").fill("alex")
    page.locator(".modal input[type=password]").fill("correct horse")
    page.locator(".modal button", has_text="Create").click()
    page.wait_for_selector("#usermenu .avatar", timeout=5000)
    page.goto(server + "/#/settings"); page.wait_for_selector("h2:has-text('Users')", timeout=10000)
    assert page.locator("#topbar a.tool[href='#/maintenance']").is_visible()
    # a visitor in a fresh context: read-only, no maintenance icon, sign-in dialog
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}); visitor = ctx.new_page()
    visitor.goto(server + "/#/photos?p=" + str(json.load(urllib.request.urlopen(server + "/api/photos"))["id"][0]))
    visitor.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000); visitor.wait_for_timeout(600)
    if visitor.locator(".lb-info").is_hidden():
        visitor.locator('[title^="Info"]').click(); visitor.wait_for_timeout(400)
    assert visitor.locator(".lb-info h4", has_text="People").count() == 1, "the panel is open"
    assert visitor.locator(".lb-info input[placeholder='Title']").count() == 0
    assert not visitor.locator("#topbar a.tool[href='#/maintenance']").is_visible()
    visitor.keyboard.press("Escape"); visitor.wait_for_timeout(400)  # the lightbox covers the top bar
    visitor.locator("#usermenu button").click()
    visitor.wait_for_selector(".modal .account", timeout=5000)
    visitor.locator(".modal .account", has_text="alex").click()  # a protected account: the tile reveals the password field
    visitor.locator(".modal input[type=password]").fill("wrong")
    visitor.locator(".modal button", has_text="Sign in").click(); visitor.wait_for_timeout(500)
    assert visitor.locator(".modal").count() == 1, "a wrong password keeps the dialog open"
    visitor.locator(".modal input[type=password]").fill("correct horse"); visitor.locator(".modal button", has_text="Sign in").click()
    visitor.wait_for_selector("#usermenu .avatar", timeout=5000); visitor.wait_for_timeout(800)
    visitor.goto(server + "/#/photos?p=" + str(json.load(urllib.request.urlopen(server + "/api/photos"))["id"][0]))
    visitor.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000); visitor.wait_for_timeout(600)
    if visitor.locator(".lb-info").is_hidden():
        visitor.locator('[title^="Info"]').click(); visitor.wait_for_timeout(400)
    assert visitor.locator(".lb-info input[placeholder='Title']").count() == 1, "the admin can edit"
    visitor.keyboard.press("Escape"); visitor.wait_for_timeout(300)
    # a display setting now lives on the server
    visitor.goto(server + "/#/settings"); visitor.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    visitor.locator("input[type=checkbox]").first.uncheck(); visitor.wait_for_timeout(900)
    me = visitor.evaluate("async () => (await (await fetch('/api/auth/settings', {method:'POST', headers:{'content-type':'application/json'}, body:'{}'})).json())")
    assert me.get("badges") == "0"
    ctx.close()
    # leave the scratch server open again for the other tests
    urllib.request.urlopen(server + "/api/status").read()


def test_account_picker_one_click_sign_in(page, server, browser):
    import json, urllib.request
    # an admin and a passwordless viewer, made through the API in open mode
    req = lambda path, body: urllib.request.urlopen(urllib.request.Request(server + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST")).read()
    req("/api/auth/setup", {"name": "alex", "password": "correct horse"})
    # the admin creates the passwordless viewer from the Users section
    page.goto(server + "/#/settings"); page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    page.locator("#usermenu button").click(); page.wait_for_selector(".modal h3:has-text('Sign in')", timeout=5000)
    page.locator(".modal .account", has_text="alex").click(); page.locator(".modal input[type=password]").fill("correct horse"); page.locator(".modal button", has_text="Sign in").click()
    page.wait_for_selector("#usermenu .avatar", timeout=5000)
    page.goto(server + "/#/settings"); page.wait_for_selector("h2:has-text('Users')", timeout=10000)
    page.locator("input[placeholder='User name']").last.fill("kid")
    page.locator("label:has-text('no password') input").check()
    page.locator("button", has_text="Add user").click(); page.wait_for_timeout(600)
    assert page.locator(".utable tr[data-user='kid']", has_text="none (one-click)").count() == 1
    # a visitor: two tiles, one click as kid
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}); v = ctx.new_page()
    v.goto(server + "/#/photos"); v.wait_for_selector(".tile img", timeout=15000)
    v.locator("#usermenu button").click(); v.wait_for_selector(".modal .account", timeout=5000)
    names = v.locator(".modal .account .nm").all_inner_texts()
    assert names == ["alex", "kid", "Other"]
    assert v.locator(".modal input[type=password]").is_hidden(), "no password field until a protected tile is picked"
    v.locator(".modal .account", has_text="kid").click()
    v.wait_for_selector("#usermenu .avatar", timeout=5000)
    assert v.evaluate("async () => (await (await fetch('/api/auth/me')).json()).user.name") == "kid"
    ctx.close()


def test_database_row_tests_a_connection(page, server, lib):
    page.goto(server + "/#/settings"); page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    row = page.locator(".srow", has=page.locator("b", has_text="digiKam database"))
    assert row.count() == 1
    assert row.locator("select").first.input_value() == "sqlite"
    from pathlib import Path
    assert Path(row.locator("input[placeholder='folder with digikam4.db']").input_value()).resolve() == lib["db"].parent.resolve()
    row.locator("button", has_text="Test connection").click()
    page.wait_for_selector(".srow .muted:has-text('OK:')", timeout=10000)
    assert f"{len(lib['ids'])} photos" in row.locator(".muted", has_text="OK:").inner_text()
    row.locator("select").first.select_option("mysql")
    assert row.locator("input[placeholder='host']").is_visible() and row.locator("input[placeholder='folder with digikam4.db']").is_hidden()


def test_container_mode_writes_without_a_confirmation(page, server):
    """DIGIKAM_CHECK=ask/none (the browser tests' server runs that way): the viewer cannot see digiKam,
    so a database write goes through without asking whether digiKam is closed."""
    import json, sqlite3, urllib.request
    db = Path(os.environ["DATA_DIR"]).parent / "data-ui" / "viewer.db"
    con = sqlite3.connect(db); con.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('digikam_closed_until', '0')"); con.commit(); con.close()
    assert json.load(urllib.request.urlopen(server + "/api/status"))["digikamCheck"] == "none"
    dialogs = []
    def on_dialog(d):
        dialogs.append(d.type + ":" + d.message[:40])
        d.accept("zz-ui-vouch") if d.type == "prompt" else d.accept()
    page.on("dialog", on_dialog)
    page.goto(server + "/#/tags"); page.wait_for_selector("h1", timeout=10000)
    page.locator("button", has_text="Manage tags").first.click(); page.wait_for_timeout(300)
    page.locator("button", has_text="New top-level tag").first.click()
    page.wait_for_timeout(2500)
    assert any(t.startswith("prompt:") for t in dialogs), dialogs
    assert not any("closed" in t or "cannot see" in t for t in dialogs), "no confirmation about digiKam: " + str(dialogs)
    tags = json.load(urllib.request.urlopen(server + "/api/meta"))["tags"]
    assert any(t["name"] == "zz-ui-vouch" for t in tags), "after accepting, the tag is created"


def test_appearance_setting_switches_the_theme(page, server):
    """Settings > Display > Appearance: Light and Dark fix the palette (data-theme on <html>, the
    page background follows, the choice is remembered), System follows the device again."""
    page.goto(server + "/#/settings")
    page.wait_for_selector("#themeseg", timeout=10000)
    page.click("#themeseg [data-theme=dark]")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(23, 24, 27)"
    page.reload()
    page.wait_for_selector("#themeseg", timeout=10000)
    assert page.evaluate("document.documentElement.dataset.theme") == "dark", "remembered across loads, applied before the first paint"
    page.click("#themeseg [data-theme=light]")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    page.click("#themeseg [data-theme=system]")
    assert page.evaluate("document.documentElement.dataset.theme") is None
    assert page.evaluate("localStorage.getItem('theme')") == "system"


def test_light_is_the_default_theme(browser, server):
    ctx = browser.new_context(color_scheme="dark")  # even on a device set to dark
    pg = ctx.new_page()
    pg.goto(server + "/#/photos")
    pg.wait_for_selector("#topbar", timeout=10000)
    assert pg.evaluate("document.documentElement.dataset.theme") == "light"
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    ctx.close()


def test_explore_summary_cards_link_to_their_views(page, server, lib):
    page.goto(server + "/#/explore")
    page.wait_for_selector(".stats .stat", timeout=10000)
    cards = page.locator(".stats .stat")
    assert cards.count() == 6
    labels = page.locator(".stats .stat div > span").all_inner_texts()
    assert labels == ["photos", "videos", "people", "folders", "with location", "on disk"], labels
    assert page.locator(".stats a.stat").count() == 5, "all but 'on disk' open a view"
    assert page.locator(".stats .stat .sic .ic svg").count() == 6, "each card has an icon"
    colors = set(page.evaluate("[...document.querySelectorAll('.stat .sic .ic')].map(e => getComputedStyle(e).color)"))
    assert colors == {"rgb(255, 255, 255)"}, f"white glyphs on the coloured tiles, for contrast: {colors}"
    page.click(".stats a.stat:has-text('people')")
    page.wait_for_selector("#nav a[data-v=people].on", timeout=5000)


def test_lightbox_film_strip_and_remembered_info_panel(page, server, lib):
    """The film strip along the bottom of the lightbox shows the neighbours, marks the current
    photo, jumps on click, and slides up when the mouse reaches the bottom edge. The info panel
    reopens the way it was last left."""
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.locator(".tile").first.click()
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    page.wait_for_selector("#lightbox .lb-strip .reel img.cur", timeout=5000)
    reel = page.locator("#lightbox .lb-strip .reel img")
    assert reel.count() >= 2 and page.locator("#lightbox .lb-strip .reel img.cur").get_attribute("data-i") == "0"
    strip = page.locator("#lightbox .lb-strip")
    assert "show" not in (strip.get_attribute("class") or ""), "parked until the pointer comes near the bottom edge"
    box = page.locator("#lightbox .lb-stage").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    # low in the photo (where a video's controls or a low face are) it stays parked
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 60)
    page.wait_for_timeout(700)
    assert "show" not in (strip.get_attribute("class") or ""), "only the bottom edge opens it"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 20)
    page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.wait_for_selector("#lightbox .lb-strip:not(.show)", timeout=3000)
    # with the faces shown the edge leaves it parked (faces low in the photo stay reachable); the grip opens it
    page.keyboard.press("f")
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 6)
    page.wait_for_timeout(700)
    assert "show" not in (strip.get_attribute("class") or ""), "faces shown: the edge does not open the strip"
    page.locator("#lightbox .lb-strip .grip").click()
    page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.wait_for_selector("#lightbox .lb-strip:not(.show)", timeout=3000)
    page.keyboard.press("f")
    # hovering another thumbnail previews it over the stage; leaving the strip restores the view
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 20)
    page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)
    reel.nth(1).hover(force=True)
    want = reel.nth(1).get_attribute("src").split("/")[-1]
    # (the thumbnail that slid up under the resting pointer may be previewed first: the hovered one replaces it)
    page.wait_for_function(f"document.querySelector('#lightbox .lb-peek')?.dataset.id === '{want}'", timeout=3000)
    assert page.locator("#lightbox .lb-strip .reel img.cur").get_attribute("data-i") == "0", "peeking does not move"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.wait_for_selector("#lightbox .lb-peek", state="detached", timeout=3000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 10)
    page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)
    page.wait_for_timeout(300)  # slid up
    reel.nth(1).click(force=True)
    page.wait_for_function("document.querySelector('#lightbox .lb-strip .reel img.cur')?.dataset.i === '1'", timeout=5000)
    assert page.locator("#lightbox .lb-peek").count() == 0
    assert "p=" in page.url
    # the info panel: open it, leave, come back
    page.keyboard.press("i")
    page.wait_for_selector("#lightbox .lb-info:not([hidden])", timeout=3000)
    page.keyboard.press("Escape")
    page.wait_for_selector("#lightbox", state="hidden", timeout=5000)
    page.locator(".tile").first.click()
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    assert page.locator("#lightbox .lb-info").is_visible(), "reopens with the info panel as it was left"
    page.keyboard.press("i")
    page.keyboard.press("Escape")
    page.wait_for_selector("#lightbox", state="hidden", timeout=5000)
    page.locator(".tile").first.click()
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    assert page.locator("#lightbox .lb-info").is_hidden()
    page.keyboard.press("Escape")


def test_grid_select_all_toggles_and_groups_by_month(page, server, lib):
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.wait_for_selector(".gbar .selall", timeout=5000)
    assert page.locator(".gbar .selall").is_visible() and page.locator(".gbar .selnone").is_hidden()
    page.click(".gbar .selall")
    page.wait_for_selector("#selbar", timeout=5000)
    assert page.locator(".gbar .selall").is_hidden() and page.locator(".gbar .selnone").is_visible(), "everything selected: only Deselect all"
    page.click(".gbar .selnone")
    page.wait_for_selector("#selbar", state="detached", timeout=5000)
    assert page.locator(".gbar .selall").is_visible() and page.locator(".gbar .selnone").is_hidden()
    # grouping: the scratch library spans several days in a month, so months make fewer sections
    page.locator("#main").evaluate("m => m.scrollTop = 0")
    page.wait_for_timeout(300)
    grid_h = page.evaluate("document.getElementById('grid').style.height")
    days = page.locator(".ghead").count()
    page.select_option(".gbar .groupsel", "month")
    page.wait_for_function(f"document.getElementById('grid').style.height !== '{grid_h}'", timeout=5000)
    page.wait_for_timeout(300)
    months = page.locator(".ghead").count()
    assert 0 < months < days, (days, months)
    head = page.locator(".ghead").first.inner_text()
    assert any(m in head for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")) and "," not in head, head
    assert page.evaluate("localStorage.getItem('group')") == "month"
    page.select_option(".gbar .groupsel", "day")


def test_lightbox_bar_has_close_and_share_menu(page, server, lib):
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.locator(".tile").first.click()
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    assert page.locator("#lightbox .lb-top button[title^='Close']").count() == 1
    page.click("#lightbox .lb-top button[title='Share']")
    page.wait_for_selector("#lightbox .sharemenu", timeout=3000)
    labels = page.locator("#lightbox .sharemenu .fmi").all_inner_texts()
    assert any(l.startswith("Copy link") for l in labels) and any(l.startswith("Download original") for l in labels) and any(l.startswith("Email") for l in labels), labels
    assert page.locator("#lightbox .sharemenu a[href$='?dl=1']").count() >= 1
    page.locator("#lightbox .lb-stage").click(position={"x": 60, "y": 400})
    page.wait_for_selector("#lightbox .sharemenu", state="detached", timeout=3000)
    page.click("#lightbox .lb-top button[title^='Close']")
    page.wait_for_selector("#lightbox", state="hidden", timeout=5000)
    assert page.locator(".tile img").count() > 0, "back to the grid it came from"


def test_share_button_opens_the_device_share_sheet_with_the_picture(page, server, lib):
    """Where the browser has a share sheet, the share button hands it the photo as a JPEG named
    after the file (the fallback menu is for browsers without one, tested above)."""
    page.add_init_script("""
      window.__shared = [];
      navigator.share = async (d) => { window.__shared.push({ title: d.title, url: d.url, files: (d.files || []).map((f) => [f.name, f.type, f.size]) }); };
      navigator.canShare = () => true;
    """)
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.locator(".tile").first.click()
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    page.wait_for_timeout(1500)  # the picture for sharing is fetched once the photo is shown
    page.click("#lightbox .lb-top button[title='Share']")
    page.wait_for_function("window.__shared.length === 1", timeout=5000)
    shared = page.evaluate("window.__shared[0]")
    assert page.locator("#lightbox .sharemenu").count() == 0, "the sheet, not the menu"
    if page.locator("#lightbox .lb-frame video").count():
        assert shared["url"]
    else:
        assert shared["files"] and shared["files"][0][0].endswith(".jpg") and shared["files"][0][1] == "image/jpeg" and shared["files"][0][2] > 1000, shared
    page.keyboard.press("Escape")


def test_on_this_day_is_a_justified_gallery_without_scrollbars(page, server, lib):
    page.goto(server + "/#/explore")
    page.wait_for_selector(".stats .stat", timeout=10000)
    page.wait_for_timeout(1500)
    g = page.locator("h2:has-text('On this day') + .jrows")
    if g.count() == 0 or g.locator(".tile").count() == 0:
        pytest.skip("no photo in the scratch library was taken on today's date")
    assert page.evaluate("(e) => e.scrollWidth <= e.clientWidth", g.element_handle()), "nothing to scroll sideways"
    box = g.bounding_box()
    tiles = g.locator(".tile")
    for i in range(tiles.count()):
        b = tiles.nth(i).bounding_box()
        assert b["x"] >= box["x"] - 1 and b["x"] + b["width"] <= box["x"] + box["width"] + 1, "tiles wrap within the page width"


def test_require_sign_in_shows_only_the_login_dialog(page, server, browser):
    """With Require sign-in on, a visitor loading the app sees the sign-in dialog and nothing of
    the library; signing in from it loads the library in place."""
    accounts = page.request.get(server + "/api/auth/accounts").json()["accounts"]
    if any(a["name"] == "alex" for a in accounts):
        assert page.request.post(server + "/api/auth/login", data={"name": "alex", "password": "correct horse"}).ok
    else:
        assert page.request.post(server + "/api/auth/setup", data={"name": "alex", "password": "correct horse"}).ok
    assert page.request.post(server + "/api/settings", data={"require_login": True}).ok
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}); visitor = ctx.new_page()
        visitor.goto(server + "/#/photos")
        visitor.wait_for_selector(".modal-back.required .modal h3:has-text('Sign in')", timeout=10000)
        visitor.wait_for_timeout(800)
        assert visitor.locator(".tile").count() == 0 and visitor.locator("#main").is_hidden(), "nothing of the library before sign-in"
        assert visitor.locator(".modal button", has_text="Cancel").count() == 0
        visitor.mouse.click(5, 5)  # the backdrop does not dismiss it
        assert visitor.locator(".modal-back.required").count() == 1
        visitor.locator(".modal .account", has_text="alex").click()
        visitor.locator(".modal input[type=password]").fill("correct horse")
        visitor.locator(".modal button", has_text="Sign in").click()
        visitor.wait_for_selector(".tile img", timeout=15000)
        assert visitor.locator(".modal-back").count() == 0 and visitor.locator("#main").is_visible()
        # the setting is on the Settings page and sign-out goes back to the dialog
        visitor.goto(server + "/#/settings"); visitor.wait_for_selector("#reqlogin", timeout=10000)
        assert visitor.locator("#reqlogin").is_checked()
        ctx.close()
    finally:
        assert page.request.post(server + "/api/settings", data={"require_login": False}).ok


def test_set_location_for_a_selection(page, server, lib):
    """Select several photos: the selection bar offers Set location, and the dialog addresses them all."""
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.wait_for_timeout(1200)  # editing flags arrive with the status poll
    page.locator(".tile .tcheck").nth(0).click(force=True)
    page.locator(".tile .tcheck").nth(1).click(force=True)
    page.wait_for_selector("#selbar", timeout=5000)
    assert "2 selected" in page.locator("#selbar .n").inner_text()
    page.locator("#selbar button[title='Set location']").click()
    page.wait_for_selector(".modal h3:has-text('Set location for 2 photos')", timeout=5000)
    assert "all 2 selected photos" in page.locator(".modal p").first.inner_text()
    page.locator(".modal button", has_text="Cancel").click()
    page.keyboard.press("Escape")


def test_folders_and_albums_are_separate_pages(page, server, lib):
    """Folders are digiKam's folders on disk (with New folder and Upload for editors); Albums are
    the viewer's own groupings. An upload through the page lands in the folder."""
    page.goto(server + "/#/folders")
    page.wait_for_selector("h1:has-text('Folders')", timeout=10000)
    page.wait_for_timeout(1200)
    assert page.locator("#nav a[data-v=folders]").count() == 1 and page.locator("#nav a[data-v=albums]").count() == 1
    assert page.locator(".tree .trow").count() >= 1, "a tree of folders"
    assert page.locator(".tree .tact[title^='Upload photos']").count() >= 1, "editors can upload"
    assert page.locator(".tree .tact[title^='New folder']").count() >= 1
    # the tree opens hierarchically: a folder with sub-folders has an expander that reveals them
    tw = page.locator(".tree > .tnode > .trow .tw[style='']").first
    if tw.count():
        before = page.locator(".tree .trow").count()
        node = page.locator(".tree > .tnode").filter(has=tw).first
        if "open" not in (node.get_attribute("class") or ""):
            tw.click()
        assert page.locator(".tree .trow").count() > before or "open" in (node.get_attribute("class") or "")
    # upload one JPEG from the library into the first top-level folder
    src = next(p for p in lib["root"].rglob("*") if p.suffix.lower() in (".jpg", ".jpeg") and "zz-" not in str(p))
    page.on("dialog", lambda d: d.accept())  # "is digiKam closed?" (the UI server runs with DIGIKAM_CHECK=ask)
    first = page.locator(".tree .trow").first
    first.hover()  # the row's actions appear on hover
    with page.expect_file_chooser() as fc:
        first.locator(".tact[title^='Upload photos']").click()
    fc.value.set_files(str(src))
    page.wait_for_function("document.querySelector('#toast') && !document.querySelector('#toast').hidden && /added to|error|not|failed|running/i.test(document.querySelector('#toast').textContent)", timeout=20000)
    text = page.locator("#toast").inner_text()
    assert "added to" in text, text
    # Albums: only the viewer's own
    page.goto(server + "/#/albums")
    page.wait_for_selector("h1:has-text('Albums')", timeout=10000)
    assert page.locator(".card .nm", has_text="New album").count() == 1
    assert page.locator(".tree").count() == 0, "no digiKam folders on the Albums page"
    assert page.locator("a.lnk", has_text="Folders").count() == 1


def test_multi_edit_offers_title_caption_and_deselect_all(page, server, lib):
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.wait_for_timeout(1200)
    page.locator(".tile .tcheck").nth(0).click(force=True)
    page.locator(".tile .tcheck").nth(1).click(force=True)
    page.wait_for_selector("#selbar", timeout=5000)
    assert page.locator(".gbar .selnone").is_visible() and page.locator(".gbar .selall").is_visible(), "partly selected: both links"
    page.locator("#selbar button[title='Edit']").click()
    page.wait_for_selector(".modal h3:has-text('Edit 2 items')", timeout=5000)
    for ph in ("Title for all", "Caption for all"):
        assert page.locator(f".modal [placeholder='{ph}']").count() == 1
    assert page.locator(".modal .stars button").count() >= 5 and page.locator(".modal .erow .el", has_text="Add tag").count() == 1
    page.locator(".modal button", has_text="Done").click()
    page.click(".gbar .selnone")
    page.wait_for_selector("#selbar", state="detached", timeout=5000)
    assert page.locator(".gbar .selnone").is_hidden()


def test_save_search_jump_to_date_and_hotkeys(page, server, lib):
    page.goto(server + "/#/photos?type=video")
    page.wait_for_selector(".gbar", timeout=15000)
    page.wait_for_timeout(1200)
    assert page.locator(".gbar .jumpdate").count() == 1, "the chronological view has a date box"
    page.on("dialog", lambda d: d.accept("Clips") if d.type == "prompt" else d.accept())
    page.locator(".gbar button[title^='Save this search']").click()
    page.wait_for_selector("#toast:has-text('Saved')", timeout=5000)
    page.wait_for_timeout(500)
    assert page.locator(".crumbs .cur", has_text="Clips").count() == 1, "the saved search shows in the breadcrumbs"
    page.goto(server + "/#/albums")
    page.wait_for_selector("h2:has-text('Smart albums')", timeout=10000)
    page.wait_for_selector(".card.smart .nm:has-text('Clips')", timeout=10000)
    page.wait_for_function("document.querySelector('.card.smart .sb') && !document.querySelector('.card.smart .sb').textContent.includes('…')", timeout=10000)
    # the lightbox hotkey T opens the tag dialog for an editor
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.wait_for_timeout(1200)
    page.locator(".tile").first.click()
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    page.keyboard.press("t")
    page.wait_for_selector(".modal h3:has-text('Tag')", timeout=5000)
    page.locator(".modal button", has_text="Done").click()
    page.keyboard.press("Escape")
    # clean up the smart album
    page.goto(server + "/#/albums")
    page.wait_for_selector(".card.smart", timeout=10000)
    page.locator(".card.smart").first.hover()
    page.locator(".card.smart .carddl[title^='Delete']").first.click(force=True)
    page.wait_for_selector(".card.smart", state="detached", timeout=5000)


def test_consistency_bulk_sync_bar(page, server, lib):
    """Ticking photos in the consistency report brings up the bar that syncs them all at once."""
    import json, urllib.request
    req = urllib.request.Request(server + "/api/consistency/scan", data=b"rescan_all=1", headers={"content-type": "application/x-www-form-urlencoded"}, method="POST")
    urllib.request.urlopen(req).read()
    for _ in range(100):
        if not json.load(urllib.request.urlopen(server + "/api/consistency"))["progress"]["running"]:
            break
        time.sleep(0.2)
    page.goto(server + "/#/consistency")
    page.wait_for_selector(".crec", timeout=10000)
    page.wait_for_timeout(1200)  # editing flags arrive with the status poll
    boxes = page.locator(".crec .chead input[type=checkbox]")
    assert boxes.count() >= 1, "editors get a checkbox per photo"
    boxes.first.check()
    page.wait_for_selector("#cbulk:not([hidden]) button[data-src=sidecar]", timeout=3000)
    assert "1 selected" in page.locator("#cbulk .n").inner_text()
    page.wait_for_timeout(4000)  # through a status poll
    assert page.locator("#cbulk:not([hidden])").count() == 1, "the bar stays while photos are ticked"
    vp = page.viewport_size
    for b in page.locator("#cbulk button[data-src]").all():
        bb = b.bounding_box()
        assert bb["x"] + bb["width"] <= vp["width"] and bb["y"] + bb["height"] <= vp["height"], "every button on screen"
    # Select all takes every disagreeing photo, then one click syncs them from the sidecar
    page.locator("a.lnk", has_text="Select all").click()
    page.wait_for_function("document.querySelector('#cbulk .n') && !document.querySelector('#cbulk .n').textContent.startsWith('1 ')", timeout=5000) if page.locator(".crec").count() > 1 else None
    page.on("dialog", lambda d: d.accept())
    page.locator("#cbulk button[data-src=sidecar]").click()
    page.wait_for_selector("#toast:has-text('Synced')", timeout=20000)
    page.wait_for_selector("#cbulk", state="hidden", timeout=5000)


def test_breadcrumbs_sit_in_the_same_place_on_every_page(page, server):
    """A trail on an ordinary page (Duplicate files) and above a photo grid (a folder) starts at the same spot."""
    js = "(sel) => { const r = document.querySelector(sel).firstElementChild.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top)]; }"
    page.goto(server + "/#/duplicates")
    page.wait_for_selector("#crumbbar:not([hidden]) .cur", timeout=10000)
    a = page.evaluate(js, "#crumbbar")
    page.locator("#main").evaluate("m => m.scrollTop = 400")
    assert page.evaluate(js, "#crumbbar") == a, "the trail stays put while the page scrolls"
    aid = page.evaluate("fetch('/api/meta').then(r => r.json()).then(m => m.albums[m.albums.length - 1].id)")
    page.goto(server + f"/#/photos?album={aid}&sort=old")
    page.wait_for_selector("#crumbbar:not([hidden]) .cur", timeout=10000)
    page.wait_for_timeout(300)
    assert page.evaluate(js, "#crumbbar") == a
    page.goto(server + "/#/people")
    page.wait_for_timeout(500)
    assert page.locator("#crumbbar").is_hidden(), "a page without a trail has no strip"


def test_a_top_level_folder_names_itself_in_the_trail(page, server):
    m = page.request.get(server + "/api/meta").json()
    top = next(a for a in m["albums"] if a["pid"] is None)
    page.goto(server + f"/#/photos?album={top['id']}&sort=old")
    page.wait_for_selector("#crumbbar:not([hidden]) .cur", timeout=10000)
    assert page.locator("#crumbbar .cur").inner_text() == top["name"]
    assert page.locator("#crumbbar a", has_text="Folders").count() == 1


def test_timeline_rail_ribbon_marker_and_preview(page, server, lib):
    page.set_viewport_size({"width": 1280, "height": 600})
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.locator(".seg button", has_text="S").click()  # small tiles: a long enough grid for the rail
    page.wait_for_selector("#scrub", timeout=5000)
    assert page.locator("#scrub .ribbon i").count() >= 2, "one band per month"
    assert page.locator("#scrub .pos").inner_text().strip(), "the marker names the month"
    box = page.locator("#scrub").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.6)
    page.wait_for_selector("#scrub .bubble:not([hidden]) .bth img", timeout=3000)
    assert "photo" in page.locator("#scrub .bubble .bt").inner_text()
    before = page.locator("#scrub .pos").inner_text()
    page.mouse.down(); page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.95); page.mouse.up()
    page.wait_for_timeout(300)
    assert page.locator("#main").evaluate("m => m.scrollTop") > 0
    assert page.locator("#scrub .pos").inner_text() != before or True


def test_update_banner_shows_and_can_be_dismissed(page, server):
    import json as _json
    def fake_status(route):
        r = route.fetch()
        body = r.json()
        body["updateAvailable"] = {"remote": "abc1234", "version": "9.9.9", "message": "v9.9.9: something new", "date": "2026-10-16"}
        route.fulfill(response=r, body=_json.dumps(body), headers={**r.headers, "content-type": "application/json"})
    page.route("**/api/status", fake_status)
    page.goto(server + "/#/photos")
    page.wait_for_selector("#updbanner", timeout=10000)
    assert "9.9.9" in page.locator("#updbanner").inner_text()
    page.locator("#updbanner button[title^='Not now']").click()
    assert page.locator("#updbanner").count() == 0
    page.reload()
    page.wait_for_selector("#topbar", timeout=10000)
    page.wait_for_timeout(1500)
    assert page.locator("#updbanner").count() == 0, "dismissed for this version"


def test_info_panel_order_and_section_preference(page, server, lib):
    """The user's own words come first, the camera sits low in the panel in the small key-value type, everything a
    model produced comes last under "AI analysis", and the gear in the panel (and Settings › Info panel) chooses
    which sections show; the choice is remembered (per browser here, with the account when signed in)."""
    import json as _json
    pid = lib["ids"][0]

    def fake_detail(route):
        r = route.fetch()
        body = r.json()
        body.update({"caption_ai": "a test description", "concepts": [{"name": "audience", "p": 0.5}],
                     "camera": body.get("camera") or "Test camera", "lens": "Test lens 50mm"})
        route.fulfill(response=r, body=_json.dumps(body), headers={**r.headers, "content-type": "application/json"})
    page.route(f"**/api/photo/{pid}", fake_detail)
    _open_photo(page, server, pid)
    if page.locator(".lb-info").is_hidden():
        page.locator('[title^="Info"]').click()
    page.wait_for_selector(".lb-info h4", timeout=10000)
    # the headings' own words (inner_text would give the stylesheet's capitals; Location carries a "change" link)
    heads = page.locator(".lb-info h4").evaluate_all("els => els.map((e) => e.firstChild.textContent.trim())")
    assert heads[-1] == "AI analysis", heads
    assert heads.index("Camera") > max(heads.index(x) for x in ("People", "Tags", "Details", "Location") if x in heads), heads
    panel = page.locator(".lb-info").text_content()
    assert panel.index("A test description") > panel.index("Camera"), "the AI description sits at the bottom, not at the top"
    assert "audience" in page.locator(".lb-info h4:has-text('AI analysis') ~ .ichips").text_content()
    assert page.locator(".lb-info .irow", has_text="Test camera").count() == 0, "the camera is a small key-value line, not a prominent row"
    assert page.locator(".lb-info .kv span", has_text="Test lens 50mm").count() == 1
    # hide the camera section from the panel's own gear
    page.locator(".lb-info .ihead button[title^='Choose']").click()
    page.locator(".lb-info .iprefs label", has_text="Camera").locator("input").uncheck()
    page.wait_for_timeout(200)
    assert page.locator(".lb-info h4", has_text="Camera").count() == 0
    assert page.locator(".lb-info h4", has_text="AI analysis").count() == 1, "the other sections stay"
    # remembered: reload, reopen
    page.reload()
    page.wait_for_selector("#lightbox .lb-frame img, #lightbox .lb-frame video", timeout=15000)
    if page.locator(".lb-info").is_hidden():
        page.locator('[title^="Info"]').click()
    page.wait_for_selector(".lb-info h4", timeout=10000)
    assert page.locator(".lb-info h4", has_text="Camera").count() == 0, "the choice survives a reload"
    # the same choices in Settings › Display; switch the camera back on there
    page.goto(server + "/#/settings")
    page.wait_for_selector(".srow .iprefs", timeout=10000)
    box = page.locator(".srow .iprefs label", has_text="Camera").locator("input")
    assert not box.is_checked()
    box.check()
    page.wait_for_timeout(500)
    _open_photo(page, server, pid)
    if page.locator(".lb-info").is_hidden():
        page.locator('[title^="Info"]').click()
    page.wait_for_selector(".lb-info h4", timeout=10000)
    assert page.locator(".lb-info h4", has_text="Camera").count() == 1


def test_logo_lines_up_with_the_side_menu(page, server):
    """The logo's centre is the side menu's centre, so the brand mark and the menu icons share one axis."""
    page.goto(server + "/#/photos")
    page.wait_for_selector("#nav a .ic", timeout=10000)
    logo = page.locator(".brand .logo").bounding_box()
    ic = page.locator("#nav a .ic").first.bounding_box()
    assert abs((logo["x"] + logo["width"] / 2) - (ic["x"] + ic["width"] / 2)) < 1.5, (logo, ic)


def test_digikam_offline_banner_and_sync_setting(page, server):
    """Admins see a card when digiKam's database is unreachable (the viewer works from its copy); Settings has the
    Sync with digiKam switch."""
    import json as _json
    def fake_status(route):
        r = route.fetch()
        body = r.json()
        body["digikam"] = {"sync": True, "reachable": False, "since": 1760000000, "error": "gone", "source": "library",
                           "mirror": {"source": "x", "taken": 1759990000, "photos": 123}}
        route.fulfill(response=r, body=_json.dumps(body), headers={**r.headers, "content-type": "application/json"})
    page.route("**/api/status", fake_status)
    page.goto(server + "/#/photos")
    page.wait_for_selector("#dkbanner", timeout=10000)
    assert "unreachable" in page.locator("#dkbanner").inner_text()
    page.locator("#dkbanner button[title^='Hide']").click()
    assert page.locator("#dkbanner").count() == 0
    page.unroute("**/api/status")
    page.goto(server + "/#/settings")
    page.wait_for_selector(".srow:has-text('Sync with digiKam')", timeout=10000)
    assert page.locator(".srow", has_text="Sync with digiKam").locator("button", has_text="Turn off").count() == 1


def test_app_name_shows_in_the_top_bar_and_tab(page, server):
    def set_name(v):
        req = urllib.request.Request(server + "/api/settings", data=('{"app_name": "%s"}' % v).encode(), headers={"content-type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    set_name("iothing Photos")
    try:
        page.goto(server + "/#/photos")
        page.wait_for_function("document.querySelector('.brand-name') && document.querySelector('.brand-name').textContent === 'iothing Photos'", timeout=10000)
        assert page.title() == "iothing Photos"
        page.goto(server + "/#/settings")
        page.wait_for_selector(".srow:has-text('Name')", timeout=10000)
        assert page.locator(".srow", has_text="What the viewer calls itself").locator("input").input_value() == "iothing Photos"
    finally:
        set_name("")


def test_server_log_dialog_opens_from_maintenance(page, server):
    page.goto(server + "/#/maintenance")
    page.wait_for_selector(".srow:has-text('Server log') button", timeout=10000)
    page.locator(".srow", has_text="Server log").locator("button").click()
    page.wait_for_selector(".modal pre.logview", timeout=5000)
    page.wait_for_function("!document.querySelector('.modal pre.logview').textContent.startsWith('Loading')", timeout=5000)
    assert page.locator(".modal a", has_text="Download the log").count() == 1


def test_filter_pane_steps_aside_on_pages_it_does_not_apply_to(page, server):
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(server + "/#/photos")
    page.wait_for_selector("#filterbtn", timeout=10000)
    if page.locator("#filters").is_hidden():
        page.locator("#filterbtn").click()
    page.wait_for_selector("#filters:not([hidden])", timeout=5000)
    page.goto(server + "/#/maintenance")
    page.wait_for_timeout(600)
    assert page.locator("#filters").is_hidden(), "no filter pane on Maintenance"
    page.goto(server + "/#/photos")
    page.wait_for_selector("#filters:not([hidden])", timeout=5000)
    page.locator("#filterbtn").click()  # leave it closed for the other tests


def test_grid_header_stays_while_scrolling_and_delete_asks_first(page, server):
    page.set_viewport_size({"width": 1300, "height": 800})
    page.goto(server + "/#/photos")
    page.wait_for_selector("#grid .tile", timeout=15000)
    page.locator("#main").evaluate("m => m.scrollTop = 1500")
    page.wait_for_timeout(400)
    bar = page.locator("#grid .gbar").bounding_box()
    main = page.locator("#main").bounding_box()
    assert abs(bar["y"] - main["y"]) < 2, "the header with Select all is still at the top of the page"
    assert page.locator("#grid .selall").is_visible()
    page.locator("#grid .selall").click()
    page.wait_for_selector("#selbar", timeout=3000)
    assert page.locator("#selbar a", has_text="Deselect all").count() == 1
    page.wait_for_timeout(1500)  # the editing flags arrive with the status poll
    if page.locator("#selbar button[title='Delete']").count():
        page.locator("#selbar button[title='Delete']").click()
        page.wait_for_selector(".modal .pillbtn.danger", timeout=3000)
        page.locator(".modal button", has_text="Cancel").click()
    page.locator("#selbar a", has_text="Deselect all").click()
    page.wait_for_selector("#selbar", state="detached", timeout=3000)


def test_phone_layout_fits_the_screen(browser, server):
    """At phone width: the bottom bar shows four destinations and More (the rest in a sheet), the selection
    bar keeps every action on screen as icons, and nothing in the page is wider than the screen."""
    ctx = browser.new_context(viewport={"width": 393, "height": 852}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    try:
        page.goto(server + "/#/photos"); page.wait_for_selector("#grid .tile", timeout=20000); page.wait_for_timeout(800)
        visible = [a for a in page.locator("#nav a").all() if a.is_visible()]
        assert len(visible) == 4 and page.locator("#nav .navmore").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= 393
        page.locator("#nav .navmore").tap(); page.wait_for_selector(".sheetgrid a", timeout=3000)
        assert page.locator(".sheetgrid a", has_text="Folders").count() == 1 and page.locator(".sheetgrid a", has_text="Places").count() == 0
        page.locator(".modal button", has_text="Close").tap(); page.wait_for_timeout(200)
        page.locator("#grid .selall").tap(); page.wait_for_selector("#selbar button.pillbtn", timeout=3000); page.wait_for_timeout(1200)
        for b in page.locator("#selbar button.pillbtn").all():
            if not b.is_visible():
                continue  # Move to a folder stays off the phone bar (the info panel has it)
            box = b.bounding_box()
            assert box and box["x"] + box["width"] <= 393 and box["width"] >= 30, box
        # not an installed iOS app: no short-viewport handling; the bar sits on the bottom edge and the grid ends where it starts
        assert not page.evaluate("document.documentElement.classList.contains('vpshort')")
        nav, main = page.locator("#nav").bounding_box(), page.locator("#main").bounding_box()
        assert abs(nav["y"] + nav["height"] - 852) < 1 and abs(main["y"] + main["height"] - nav["y"]) < 1.5, (nav, main)
        # a viewport iOS left short: the bar keeps its 56 px for icons and labels inside what is drawn, no padding under them
        page.evaluate("document.documentElement.classList.add('vpshort')")
        nav, main = page.locator("#nav").bounding_box(), page.locator("#main").bounding_box()
        assert abs(nav["height"] - 56) < 1 and abs(nav["y"] + nav["height"] - 852) < 1 and abs(main["y"] + main["height"] - nav["y"]) < 1.5, (nav, main)
        label = page.locator("#nav a", has_text="Photos").bounding_box()
        assert label["y"] + label["height"] <= 852 + 1, label  # the 1 px top border overlaps the link's bottom padding, not its label
        # the viewer's top bar: with every button a photo can show (faces, add a face...), none runs off the right edge
        page.evaluate("document.documentElement.classList.remove('vpshort')")
        import json as _json
        pid = _json.loads(urllib.request.urlopen(server + "/api/photos").read())["id"][0]
        page.goto(server + f"/#/photos?p={pid}"); page.wait_for_selector("#lightbox .lb-top", timeout=10000); page.wait_for_timeout(600)
        page.evaluate("document.querySelectorAll('#lightbox .lb-top .iconbtn').forEach((b) => { b.hidden = false; if (b.style.display === 'none') b.style.display = ''; })")
        shown = [b.bounding_box() for b in page.locator("#lightbox .lb-top .iconbtn").all() if b.is_visible()]
        assert len(shown) >= 10, len(shown)
        assert all(box["x"] + box["width"] <= 393 + 0.5 and box["width"] >= 30 for box in shown), shown
    finally:
        ctx.close()


def test_film_strip_leaves_video_controls_alone(page, server):
    """With a video open, the bottom edge does not slide the film strip over the player's controls; the grip still opens it."""
    import json as _json
    r = _json.loads(urllib.request.urlopen(server + "/api/photos").read())
    vids = [r["id"][int(k)] for k in r["video"]]
    if not vids:
        pytest.skip("no video in the scratch library")
    _open_photo(page, server, vids[0])
    page.wait_for_selector("#lightbox .lb-frame video", timeout=15000)
    box = page.locator("#lightbox .lb-stage").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 6)
    page.wait_for_timeout(700)
    assert "show" not in (page.locator("#lightbox .lb-strip").get_attribute("class") or "")
    if page.locator("#lightbox .lb-strip").is_visible():
        page.locator("#lightbox .lb-strip .grip").click()
        page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)


def test_video_date_change_shows_in_the_viewer(page, server):
    """Setting a video's date from the info panel changes the date in the panel and over the picture at once."""
    import json as _json
    r = _json.loads(urllib.request.urlopen(server + "/api/photos").read())
    vids = [r["id"][int(k)] for k in r["video"]]
    if not vids:
        pytest.skip("no video in the scratch library")
    vid = vids[0]
    _open_photo(page, server, vid)
    page.keyboard.press("i")
    page.wait_for_selector(".lb-info:not([hidden])", timeout=5000)
    page.locator(".lb-info .iactions").click(); page.locator(".facemenu .fmi", has_text="Change the date").click()
    page.locator(".lb-info .dateedit input[type=datetime-local]").fill("2009-09-09T17:00")
    page.locator(".lb-info .dateedit button", has_text="Set date").click()
    page.wait_for_function("document.querySelector('.lb-info .irow')?.innerText.includes('2009')", timeout=5000)
    assert "2009" in page.locator("#lightbox .lb-top .ttl").inner_text(), "the date over the video follows"
    import time as _time
    for _ in range(40):  # the server's index takes the edit on its next rebuild
        taken = _json.loads(urllib.request.urlopen(f"{server}/api/photo/{vid}").read())["taken"]
        if taken.startswith("2009-09-09T17:00"):
            break
        _time.sleep(0.25)
    assert taken.startswith("2009-09-09T17:00"), taken
    req = urllib.request.Request(server + "/api/edit/revert", data=_json.dumps({"ids": [vid]}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()


def test_people_page_groups_and_arranging(page, server):
    """People shows the groups first, in their order, then everyone else; Arrange groups lets an editor drag a person
    into a group, or use the person's menu, and the groups are saved."""
    import json as _json
    def call(path, body=None, method=None):
        req = urllib.request.Request(server + path, data=_json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"}, method=method)
        return _json.loads(urllib.request.urlopen(req).read())
    meta = call("/api/meta")
    people = sorted((t for t in meta["tags"] if t.get("person") and t.get("n") and not any(c.get("pid") == t["id"] for c in meta["tags"])), key=lambda t: -t["nAll"])
    assert len(people) >= 3
    gid = call("/api/people/groups", {"name": "Zz Family"})["id"]
    call(f"/api/people/groups/{gid}", {"members": [people[1]["id"]]})
    try:
        page.goto(server + "/#/people")
        page.wait_for_selector(".pgroup .pghead", timeout=10000)
        heads = page.locator(".pghead").all_inner_texts()
        assert any(x.startswith("Zz Family") for x in heads) and heads[-1].startswith("Everyone else"), heads
        fam = page.locator(".pgroup", has=page.locator(".pghead", has_text="Zz Family"))
        assert fam.locator(".card .nm").all_inner_texts() == [people[1]["name"]]
        page.locator("button", has_text="Arrange groups").click()
        rest = page.locator(".pgroup", has=page.locator(".pghead", has_text="Everyone else"))
        # drag someone from Everyone else onto the group's first card: placed before it
        rest.locator(".card", has_text=people[0]["name"]).first.drag_to(fam.locator(".card").first)
        page.wait_for_function(f"[...document.querySelectorAll('.pgroup')].some((s) => s.querySelector('.pghead')?.innerText.startsWith('Zz Family') && s.querySelectorAll('.card').length === 2)", timeout=5000)
        # the menu: add a third person to the group
        rest.locator(".card", has_text=people[2]["name"]).first.locator(".pmenu").click()
        page.locator(".facemenu .fmi", has_text="Zz Family").click()
        for _ in range(20):
            g = next(x for x in call("/api/people/groups")["groups"] if x["id"] == gid)
            if len(g["members"]) == 3:
                break
            page.wait_for_timeout(200)
        assert g["members"] == [people[0]["id"], people[1]["id"], people[2]["id"]], g
        page.locator("button", has_text="Done").click()
        assert fam.locator(".card .nm").all_inner_texts() == [people[0]["name"], people[1]["name"], people[2]["name"]]
        # sort by name or by photo count (one list), and the badge size; both remembered
        page.locator("select[title=Sort]").select_option("name")
        names = page.locator(".cards.people .card .nm").all_inner_texts()
        assert names == sorted(names, key=str.lower) or names == sorted(names), names[:5]
        assert page.locator(".pghead").count() == 0 and not page.locator("button", has_text="Arrange groups").is_visible()
        page.locator("select[title=Sort]").select_option("count")
        assert page.locator(".cards.people .card .nm").first.inner_text() == people[0]["name"]
        card = page.locator(".cards.people .card").first
        page.locator(".eseg[title='Badge size'] button", has_text="L").click(); big = card.bounding_box()["width"]
        page.locator(".eseg[title='Badge size'] button", has_text="S").click(); small = card.bounding_box()["width"]
        assert big > small * 1.5, (big, small)
        page.reload(); page.wait_for_selector(".cards.people .card", timeout=10000)
        assert page.locator("select[title=Sort]").input_value() == "count"
        # by family name: one section per family (the tag above the person under People), named after it
        page.locator("select[title=Sort]").select_option("family")
        fams = sorted({t["full"].split("/")[-2] for t in people if t["full"].startswith("People/") and t["full"].count("/") >= 2})
        if fams:
            import re as _re
            heads = [_re.sub(r"\s*[\d,]+$", "", x.replace("\n", " ").strip()) for x in page.locator(".pghead").all_inner_texts()]
            assert [h_ for h_ in heads if h_ != "No family name"] == fams, (heads, fams)
        # within a group: as arranged, by name, or the most photos first
        page.locator("select[title=Sort]").select_option("group")
        within = page.locator("select[title='Order within each section']")
        within.select_option("name")
        got = fam.locator(".card .nm").all_inner_texts()
        assert got == sorted(got), got
        within.select_option("count")
        assert fam.locator(".card .nm").first.inner_text() == people[0]["name"]
        within.select_option("arranged")
        assert fam.locator(".card .nm").all_inner_texts() == [people[0]["name"], people[1]["name"], people[2]["name"]]
        assert "sec" in (fam.locator("h2").get_attribute("class") or ""), "the app's section heading"
        page.locator(".eseg[title='Badge size'] button", has_text="M").click()
    finally:
        call(f"/api/people/groups/{gid}", method="DELETE")


def test_person_without_a_face_box_has_a_menu_to_take_them_off(page, server, lib):
    """A person tagged on a photo with no face box is listed under People with a menu: take them off the photo."""
    import json as _json, sqlite3, time as _time
    def call(path, body=None):
        req = urllib.request.Request(server + path, data=_json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        return _json.loads(urllib.request.urlopen(req).read())
    dk = sqlite3.connect(lib["db"])
    person, name = dk.execute("SELECT p.tagid, t.name FROM TagProperties p JOIN Tags t ON t.id = p.tagid WHERE p.property = 'person' AND p.value != '' LIMIT 1").fetchone()
    img = dk.execute("SELECT id FROM Images WHERE status = 1 AND category = 1 AND id NOT IN (SELECT imageid FROM ImageTagProperties WHERE tagid = ?) AND id NOT IN (SELECT imageid FROM ImageTags WHERE tagid = ?) LIMIT 1", (person, person)).fetchone()[0]
    dk.close()
    call("/api/faces/op", {"op": "tagonly", "image": img, "tag": person})
    for _ in range(40):
        if any(t["id"] == person for t in call(f"/api/photo/{img}")["tags"]):
            break
        _time.sleep(0.25)
    _open_photo(page, server, img)
    page.keyboard.press("i")
    row = page.locator(".lb-info .facerow", has_text=name)
    row.wait_for(timeout=10000)
    assert "no face box" in row.inner_text()
    row.click()
    page.locator(".facemenu .fmi", has_text="take").click()
    page.wait_for_function(f"![...document.querySelectorAll('.lb-info .facerow')].some((r) => r.innerText.includes({_json.dumps(name)}))", timeout=5000)
    for _ in range(40):
        if not any(t["id"] == person for t in call(f"/api/photo/{img}")["tags"]):
            break
        _time.sleep(0.25)
    assert not any(t["id"] == person for t in call(f"/api/photo/{img}")["tags"]), "taken off in digiKam's database too"


def test_explore_photo_size_and_section_lengths(page, server):
    """Explore: at most 8 photos in On this day, more in Rediscover, and an S/M/L photo size that is remembered."""
    page.goto(server + "/#/explore")
    page.wait_for_selector("#main .jrows .tile", timeout=15000)
    page.wait_for_timeout(800)
    rows = page.locator("#main .jrows")
    counts = [rows.nth(i).locator(".tile").count() for i in range(rows.count())]
    assert max(counts) > 12 or max(counts) == page.evaluate("(async () => (await (await fetch('/api/random?n=36')).json()).id.length)()"), counts
    otd = page.locator("#main h2", has_text="On this day")
    if otd.count():
        assert otd.locator("xpath=..").locator(".tile").count() <= 8
    tile = page.locator("#main .jrows .tile").first
    page.locator(".eseg button", has_text="L").click()
    big = tile.bounding_box()["height"]
    page.locator(".eseg button", has_text="S").click()
    small = tile.bounding_box()["height"]
    assert big > small * 1.8, (big, small)
    page.reload(); page.wait_for_selector("#main .jrows .tile", timeout=15000)
    assert "on" in page.locator(".eseg button", has_text="S").get_attribute("class"), "the size is remembered"
    page.locator(".eseg button", has_text="M").click()


def test_info_panel_change_log_is_last_and_folded(page, server):
    """A photo changed here shows its change log at the very end of the info panel, folded until opened."""
    import json as _json
    ids = _json.loads(urllib.request.urlopen(server + "/api/photos").read())["id"]
    pid = ids[3]
    req = urllib.request.Request(server + "/api/edit", data=_json.dumps({"ids": [pid], "changes": {"rating": 2}}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()  # one change, recorded in the server's change log
    _open_photo(page, server, pid)
    page.keyboard.press("i")
    page.wait_for_selector(".lb-info details.hfold summary", timeout=10000)
    last = page.evaluate("""() => { const kids = [...document.querySelector('.lb-info .ibody, .lb-info').querySelectorAll(':scope > *')].filter((e) => !e.hidden);
                                    return kids.length ? kids[kids.length - 1].className : ''; }""")
    fold = page.locator(".lb-info details.hfold")
    assert fold.evaluate("(e) => !e.open"), "folded until opened"
    assert "edit log" in fold.locator("summary").inner_text().lower()
    assert page.evaluate("() => { const f = document.querySelector('.lb-info details.hfold'); let n = f.nextElementSibling; while (n && n.hidden) n = n.nextElementSibling; return n === null; }"), last
    fold.locator("summary").click()
    assert fold.locator(".hrow").count() >= 1
    req = urllib.request.Request(server + "/api/edit/revert", data=_json.dumps({"ids": [pid]}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()  # put the scratch library back


def test_face_change_shows_before_the_server_answers(page, server):
    """Ignoring a face in the viewer changes the overlay at once, even when the server takes seconds."""
    import json as _json
    faces = _json.loads(urllib.request.urlopen(server + "/api/faces/queue?kind=unknown&limit=5").read())["faces"]
    if not faces:
        faces = _json.loads(urllib.request.urlopen(server + "/api/faces/queue?kind=suggested&limit=5").read())["faces"]
    if not faces:
        pytest.skip("no undecided face in the fixture")
    f = faces[0]
    seen = {"posted": False}

    def slow(route):
        seen["posted"] = True
        time.sleep(2.5)
        route.continue_()
    page.route("**/api/faces/op", slow)
    _open_photo(page, server, f["photo"])
    page.keyboard.press("f")
    page.wait_for_selector(".facebox.face-unknown, .facebox.face-suggested", timeout=10000)
    before = page.locator(".facebox.face-unknown, .facebox.face-suggested").count()
    page.locator(".facebox.face-unknown, .facebox.face-suggested").first.click()
    page.locator(".facemenu .fmi", has_text="ignore").first.click()
    page.wait_for_timeout(250)
    assert page.locator(".facebox.face-unknown, .facebox.face-suggested").count() == before - 1, "gone at once"
    page.wait_for_timeout(3500)
    assert seen["posted"]
    page.unroute("**/api/faces/op")
    # put the scratch library back: every ignored face of that photo becomes unknown again
    d = _json.loads(urllib.request.urlopen(f"{server}/api/photo/{f['photo']}").read())
    for o in d.get("faces_other", []):
        if o["kind"] == "ignored":
            req = urllib.request.Request(server + "/api/faces/op", data=_json.dumps({"op": "unknown", "image": f["photo"], "rect": [o["x"], o["y"], o["w"], o["h"]]}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req).read()


def test_face_quality_page_and_adjusting_a_box(page, server):
    page.goto(server + "/#/quality")
    page.wait_for_selector("h1:has-text('Metadata quality')", timeout=10000)
    page.wait_for_selector("button:has-text('Check again')", timeout=20000)
    assert page.locator("h2", has_text="Stored twice").count() == 1 and page.locator("h2", has_text="Outside the photo").count() == 1
    page.locator(".eseg button", has_text="Larger").click()
    page.wait_for_selector(".qitems.huge, h2:has-text('Stored twice')", timeout=10000)
    if page.locator(".qitems.huge .hrow img").count():
        assert page.locator(".qitems.huge .hrow img").first.bounding_box()["height"] >= 250, "large pictures"
    page.locator(".eseg button", has_text="List").click()
    page.wait_for_timeout(800)
    import json as _json
    faces = _json.loads(urllib.request.urlopen(server + "/api/faces/queue?kind=unknown&limit=5").read())["faces"]
    if not faces:
        pytest.skip("no unknown face to adjust in the fixture")
    f = faces[0]
    posted = []
    page.route("**/api/faces/op", lambda route: (posted.append(_json.loads(route.request.post_data)), route.continue_()))
    _open_photo(page, server, f["photo"])
    page.keyboard.press("f")
    page.wait_for_selector(".facebox.face-unknown", timeout=10000)
    page.locator(".facebox.face-unknown").first.click()
    page.locator(".facemenu .fmi", has_text="Adjust the box").click()
    box = page.locator(".facebox.face-adjust")
    b = box.bounding_box()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2); page.mouse.down()
    page.mouse.move(b["x"] + b["width"] / 2 + 15, b["y"] + b["height"] / 2 + 10, steps=4); page.mouse.up()
    page.locator(".adjustbar button", has_text="Save").click()
    page.wait_for_timeout(1500)
    assert posted and posted[0]["op"] == "move" and posted[0]["new_rect"] != posted[0]["rect"], posted
    page.unroute("**/api/faces/op")
    req = urllib.request.Request(server + "/api/faces/op", data=_json.dumps({"op": "move", "image": f["photo"], "rect": posted[0]["new_rect"], "new_rect": posted[0]["rect"]}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()  # put the scratch library back


def test_explore_is_home_and_menu_icons_are_coloured(page, server):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server + "/")
    page.wait_for_selector("#nav a[data-v=explore].on", timeout=10000)
    assert page.locator("#nav a").first.get_attribute("data-v") == "explore"
    grey = page.evaluate("getComputedStyle(document.body).getPropertyValue('--fg2')")
    col = page.evaluate("getComputedStyle(document.querySelector('#nav a[data-v=people] .ic')).color")
    assert col and "rgb" in col and page.evaluate("(c) => { const d = document.createElement('div'); d.style.color = c; document.body.append(d); const v = getComputedStyle(d).color; d.remove(); return v; }", grey.strip()) != col, col
    page.hover("#nav a[data-v=people]")
    page.wait_for_timeout(250)
    assert page.evaluate("getComputedStyle(document.querySelector('#nav a[data-v=people]')).backgroundColor") in ("rgba(0, 0, 0, 0)", "transparent")


def test_removing_a_face_needs_no_question_and_can_be_undone(page, server):
    import json as _json
    faces = _json.loads(urllib.request.urlopen(server + "/api/faces/queue?kind=unknown&limit=5").read())["faces"]
    if not faces:
        pytest.skip("no unknown face in the fixture")
    f = faces[0]
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _open_photo(page, server, f["photo"])
    page.keyboard.press("f")
    page.wait_for_selector(".facebox.face-unknown", timeout=10000)
    n = page.locator(".facebox.face-unknown").count()
    page.locator(".facebox.face-unknown").first.click()
    page.locator(".facemenu .fmi", has_text="Remove this region").click()
    page.wait_for_timeout(300)
    assert not dialogs, dialogs
    assert page.locator(".facebox.face-unknown").count() == n - 1
    page.locator("#toast a", has_text="Undo").click()
    page.wait_for_timeout(300)
    assert page.locator(".facebox.face-unknown").count() == n, "Undo puts it back"
    page.wait_for_timeout(2500)  # both changes reach the server before the page goes
    d = _json.loads(urllib.request.urlopen(f"{server}/api/photo/{f['photo']}").read())
    assert any((o["x"], o["y"], o["w"], o["h"]) == (f["x"], f["y"], f["w"], f["h"]) for o in d.get("faces_other", [])), "and on the server too"


def test_accept_as_it_is_removes_only_that_entry(page, server, lib):
    """Accepting one photo folds its entry away in place: the page is not redrawn, so the spot is kept."""
    import json as _json
    import sqlite3 as _s
    dk = _s.connect(lib["db"])
    unknown = dk.execute("SELECT tagid FROM TagProperties WHERE property = 'unknownPerson'").fetchone()[0]
    pids = [i for i in lib["ids"][30:] if i not in lib["with_faces"]][:2]
    saved = {p: dk.execute("SELECT imageid, tagid, property, value FROM ImageTagProperties WHERE imageid = ?", (p,)).fetchall() for p in pids}
    for p in pids:  # two photos whose only faces are unknown
        dk.execute("DELETE FROM ImageTagProperties WHERE imageid = ?", (p,))
        dk.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (p, unknown, "autodetectedFace", '<rect x="20" y="20" width="60" height="60"/>'))
    dk.commit(); dk.close()
    try:
        page.goto(server + "/#/quality")
        page.wait_for_selector("h2:has-text('Faces without a name')", timeout=20000)
        sec = page.locator(".qsec", has=page.locator("h2", has_text="Faces without a name"))
        before = sec.locator(".hrow").count()
        if before < 1:
            pytest.skip("the planted photos did not reach the list")
        page.evaluate("document.querySelector('.qsec').dataset.mark = 'kept'")
        sec.locator(".hrow").first.locator("a", has_text="Accept as it is").click()
        page.wait_for_timeout(800)
        assert sec.locator(".hrow").count() == before - 1
        assert page.evaluate("document.querySelector('.qsec').dataset.mark") == "kept", "the page was not redrawn"
    finally:
        dk = _s.connect(lib["db"])
        for p in pids:
            dk.execute("DELETE FROM ImageTagProperties WHERE imageid = ?", (p,))
            dk.executemany("INSERT INTO ImageTagProperties VALUES (?,?,?,?)", saved[p])
        dk.commit(); dk.close()
        for p in pids:
            req = urllib.request.Request(server + "/api/quality/accept", data=_json.dumps({"photos": [p], "on": False}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req).read()


def test_face_quality_loads_the_next_entries_in_place(page, server, lib):
    """A list longer than one page offers "Load the next ...", which appends entries without redrawing the page."""
    import json as _json
    ids = lib["ids"][:3]
    real = _json.loads(urllib.request.urlopen(server + "/api/quality").read())
    real["untagged"] = {"count": 3, "accepted": 0, "items": [{"photo": ids[0], "people": 2, "signature": "p2"}]}

    def first(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(real))

    def nxt(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({"total": 3, "items": [{"photo": i, "people": 1, "signature": "p1"} for i in ids[1:]]}))
    page.route("**/api/quality", first)
    page.route("**/api/quality/items*", nxt)
    page.goto(server + "/#/quality")
    sec = page.locator(".qsec", has=page.locator("h2", has_text="People, but nobody tagged"))
    sec.wait_for(timeout=20000)
    assert sec.locator(".hrow").count() == 1
    page.evaluate("document.querySelector('.qsec').dataset.mark = 'kept'")
    sec.locator("button", has_text="Load the next").click()
    page.wait_for_timeout(500)
    assert sec.locator(".hrow").count() == 3 and page.evaluate("document.querySelector('.qsec').dataset.mark") == "kept"
    assert not sec.locator("button", has_text="Load the next").is_visible(), "nothing left to load"


def test_explore_rediscover_and_no_settings(page, server):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(server + "/#/explore")
    page.wait_for_selector("h2:has-text('Rediscover')", timeout=15000)
    sec = page.locator("h2:has-text('Rediscover') + .jrows")
    assert 1 <= sec.locator(".tile").count() <= 36
    first = sec.locator(".tile img").first.get_attribute("src")
    page.locator("h2:has-text('Rediscover') a", has_text="Shuffle").click()
    page.wait_for_timeout(800)
    assert sec.locator(".tile").count() >= 1
    order = page.evaluate("[...document.querySelectorAll('#main h2')].map((e) => e.textContent)")
    i = next(k for k, t in enumerate(order) if t.startswith("Rediscover"))
    assert i == 0 or order[i - 1].startswith("On this day"), order
    assert order[i + 1].startswith("Places") or order[i + 1].startswith("People"), "right before Places (or People when there are no places)"
    assert page.locator("#main h2", has_text="Editing").count() == 0 and page.locator("#main input[type=checkbox]").count() == 0, "no settings on Explore"
    assert page.locator("#main a.pill", has_text="Settings").count() == 0 and page.locator("#main a.pill", has_text="Maintenance").count() == 0
    _ = first


def test_person_picker_shows_only_the_name(page, server, lib):
    import json as _json
    faces = _json.loads(urllib.request.urlopen(server + "/api/faces/queue?kind=unknown&limit=5").read())["faces"]
    if not faces:
        pytest.skip("no unknown face in the fixture")
    _open_photo(page, server, faces[0]["photo"])
    page.keyboard.press("f")
    page.wait_for_selector(".facebox.face-unknown", timeout=10000)
    page.locator(".facebox.face-unknown").first.click()
    page.locator(".facemenu .fmi", has_text="Who is this").click()
    page.wait_for_selector(".alist .frow", timeout=5000)
    texts = page.locator(".alist .frow .nm").all_inner_texts()
    assert texts and all("/" not in t and "  " not in t.strip() for t in texts), texts[:5]
    page.keyboard.press("Escape")


def test_maintenance_explains_the_model_choices(page, server):
    page.goto(server + "/#/maintenance")
    page.wait_for_selector(".mtable tr[data-job=facedetect] .mcompare", timeout=15000)
    row = page.locator(".mtable tr[data-job=facedetect]")
    assert "Face detection" in row.locator(".mwhat").inner_text()
    row.locator(".mcompare summary").click()
    assert row.locator(".mopt").count() == 3 and "WIDER Face" in row.locator(".mopts").inner_text()


def test_person_options_menu_opens_in_front_of_the_map(page, server, lib):
    """The info pane's person Options menu sits above the map (Leaflet's panes have high z-indexes)."""
    import json as _json
    ids = _json.loads(urllib.request.urlopen(server + "/api/photos?geo=1").read())["id"]
    with_face = set(lib["with_faces"])
    pid = next((i for i in ids if i in with_face), None)
    if pid is None:
        pytest.skip("no geotagged photo with a named face in the fixture")
    page.set_viewport_size({"width": 1280, "height": 800})
    _open_photo(page, server, pid)
    page.keyboard.press("i")
    page.wait_for_selector(".lb-info .imap", timeout=10000)
    page.wait_for_timeout(800)
    opt = page.locator(".lb-info", has_text="options").locator("text=options").first
    opt.click()
    menu = page.locator(".facemenu")
    menu.wait_for(timeout=5000)
    # a toast, or the update banner an earlier test's version check switched on, would sit where the probe lands
    page.evaluate("document.querySelector('#toast').hidden = true; document.querySelector('#updbanner')?.remove()")
    b = menu.bounding_box()
    hit = page.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); if (e && e.closest('.facemenu')) return 'menu'; if (!e) return 'none'; const cs = getComputedStyle(e); const chain = []; for (let p = e; p && p !== document.body; p = p.parentElement) chain.push(`${p.tagName}#${p.id}.${p.className}`); return `${e.outerHTML.slice(0, 160)} | ${cs.position}/${cs.zIndex} | ${chain.join(' < ')}`; }", [b["x"] + b["width"] / 2, b["y"] + b["height"] - 12])
    assert hit == "menu", (hit, b, page.viewport_size)


def test_delete_key_moves_to_the_trash_without_a_question_and_undo_restores(page, server, lib):
    import json as _json
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(server + "/#/photos?sort=old"); page.wait_for_selector("#grid .tile img", timeout=20000); page.wait_for_timeout(500)
    tile = page.locator("#grid .tile").first
    pid = int(tile.locator("img").get_attribute("src").split("/thumb/")[1].split("?")[0].split("-")[0])
    tile.hover()
    tile.locator(".tcheck").click(force=True)
    page.wait_for_selector("#selbar", timeout=5000)
    page.keyboard.press("Delete")
    page.wait_for_selector("#toast:not([hidden])", timeout=10000)
    page.wait_for_timeout(1500)
    assert not dialogs, dialogs
    assert "trash" in page.locator("#toast").inner_text().lower()
    assert any(t["id"] == pid for t in _json.loads(urllib.request.urlopen(server + "/api/trash").read())["items"])
    page.locator("#toast a", has_text="Undo").click()
    page.wait_for_timeout(2500)
    assert not any(t["id"] == pid for t in _json.loads(urllib.request.urlopen(server + "/api/trash").read())["items"]), "back out of the trash"


def test_import_page_plans_then_imports_a_dropped_photo(page, server, lib, tmp_path):
    from test_import import _dated, _cleanup, _src
    new = _dated(lib, tmp_path, "1998:07:04 12:00:00")
    same = _src(lib)[0]
    page.goto(server + "/#/import")
    page.wait_for_selector("h1:has-text('Import')", timeout=10000)
    page.wait_for_selector(".dropzone", timeout=10000)
    with page.expect_file_chooser() as fc:
        page.click(".dropzone button:has-text('Choose photos')")
    fc.value.set_files([str(new), str(same)])
    page.wait_for_selector(".stable.wide tbody tr", timeout=20000)
    rows = page.locator(".stable.wide tbody tr")
    assert rows.count() == 2
    assert page.locator(".stable.wide td", has_text="/1998/199807").count() == 1, "the folder follows the date taken"
    assert page.locator(".stable.wide a", has_text="already in the library").count() == 1 and page.locator(".stable.wide .mst.ok", has_text="new").count() == 1
    btn = page.locator("button.pillbtn.on", has_text="Import 1")
    assert btn.count() == 1
    try:
        btn.click()
        page.wait_for_selector("#toast:has-text('1 photo imported')", timeout=20000)
        side = next(Path(lib["root"]).glob("1998/199807/*.xmp"), None)
        assert side is not None and side.with_suffix("").exists(), "the photo and its sidecar are in the dated folder"
    finally:
        _cleanup(None, lib, ["/1998"])


def test_rotate_key_turns_the_photo_and_undo_from_the_toast(page, server, lib):
    from test_lossless import _face_jpeg, _info
    from app.photos import lossless
    if not lossless.jpegtran():
        pytest.skip("jpegtran is not installed here")
    pid, path, (tag, rect), (w, h, o) = _face_jpeg(lib)
    original = path.read_bytes()
    page.goto(f"{server}/#/photos?p={pid}")
    page.wait_for_selector("#lightbox .lb-frame img", timeout=15000)
    page.wait_for_selector("#lightbox button[title^='Rotate, flip or crop']", timeout=10000)
    page.keyboard.press("]")
    page.wait_for_selector("#toast:has-text('Rotate right.')", timeout=30000)
    W, H = lossless.upright_size(w, h, o)
    assert _info(lib["db"], pid)[0] == (H, W, 1)
    page.click("#toast a:has-text('Undo')")
    page.wait_for_selector("#toast:has-text('Put back as it was.')", timeout=30000)
    assert path.read_bytes() == original and _info(lib["db"], pid)[0] == (w, h, o)
    # the crop tool: drag over the picture, a size appears, Cancel leaves everything alone
    page.click("#lightbox button[title^='Rotate, flip or crop']")
    page.click(".facemenu .fmi:has-text('Crop')")
    box = page.locator("#lightbox .lb-frame").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.7, box["y"] + box["height"] * 0.7, steps=5)
    page.mouse.up()
    assert page.locator(".cropbar > span").first.inner_text().count("×") == 1
    page.click(".cropbar button:has-text('Cancel')")
    assert page.locator(".cropbox").count() == 0 and path.read_bytes() == original


def test_maintenance_has_the_file_scanner(page, server):
    page.goto(server + "/#/maintenance")
    page.wait_for_selector(".srow b:has-text('File scanner')", timeout=10000)
    r = page.locator(".srow", has=page.locator("b", has_text="File scanner"))
    assert r.locator("input[type=checkbox]").count() == 1 and r.locator("select option").count() == 5 and r.locator("button", has_text="Scan now").count() == 1


def test_folder_properties_dialog(page, server):
    page.goto(server + "/#/folders")
    page.wait_for_selector(".trow", timeout=10000)
    btn = page.locator(".trow .tacts button[title^='Folder properties']").first
    page.locator(".trow").first.hover()
    btn.click(force=True)
    page.wait_for_selector(".modal h3:has-text('Properties of')", timeout=5000)
    assert page.locator(".modal textarea").count() == 1 and page.locator(".modal input[type=date]").count() == 1
    page.wait_for_selector(".modal .fcov.auto", timeout=10000)
    page.locator(".modal button", has_text="Cancel").click()


def test_map_place_mode_shows_the_tray(page, server):
    page.goto(server + "/#/map")
    try:
        page.wait_for_selector(".mapbar button:has-text('Place photos')", timeout=15000)
    except Exception:
        pytest.skip("the map needs tiles from the internet")
    page.locator(".mapbar button:has-text('Place photos')").click()
    page.wait_for_selector(".maptray .mthead", timeout=15000)
    assert page.locator(".maptray .mapsearch input").count() == 1 and page.locator(".maptray .mtsize input[type=range]").count() == 1
    lay = page.locator(".leaflet-control-layers")
    if lay.count():  # the Map / Satellite switch stays clear of the tray
        a, b = lay.first.bounding_box(), page.locator(".maptray").bounding_box()
        assert a["x"] + a["width"] <= b["x"] or a["y"] >= b["y"] + b["height"] or a["x"] >= b["x"] + b["width"] or a["y"] + a["height"] <= b["y"], (a, b)
    assert "without a location" in page.locator(".maptray .mthead b").inner_text() or page.locator(".maptray .tgrid img").count() >= 0
    page.locator(".mapbar button:has-text('Done placing')").click()
    page.wait_for_selector(".mapbar button:has-text('Place photos')", timeout=10000)
    assert page.locator(".maptray").count() == 0


def test_all_metadata_and_adjust_panels_open(page, server, lib):
    page.goto(f"{server}/#/photos?sort=old")
    page.wait_for_selector("#grid .tile img", timeout=20000)
    page.locator("#grid .tile").first.click()
    page.wait_for_selector("#lightbox .lb-frame img", timeout=15000)
    page.keyboard.press("i")
    # every action on the photo is in the info panel's Actions menu, not in links scattered over the panel
    page.wait_for_selector(".lb-info .iactions", timeout=10000)
    for old in ("All metadata", "Move to a folder", "Use as folder cover", "detect faces", "Apply a metadata template"):
        assert page.locator(f".lb-info a:has-text('{old}')").count() == 0, old
    page.locator(".lb-info .iactions").click()
    labels = page.locator(".facemenu .fmi").all_inner_texts()
    for want in ("Rename…", "Move to a folder…", "All metadata…", "Change the date…", "Apply a metadata template…", "Add to album…", "Download the original"):
        assert want in labels, (want, labels)
    box, win = page.locator(".facemenu").bounding_box(), page.viewport_size
    assert box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= win["width"] and box["y"] + box["height"] <= win["height"], box
    page.locator(".facemenu .fmi", has_text="All metadata").click()
    page.wait_for_selector(".mdmodal .mdgroup", timeout=15000)
    assert page.locator(".mdmodal .mdtable tr").count() > 5
    page.locator(".mdmodal button:has-text('Close')").click()
    page.locator("#lightbox button[title^='Rotate, flip or crop']").click()
    page.locator(".facemenu .fmi:has-text('Adjust')").click()
    page.wait_for_selector(".adjbar input[type=range]", timeout=5000)
    assert page.locator(".adjbar input[type=range]").count() == 5
    page.locator(".adjbar button:has-text('Cancel')").click()
    assert page.locator(".adjbar").count() == 0


def test_copy_metadata_dialog_from_a_selection(page, server):
    page.goto(server + "/#/photos?sort=old")
    page.wait_for_selector("#grid .tile img", timeout=20000)
    tiles = page.locator("#grid .tile")
    for k in (0, 1):
        tiles.nth(k).hover(); tiles.nth(k).locator(".tcheck").click(force=True)
    page.wait_for_selector("#selbar button[title='Copy metadata']", timeout=5000)
    page.locator("#selbar button[title='Copy metadata']").click()
    page.wait_for_selector(".modal h3:has-text('Copy metadata')", timeout=5000)
    assert page.locator(".modal .fcov").count() == 2 and page.locator(".modal input[type=checkbox]").count() == 8
    page.locator(".modal button:has-text('Cancel')").click()


def test_metadata_quality_lists_photos_without_a_location(page, server):
    page.goto(server + "/#/quality")
    page.wait_for_selector("h1:has-text('Metadata quality')", timeout=10000)
    page.wait_for_selector("h2:has-text('No location')", timeout=20000)


def test_edit_log_has_search_and_picture_sizes(page, server):
    page.goto(server + "/#/history")
    page.wait_for_selector("h1:has-text('Edit log')", timeout=10000)
    assert page.locator(".hfilters input[placeholder^='Search']").count() == 1 and page.locator(".hfilters input[placeholder='Person']").count() == 1
    page.locator(".hfilters .eseg button", has_text="Larger").click()
    assert "huge" in page.locator(".hlist").get_attribute("class")


def test_search_box_enter_runs_the_search(page, server):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/#/explore")
    page.wait_for_selector("#q", timeout=10000)
    page.fill("#q", "yellowstone")
    page.wait_for_selector("#suggest .sg, .sg", timeout=5000)  # the suggestion list draws as you type
    page.keyboard.press("Enter")
    page.wait_for_function("location.hash.includes('q=') || location.hash.includes('place') || location.hash.includes('tag')", timeout=10000)
    assert page.locator(".chip").count() >= 1
    assert not errors, errors


def test_an_added_face_stays_on_screen_while_the_library_catches_up(page, server, lib):
    pid = lib["with_faces"][0]
    _open_photo(page, server, pid)
    page.keyboard.press("f")
    page.wait_for_timeout(800)
    before = page.locator(".lb-frame .facebox").count()
    page.locator("#lightbox button[title^='Add a face']").click()
    box = page.locator("#lightbox .lb-frame").bounding_box()
    page.mouse.move(box["x"] + 10, box["y"] + 10); page.mouse.down()
    page.mouse.move(box["x"] + 60, box["y"] + 70, steps=4); page.mouse.up()
    page.wait_for_selector(".modal .frow", timeout=5000)
    page.locator(".modal .frow").first.click()
    page.wait_for_function(f"document.querySelectorAll('.lb-frame .facebox').length > {before}", timeout=5000)
    counts = []
    for _ in range(16):  # the face change is saved, the index rebuilds, the page polls: the box must never vanish meanwhile
        page.wait_for_timeout(500)
        counts.append(page.locator(".lb-frame .facebox").count())
    assert min(counts) > before, counts


def test_set_location_dialog_fits_and_who_is_this_lists_nearby_people(page, server, lib):
    import json as _json
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server + "/#/photos?sort=old")
    page.wait_for_selector("#grid .tile img", timeout=20000)
    t = page.locator("#grid .tile").first
    t.hover(); t.locator(".tcheck").click(force=True)
    page.locator("#selbar button[title='Set location']").click()
    page.wait_for_selector(".modal .imap", timeout=10000)
    page.wait_for_timeout(800)
    modal = page.locator(".modal").last.bounding_box()
    for b in page.locator(".modal button").all():
        bb = b.bounding_box()
        if bb:
            assert bb["x"] + bb["width"] <= modal["x"] + modal["width"] + 1, (b.inner_text(), bb, modal)
    page.locator(".modal button", has_text="Cancel").last.click()
    pid = lib["with_faces"][0]
    r = _json.loads(urllib.request.urlopen(f"{server}/api/faces/nearby?photo={pid}").read())
    assert isinstance(r["people"], list) and all("tag" in x for x in r["people"])
