"""The viewer (lightbox): the picture, its toolbar and film strip, the face boxes and their menus, the info panel and the
edits made from it."""
import sqlite3
import time

import pytest

from helpers import need_tool

need_tool("playwright", module_level=True)
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402

from ui.conftest import _eventually, _get, _open_photo, _post, _set_read_only, _viewer_ready, _wait_flags  # noqa: E402


def test_face_boxes_follow_the_toggle(page, server, lib):
    pid = lib["with_faces"][0]
    _open_photo(page, server, pid)
    assert page.locator(".facebox:not(.objbox)").count() == 0, "boxes shown before the toggle"
    btn = page.locator('[title="Show faces (F)"]')
    btn.click()
    page.wait_for_selector(".facebox:not(.objbox)", timeout=5000)
    assert page.locator(".facebox:not(.objbox)").count() > 0
    assert "on" in (btn.get_attribute("class") or "").split()
    page.keyboard.press("f")
    page.wait_for_selector(".facebox:not(.objbox)", state="detached", timeout=5000)
    assert page.locator(".facebox:not(.objbox)").count() == 0
    assert "on" not in (btn.get_attribute("class") or "").split()

def test_ignored_faces_are_not_drawn_or_listed(page, server, lib):
    for pid in lib["with_faces"]:
        d = _get(server, f"/api/photo/{pid}")
        if any(f["kind"] == "ignored" for f in d.get("faces_other", [])):
            break
    else:
        pytest.skip("no ignored face in the scratch library")
    _open_photo(page, server, pid)
    page.keyboard.press("f")
    page.wait_for_selector(".facebox:not(.objbox)", timeout=5000)  # the photo's confirmed faces are drawn
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
    page.wait_for_selector(".facemenu", state="detached", timeout=5000)
    assert page.locator(".facemenu").count() == 0, f"menu item {label!r} did not react to the click"

def test_read_only_hides_every_editing_control(page, server, lib):
    _set_read_only(server, True)
    try:
        pid = lib["with_faces"][0]
        _open_photo(page, server, pid)
        _wait_flags(page)  # read-only arrived with the status poll
        page.reload()
        _viewer_ready(page)
        _wait_flags(page)
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

def test_keyboard_navigation(page, server, lib):
    ids = [i for i in lib["ids"] if i not in lib["videos"]]
    _open_photo(page, server, ids[0])
    page.keyboard.press("ArrowRight")
    page.wait_for_function(f"!location.hash.endsWith('p={ids[0]}')", timeout=5000)
    assert page.url.endswith(f"p={ids[1]}") or f"p={ids[1]}" in page.url or page.url != f"{server}/#/photos?p={ids[0]}"
    page.keyboard.press("Escape")
    page.wait_for_selector("#lightbox", state="hidden", timeout=5000)
    assert page.locator("#lightbox").is_hidden() or page.locator("#lightbox").count() == 0

def test_pasting_a_coordinate_pair_fills_both_fields(page, server, lib):
    pid = next(i for i in lib["ids"] if i not in lib["videos"])
    _open_photo(page, server, pid)
    if page.locator(".lb-info").is_hidden():
        page.locator('[title^="Info"]').click()
    page.wait_for_selector(".lb-info h4", timeout=10000)
    # open the location dialog from the info pane ("Set location" link or the Location heading's "change")
    link = page.locator(".lb-info .lnk", has_text="Set location")
    if not link.count():
        link = page.locator(".lb-info .lnk", has_text="change").last
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

def test_lightbox_film_strip_and_remembered_info_panel(page, server, lib):
    """The film strip along the bottom of the lightbox shows the neighbours, marks the current
    photo, jumps on click, and slides up when the mouse reaches the bottom edge. The info panel
    reopens the way it was last left."""
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.locator(".tile").first.click()
    _viewer_ready(page)
    page.wait_for_selector("#lightbox .lb-strip .reel img.cur", timeout=5000)
    reel = page.locator("#lightbox .lb-strip .reel img")
    assert reel.count() >= 2 and page.locator("#lightbox .lb-strip .reel img.cur").get_attribute("data-i") == "0"
    strip = page.locator("#lightbox .lb-strip")
    assert "show" not in (strip.get_attribute("class") or ""), "parked until the pointer comes near the bottom edge"
    box = page.locator("#lightbox .lb-stage").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    # low in the photo (where a video's controls or a low face are) it stays parked
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 60)
    page.wait_for_timeout(700)  # a fixed wait on purpose: the strip must NOT open, and nothing marks a strip that stays parked
    assert "show" not in (strip.get_attribute("class") or ""), "only the bottom edge opens it"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 20)
    page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.wait_for_selector("#lightbox .lb-strip:not(.show)", timeout=3000)
    # with the faces shown the edge leaves it parked (faces low in the photo stay reachable); the grip opens it
    page.keyboard.press("f")
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 6)
    page.wait_for_timeout(700)  # a fixed wait on purpose: the strip must NOT open
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
    want = reel.nth(1).get_attribute("src").split("/")[-1].split("?")[0]  # the strip asks for a small copy (?h=120)
    # (the thumbnail that slid up under the resting pointer may be previewed first: the hovered one replaces it)
    page.wait_for_function(f"document.querySelector('#lightbox .lb-peek')?.dataset.id === '{want}'", timeout=3000)
    assert page.locator("#lightbox .lb-strip .reel img.cur").get_attribute("data-i") == "0", "peeking does not move"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.wait_for_selector("#lightbox .lb-peek", state="detached", timeout=3000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 10)
    page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)
    page.wait_for_timeout(300)  # the strip's slide-up transition: the forced click below skips Playwright's own wait for a still element
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
    _viewer_ready(page)
    assert page.locator("#lightbox .lb-info").is_visible(), "reopens with the info panel as it was left"
    page.keyboard.press("i")
    page.keyboard.press("Escape")
    page.wait_for_selector("#lightbox", state="hidden", timeout=5000)
    page.locator(".tile").first.click()
    _viewer_ready(page)
    assert page.locator("#lightbox .lb-info").is_hidden()
    page.keyboard.press("Escape")

def test_lightbox_bar_has_close_and_share_menu(page, server, lib):
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    page.locator(".tile").first.click()
    _viewer_ready(page)
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
    _viewer_ready(page)
    page.wait_for_function("(() => { const e = document.querySelector('#lightbox .lb-frame img, #lightbox .lb-frame video'); return !!e && (e.tagName === 'VIDEO' || e.complete); })()", timeout=10000)  # the picture is on screen; the copy for sharing is fetched on the click itself
    page.click("#lightbox .lb-top button[title='Share']")
    page.wait_for_function("window.__shared.length === 1", timeout=5000)
    shared = page.evaluate("window.__shared[0]")
    assert page.locator("#lightbox .sharemenu").count() == 0, "the sheet, not the menu"
    if page.locator("#lightbox .lb-frame video").count():
        assert shared["url"]
    else:
        assert shared["files"] and shared["files"][0][0].endswith(".jpg") and shared["files"][0][1] == "image/jpeg" and shared["files"][0][2] > 1000, shared
    page.keyboard.press("Escape")

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
    page.locator(".lb-info h4", has_text="Camera").wait_for(state="detached", timeout=5000)
    assert page.locator(".lb-info h4", has_text="Camera").count() == 0
    assert page.locator(".lb-info h4", has_text="AI analysis").count() == 1, "the other sections stay"
    # remembered: reload, reopen
    page.reload()
    _viewer_ready(page)
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
    page.wait_for_function("!(localStorage.getItem('iopix.info_hide') || '').split(',').includes('camera')", timeout=5000)  # the choice is stored (in this browser: nobody is signed in)
    _open_photo(page, server, pid)
    if page.locator(".lb-info").is_hidden():
        page.locator('[title^="Info"]').click()
    page.wait_for_selector(".lb-info h4", timeout=10000)
    assert page.locator(".lb-info h4", has_text="Camera").count() == 1

def test_film_strip_leaves_video_controls_alone(page, server):
    """With a video open, the bottom edge does not slide the film strip over the player's controls; the grip still opens it."""
    r = _get(server, "/api/photos")
    vids = [r["id"][int(k)] for k in r["video"]]
    if not vids:
        pytest.skip("no video in the scratch library")
    _open_photo(page, server, vids[0])
    page.wait_for_selector("#lightbox .lb-frame video", timeout=15000)
    box = page.locator("#lightbox .lb-stage").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 6)
    page.wait_for_timeout(700)  # a fixed wait on purpose: the strip must NOT open over the player's controls
    assert "show" not in (page.locator("#lightbox .lb-strip").get_attribute("class") or "")
    if page.locator("#lightbox .lb-strip").is_visible():
        page.locator("#lightbox .lb-strip .grip").click()
        page.wait_for_selector("#lightbox .lb-strip.show", timeout=3000)

def test_video_date_change_shows_in_the_viewer(page, server):
    """Setting a video's date from the info panel changes the date in the panel and over the picture at once."""
    r = _get(server, "/api/photos")
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
    taken = lambda: _get(server, f"/api/photo/{vid}")["taken"]  # noqa: E731
    assert _eventually(lambda: taken().startswith("2009-09-09T17:00")), taken()  # the server's index takes the edit on its next rebuild
    _post(server, "/api/edit/revert", {"ids": [vid]})

def test_person_without_a_face_box_has_a_menu_to_take_them_off(page, server, lib):
    """A person tagged on a photo with no face box is listed under People with a menu: take them off the photo."""
    import json as _json
    call = lambda path, body=None: _get(server, path) if body is None else _post(server, path, body)  # noqa: E731
    dk = sqlite3.connect(lib["db"])
    person, name = dk.execute("SELECT p.tagid, t.name FROM TagProperties p JOIN Tags t ON t.id = p.tagid WHERE p.property = 'person' AND p.value != '' LIMIT 1").fetchone()
    img = dk.execute("SELECT id FROM Images WHERE status = 1 AND category = 1 AND id NOT IN (SELECT imageid FROM ImageTagProperties WHERE tagid = ?) AND id NOT IN (SELECT imageid FROM ImageTags WHERE tagid = ?) LIMIT 1", (person, person)).fetchone()[0]
    dk.close()
    call("/api/faces/op", {"op": "tagonly", "image": img, "tag": person})
    _eventually(lambda: any(t["id"] == person for t in call(f"/api/photo/{img}")["tags"]))  # the index has the tag
    _open_photo(page, server, img)
    page.keyboard.press("i")
    row = page.locator(".lb-info .facerow", has_text=name)
    row.wait_for(timeout=10000)
    assert "no face box" in row.inner_text()
    row.click()
    page.locator(".facemenu .fmi", has_text="take").click()
    page.wait_for_function(f"![...document.querySelectorAll('.lb-info .facerow')].some((r) => r.innerText.includes({_json.dumps(name)}))", timeout=5000)
    _eventually(lambda: not any(t["id"] == person for t in call(f"/api/photo/{img}")["tags"]))
    assert not any(t["id"] == person for t in call(f"/api/photo/{img}")["tags"]), "taken off in digiKam's database too"

def test_only_the_town_is_known_saves_an_approximate_location(page, server, lib):
    """Set location › Only the town is known: no altitude, a radius, saved as the position's accuracy; the info panel
    then says the location is approximate."""
    page.add_init_script("localStorage.setItem('iopix.units', 'km')")  # the sizes below are in kilometres
    pid = lib["ids"][2]
    _open_photo(page, server, pid)
    page.keyboard.press("i")
    page.wait_for_selector(".lb-info .iactions", timeout=10000)
    page.locator(".lb-info .iactions").click()
    page.locator(".facemenu .fmi", has_text="location").first.click()
    page.wait_for_selector(".modal h3:has-text('Set location')", timeout=5000)
    town = page.locator(".modal label", has_text="Only the town is known").locator("input[type=checkbox]")
    town.check()
    assert page.locator(".modal input[placeholder='altitude m']").is_disabled()
    page.locator(".modal input[placeholder='latitude']").fill("40.3834")
    page.locator(".modal input[placeholder='longitude']").fill("-80.0459")
    page.locator(".modal select[title^='How far']").select_option("5000")
    page.locator(".modal button", has_text="Save location").click()
    page.wait_for_selector(".lb-info .approx", timeout=10000)
    assert "5 km" in page.locator(".lb-info .approx").inner_text()
    def saved():
        d = _get(server, f"/api/photo/{pid}")
        return d if d.get("extra", {}).get("gpsAccuracy") == 5000 else None
    d = _eventually(saved) or _get(server, f"/api/photo/{pid}")
    assert d["extra"]["gpsAccuracy"] == 5000 and d["alt"] is None
    _post(server, "/api/edit/revert", {"ids": [pid]})

def test_info_panel_change_log_is_last_and_folded(page, server):
    """A photo changed here shows its change log at the very end of the info panel, folded until opened."""
    pid = _get(server, "/api/photos")["id"][3]
    _post(server, "/api/edit", {"ids": [pid], "changes": {"rating": 2}})  # one change, recorded in the server's change log
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
    _post(server, "/api/edit/revert", {"ids": [pid]})  # put the scratch library back

def test_face_change_shows_before_the_server_answers(page, server):
    """Ignoring a face in the viewer changes the overlay at once, even when the server takes seconds."""
    faces = _get(server, "/api/faces/queue?kind=unknown&limit=5")["faces"]
    if not faces:
        faces = _get(server, "/api/faces/queue?kind=suggested&limit=5")["faces"]
    if not faces:
        pytest.skip("no undecided face in the fixture")
    f = faces[0]
    seen = {"posted": False}

    def slow(route):
        seen["posted"] = True
        time.sleep(2.5)  # on purpose: a server that takes seconds, so the test can see the page change before the answer
        route.continue_()
    page.route("**/api/faces/op", slow)
    _open_photo(page, server, f["photo"])
    page.keyboard.press("f")
    page.wait_for_selector(".facebox.face-unknown, .facebox.face-suggested", timeout=10000)
    before = page.locator(".facebox.face-unknown, .facebox.face-suggested").count()
    page.locator(".facebox.face-unknown, .facebox.face-suggested").first.click()
    with page.expect_response(lambda r: r.url.endswith("/api/faces/op"), timeout=15000) as answered:  # the slowed-down save
        page.locator(".facemenu .fmi", has_text="ignore").first.click()
        page.wait_for_function("(n) => document.querySelectorAll('.facebox.face-unknown, .facebox.face-suggested').length === n",
                               arg=before - 1, timeout=2000)  # gone at once: well before the server answers
    assert seen["posted"] and answered.value.ok
    page.unroute("**/api/faces/op")
    # put the scratch library back: every ignored face of that photo becomes unknown again
    for o in _get(server, f"/api/photo/{f['photo']}").get("faces_other", []):
        if o["kind"] == "ignored":
            _post(server, "/api/faces/op", {"op": "unknown", "image": f["photo"], "rect": [o["x"], o["y"], o["w"], o["h"]]})

def test_removing_a_face_needs_no_question_and_can_be_undone(page, server):
    faces = _get(server, "/api/faces/queue?kind=unknown&limit=5")["faces"]
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
    with page.expect_response(lambda r: r.url.endswith("/api/faces/op"), timeout=10000):  # the removal reaches the server
        page.locator(".facemenu .fmi", has_text="Remove this region").click()
        page.wait_for_function("(n) => document.querySelectorAll('.facebox.face-unknown').length === n", arg=n - 1, timeout=3000)
    assert not dialogs, dialogs
    assert page.locator(".facebox.face-unknown").count() == n - 1
    with page.expect_response(lambda r: r.url.endswith("/api/faces/op"), timeout=10000):  # and so does the undo, before the page goes
        page.locator("#toast .lnk", has_text="Undo").click()
        page.wait_for_function("(n) => document.querySelectorAll('.facebox.face-unknown').length === n", arg=n, timeout=3000)
    assert page.locator(".facebox.face-unknown").count() == n, "Undo puts it back"
    rect = (f["x"], f["y"], f["w"], f["h"])
    assert _eventually(lambda: any((o["x"], o["y"], o["w"], o["h"]) == rect for o in _get(server, f"/api/photo/{f['photo']}").get("faces_other", []))), "and on the server too"

def test_person_picker_shows_only_the_name(page, server, lib):
    faces = _get(server, "/api/faces/queue?kind=unknown&limit=5")["faces"]
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

def test_person_options_menu_opens_in_front_of_the_map(page, server, lib):
    """The info pane's person Options menu sits above the map (Leaflet's panes have high z-indexes)."""
    ids = _get(server, "/api/photos?geo=1")["id"]
    with_face = set(lib["with_faces"])
    pid = next((i for i in ids if i in with_face), None)
    if pid is None:
        pytest.skip("no geotagged photo with a named face in the fixture")
    page.set_viewport_size({"width": 1280, "height": 800})
    _open_photo(page, server, pid)
    page.keyboard.press("i")
    page.wait_for_function("!document.querySelector('.lb-info .imap') || document.querySelector('.lb-info .imap.leaflet-container')", timeout=10000)  # Leaflet (loaded on demand) has built the map on the element, or gave it up
    opt = page.locator(".lb-info", has_text="options").locator("text=options").first
    opt.click()
    menu = page.locator(".facemenu")
    menu.wait_for(timeout=5000)
    # a toast, or the update banner an earlier test's version check switched on, would sit where the probe lands
    page.evaluate("document.querySelector('#toast').hidden = true; document.querySelector('#updbanner')?.remove()")
    b = menu.bounding_box()
    hit = page.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); if (e && e.closest('.facemenu')) return 'menu'; if (!e) return 'none'; const cs = getComputedStyle(e); const chain = []; for (let p = e; p && p !== document.body; p = p.parentElement) chain.push(`${p.tagName}#${p.id}.${p.className}`); return `${e.outerHTML.slice(0, 160)} | ${cs.position}/${cs.zIndex} | ${chain.join(' < ')}`; }", [b["x"] + b["width"] / 2, b["y"] + b["height"] - 12])
    assert hit == "menu", (hit, b, page.viewport_size)

def test_rotate_key_turns_the_photo_and_undo_from_the_toast(page, server, lib):
    from helpers import _face_jpeg, _info
    from app.photos import lossless
    need_tool("jpegtran", lossless.jpegtran())
    pid, path, (tag, rect), (w, h, o) = _face_jpeg(lib)
    original = path.read_bytes()
    _open_photo(page, server, pid)
    page.wait_for_selector("#lightbox button[title^='Rotate, flip or crop']", timeout=10000)
    page.keyboard.press("]")
    page.wait_for_selector("#toast:has-text('Rotate right.')", timeout=30000)
    W, H = lossless.upright_size(w, h, o)
    assert _info(lib["db"], pid)[0] == (H, W, 1)
    page.click("#toast .lnk:has-text('Undo')")
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

def test_all_metadata_and_adjust_panels_open(page, server, lib):
    page.goto(f"{server}/#/photos?sort=old")
    page.wait_for_selector("#grid .tile img", timeout=20000)
    page.locator("#grid .tile").first.click()
    _viewer_ready(page)
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
    page.mouse.move(400, 300); page.mouse.move(420, 40)  # the toolbar hides 2.8 s after the last mouse move (the dialog may take longer)
    page.locator("#lightbox button[title^='Rotate, flip or crop']").click()
    page.locator(".facemenu .fmi:has-text('Adjust')").click()
    page.wait_for_selector(".adjbar input[type=range]", timeout=5000)
    assert page.locator(".adjbar input[type=range]").count() == 5
    page.locator(".adjbar button:has-text('Cancel')").click()
    assert page.locator(".adjbar").count() == 0

def test_an_added_face_stays_on_screen_while_the_library_catches_up(page, server, lib):
    pid = lib["with_faces"][0]
    _open_photo(page, server, pid)
    page.keyboard.press("f")
    page.wait_for_selector(".lb-frame .facebox", timeout=5000)
    before = page.locator(".lb-frame .facebox").count()
    page.locator("#lightbox button[title^='Add a face']").click()
    box = page.locator("#lightbox .lb-frame").bounding_box()
    page.mouse.move(box["x"] + 10, box["y"] + 10); page.mouse.down()
    page.mouse.move(box["x"] + 60, box["y"] + 70, steps=4); page.mouse.up()
    page.wait_for_selector(".modal .frow", timeout=5000)
    # the face change is saved, the index rebuilds, the page notices (its status polls) and takes the server's version of the
    # photo: the box must never vanish meanwhile (a MutationObserver keeps the lowest number of boxes seen on the way)
    try:
        with page.expect_response(lambda r: r.url.endswith(f"/api/photo/{pid}"), timeout=20000):
            page.locator(".modal .frow").first.click()
            page.wait_for_function(f"document.querySelectorAll('.lb-frame .facebox').length > {before}", timeout=5000)
            page.evaluate("""() => { window.__minBoxes = Infinity;
                new MutationObserver(() => { window.__minBoxes = Math.min(window.__minBoxes, document.querySelectorAll('.lb-frame .facebox').length); })
                  .observe(document.querySelector('#lightbox .lb-frame'), { childList: true, subtree: true }); }""")
    except PWTimeout:
        pass  # the page saw no rebuilt index to take the photo from: nothing could have replaced the box
    page.wait_for_timeout(300)  # a fixed wait on purpose: the page redraws the boxes from the answer just seen, and no element marks that redraw
    assert page.locator(".lb-frame .facebox").count() > before and page.evaluate("window.__minBoxes") > before

def test_set_location_dialog_fits_and_who_is_this_lists_nearby_people(page, server, lib):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server + "/#/photos?sort=old")
    page.wait_for_selector("#grid .tile img", timeout=20000)
    t = page.locator("#grid .tile").first
    t.hover(); t.locator(".tcheck").click(force=True)
    page.locator("#selbar button[title='Set location']").click()
    page.wait_for_function("!document.querySelector('.modal .imap') || document.querySelector('.modal .imap.leaflet-container')", timeout=10000)  # Leaflet (loaded on demand) has built the map on the element, or gave it up
    modal = page.locator(".modal").last.bounding_box()
    for b in page.locator(".modal button").all():
        bb = b.bounding_box()
        if bb:
            assert bb["x"] + bb["width"] <= modal["x"] + modal["width"] + 1, (b.inner_text(), bb, modal)
    page.locator(".modal button", has_text="Cancel").last.click()
    pid = lib["with_faces"][0]
    r = _get(server, f"/api/faces/nearby?photo={pid}")
    assert isinstance(r["people"], list) and all("tag" in x for x in r["people"])

def test_remove_all_unnamed_face_regions_from_the_actions_menu(page, server):
    """Actions › Remove all unnamed face regions: every unknown face box goes in one go (one batch when there are
    several), and Undo puts them all back."""
    from collections import Counter
    faces = _get(server, "/api/faces/queue?kind=unknown&limit=200")["faces"]
    if not faces:
        pytest.skip("no unknown face in the fixture")
    pid = Counter(f["photo"] for f in faces).most_common(1)[0][0]
    rects = {(f["x"], f["y"], f["w"], f["h"]) for f in faces if f["photo"] == pid}
    _open_photo(page, server, pid)
    page.keyboard.press("f"); page.wait_for_selector(".facebox.face-unknown", timeout=10000)
    n = page.locator(".facebox.face-unknown").count()
    page.keyboard.press("i"); page.wait_for_selector(".lb-info .iactions", timeout=10000)
    page.locator(".lb-info .iactions").click()
    item = page.locator(".facemenu .fmi", has_text="Remove all unnamed face regions")
    assert f"({n})" in item.inner_text()
    endpoint = "/api/faces/batch" if n > 1 else "/api/faces/op"
    with page.expect_response(lambda r: r.url.endswith(endpoint), timeout=10000):
        item.click()
        page.wait_for_function("() => document.querySelectorAll('.facebox.face-unknown').length === 0", timeout=3000)
    assert page.locator(".facebox.face-unknown").count() == 0
    unknown_on_server = lambda: {(o["x"], o["y"], o["w"], o["h"]) for o in _get(server, f"/api/photo/{pid}").get("faces_other", []) if o["kind"] == "unknown"}  # noqa: E731
    assert _eventually(lambda: not unknown_on_server()), "gone on the server"
    with page.expect_response(lambda r: r.url.endswith(endpoint), timeout=10000):
        page.locator("#toast .lnk", has_text="Undo").click()
        page.wait_for_function("(n) => document.querySelectorAll('.facebox.face-unknown').length === n", arg=n, timeout=3000)
    assert _eventually(lambda: unknown_on_server() == rects), "all put back on the server"

def test_closing_the_viewer_after_an_edit_keeps_the_grid_where_it_was(browser, server):
    """Photos page: open a photo far down the grid, change the library while the viewer is open (as a face edit does),
    close the viewer with its X, and with Back: the grid is drawn again with the same photo at the same height,
    not from the top."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 560}, bypass_csp=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    ids = _get(server, "/api/photos")["id"]

    edit = lambda pid, changes: _post(server, "/api/edit", {"ids": [pid], "changes": changes})  # noqa: E731
    revert = lambda pid: _post(server, "/api/edit/revert", {"ids": [pid]})  # noqa: E731

    try:
        page.goto(server + "/#/photos")
        page.wait_for_selector(".tile img", timeout=15000)
        room = page.locator("#main").evaluate("m => m.scrollHeight - m.clientHeight")
        assert room > 400, f"the scratch grid must scroll for this test ({room}px)"
        touched = []
        for close in ("x", "back"):
            page.locator("#main").evaluate(f"m => m.scrollTop = {int(room * 0.6)}")
            page.wait_for_function("(t) => { const m = document.querySelector('#main'); return Math.abs(m.scrollTop - t) < 2 "
                                   "&& [...document.querySelectorAll('.tile')].some((e) => { const r = e.getBoundingClientRect(); return r.top >= m.getBoundingClientRect().top && r.bottom <= innerHeight; }); }",
                                   arg=int(room * 0.6), timeout=5000)  # scrolled, and the grid has drawn tiles for that place
            top = page.locator("#main").evaluate("m => m.scrollTop")
            tile = page.locator(".tile").nth(page.locator(".tile").count() // 2)  # one in the middle of the screen
            idx = int(tile.get_attribute("data-i"))
            pid = ids[idx]
            y0 = tile.evaluate("t => t.getBoundingClientRect().top")
            tile.click()
            _viewer_ready(page)
            meta = page.evaluate("import('/js/state.js').then((m) => m.S.META.version)")
            edit(pid, {"rating": 3 if close == "x" else 4}); touched.append(pid)  # the library changes under the open viewer
            assert _eventually(lambda: page.evaluate("import('/js/main.js').then((m) => m.pollStatus()).then(() => import('/js/state.js')).then((m) => m.S.META.version)") != meta, 15), "the page saw the change"
            if close == "x":
                page.mouse.move(420, 40)
                page.click("#lightbox .lb-top .iconbtn >> nth=0")
            else:
                page.go_back()
            page.wait_for_selector("#lightbox", state="hidden", timeout=5000)
            page.wait_for_selector(".tile img", timeout=10000)
            page.wait_for_function("(i) => document.querySelector('#main').scrollTop > 100 && !!document.querySelector(`.tile[data-i=\"${i}\"]`)", arg=idx, timeout=5000)  # the grid is drawn again around the photo
            now = page.locator("#main").evaluate("m => m.scrollTop")
            assert now > 100, f"closed with {close}: the grid went back to the top ({top} -> {now})"
            y1 = page.locator(f'.tile[data-i="{idx}"]').evaluate("t => t.getBoundingClientRect().top")
            assert abs(y1 - y0) <= 2, f"closed with {close}: the photo moved from {y0} to {y1} (scroll {top} -> {now})"
    finally:
        for pid in set(touched):
            revert(pid)
        ctx.close()
    assert not errors, "uncaught JS errors: " + "; ".join(errors)[:500]

def test_an_approximate_date_shows_only_what_is_known(page, server, lib):
    """Year only / unknown (dateprecision.py): the info panel and the grid's section heading say "1992" or
    "Date unknown", never the placeholder day, as soon as the edit is made; the date editor offers the choice."""
    pid = _get(server, "/api/photos")["id"][-1]
    try:
        _open_photo(page, server, pid)
        if not page.locator("#lightbox .lb-info .irow").count():
            page.keyboard.press("i")
        page.evaluate(f"import('/js/editing.js').then((m) => m.editPhotos([{pid}], {{ date_precision: 'year', taken: '1992' }}))")
        page.wait_for_function("document.querySelector('#lightbox .lb-info')?.textContent.includes('only the year is known')", timeout=10000)
        info = page.locator("#lightbox .lb-info").inner_text()
        assert "1992" in info and "January 1" not in info and "Jan 1" not in info, info
        assert page.locator("#lightbox .dateedit select").first.input_value() == "year"
        page.evaluate(f"import('/js/editing.js').then((m) => m.editPhotos([{pid}], {{ date_precision: 'unknown' }}))")
        page.wait_for_function("document.querySelector('#lightbox .lb-info')?.textContent.includes('Date unknown')", timeout=10000)
        page.keyboard.press("Escape")
        page.wait_for_selector(".ghead", timeout=10000)
        assert _eventually(lambda: page.evaluate("[...document.querySelectorAll('.ghead')].some((h) => h.textContent.includes('Date unknown'))"), 15), \
            "the grid heads the photo's section with what is known"
    finally:
        _post(server, "/api/edit/revert", {"ids": [pid]})
