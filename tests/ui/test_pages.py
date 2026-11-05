"""The pages: the photo grid, Explore, People, Folders and Albums, the quality and suggestion pages, the map, the import page,
Settings and Maintenance."""
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from helpers import need_tool

need_tool("playwright", module_level=True)
from playwright.sync_api import TimeoutError as PWTimeout, expect  # noqa: E402

from ui.conftest import _eventually, _get, _open_photo, _post, _viewer_ready, _wait_flags, ui_db  # noqa: E402


def test_grid_renders_thumbnails(page, server, lib):
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    assert page.locator(".tile").count() > 10

def test_settings_and_maintenance_pages(page, server):
    page.goto(server + "/#/settings")
    page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    page.wait_for_selector(".stabs", timeout=10000)  # the sections arrive with /api/maintenance
    assert page.locator(".stabs button").all_inner_texts()[:4] == ["Display", "digiKam and server", "Editing", "AI and search"]
    heads = page.locator("h2").evaluate_all("els => els.map((e) => e.textContent)")
    for h in ("Display", "Search and background work", "Editing", "Place tags", "Metadata templates"):
        assert h in heads, heads
    page.locator(".stabs button", has_text="Editing").click()
    assert page.locator("h2:has-text('Metadata templates')").is_visible() and page.locator("h2:has-text('Display')").is_hidden()
    assert "tab=editing" in page.url
    page.reload(); page.wait_for_selector(".stabs button.on:has-text('Editing')", timeout=10000)
    page.locator(".stabs button", has_text="Display").click()
    assert "Storage" not in heads and "Library" not in heads, "maintenance sections must not be on the Settings page"
    assert page.locator(".ajob select").count() >= 1, "the core budget selector lives in Settings"
    page.goto(server + "/#/maintenance")
    page.wait_for_selector("h1:has-text('Maintenance')", timeout=10000)  # a hash-only navigation re-renders in place
    page.wait_for_selector(".stabs", timeout=10000)
    assert page.locator(".stabs button").all_inner_texts() == ["Library", "AI analysis and models", "Storage and backups", "ioPix"], "the app's name, not the retired Viewer"
    heads = page.locator("h2").evaluate_all("els => els.map((e) => e.textContent)")
    for h in ("Library", "AI analysis and models", "Storage and backups", "ioPix"):
        assert any(x.startswith(h) for x in heads), (h, heads)
    assert "Display" not in heads and "Editing" not in heads
    page.locator(".stabs button", has_text="AI analysis and models").click()
    page.wait_for_selector(".mtable tbody tr", timeout=10000)
    assert page.locator("b", has_text="Background work").count() == 1
    rows = page.locator(".mtable tbody tr")
    assert rows.count() >= 7, "one row per feature"
    assert page.locator(".mtable tr[data-job=objects][data-model=yolox]").count() == 1, "a job and its model share a row"
    assert page.locator(".mtable tr[data-job=objects] select.mpick option").count() == 4, "the four YOLOX sizes are offered in a menu"
    assert page.locator(".mtable tr[data-job=facedetect] select.mpick option").count() == 3, "YuNet, SCRFD and RetinaFace"
    assert page.locator(".mtable tr[data-model=geonames]").count() == 1, "models without a job have a row too"
    assert page.locator(".mtable tr[data-job=ages][data-model=mivolo]").count() == 1, "the age model sits on its job's row"
    assert page.locator(".mtable tr[data-job=looks][data-model='aesthetic eyes']").count() == 1, "both photo quality models on its row"
    for key in ("mivolo", "aesthetic", "eyes"):
        assert page.locator(f".mtable tr[data-model~={key}]").count() == 1, f"{key} is listed once"
    assert page.locator(".mtable thead th").all_inner_texts()[:3] == ["Feature", "Model", "Progress"]
    assert page.locator("b", has_text="Restart ").count() == 1  # "Restart <the app's name>"
    page.locator(".stabs button", has_text="Library").click()
    assert page.locator(".srow b", has_text="Metadata consistency").count() == 1, "consistency sits under Library"
    assert page.locator(".stable.wide tr").count() >= 10 and page.locator(".stable.wide button", has_text="Rebuild").count() == 1
    assert page.locator(".navfoot a[data-v=maintenance]").count() == 1 and page.locator(".navfoot a[data-v=settings]").count() == 1, "at the foot of the sidebar"

def test_database_row_tests_a_connection(page, server, lib):
    page.goto(server + "/#/settings?tab=server"); page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
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
    con = ui_db(); con.cursor().execute("REPLACE INTO app_settings (`key`, value) VALUES ('digikam_closed_until', '0')"); con.close()
    assert _get(server, "/api/status")["digikamCheck"] == "none"
    dialogs = []
    def on_dialog(d):
        dialogs.append(d.type + ":" + d.message[:40])
        d.accept("zz-ui-vouch") if d.type == "prompt" else d.accept()
    page.on("dialog", on_dialog)
    page.goto(server + "/#/tags"); page.wait_for_selector("h1", timeout=10000)
    page.locator("button", has_text="Manage tags").first.click(); page.wait_for_selector("button:has-text('New top-level tag')", timeout=5000)
    page.locator("button", has_text="New top-level tag").first.click()
    page.wait_for_selector(".modal input", timeout=5000)  # the app's own prompt, not the browser's
    page.locator(".modal input").fill("zz-ui-vouch")
    try:
        with page.expect_response(lambda r: r.url.endswith("/api/tags/op"), timeout=10000):  # created without a question
            page.locator(".modal-btns button.on").click()
    except PWTimeout:
        pass  # the assertions below say what stood in the way
    assert not dialogs, "no browser dialogs: " + str(dialogs)
    assert page.locator(".modal").count() == 0, "no confirmation about digiKam"
    assert _eventually(lambda: any(t["name"] == "zz-ui-vouch" for t in _get(server, "/api/meta")["tags"])), "after accepting, the tag is created"

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
    page.wait_for_function("document.querySelector('#main').scrollTop === 0", timeout=5000)
    grid_h = page.evaluate("document.getElementById('grid').style.height")
    days = page.locator(".ghead").count()
    page.select_option(".gbar .groupsel", "month")
    page.wait_for_function(f"document.getElementById('grid').style.height !== '{grid_h}'", timeout=5000)
    page.wait_for_function(f"document.querySelectorAll('.ghead').length !== {days}", timeout=5000)  # the sections are drawn again by month
    months = page.locator(".ghead").count()
    assert 0 < months < days, (days, months)
    head = page.locator(".ghead").first.inner_text()
    assert any(m in head for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")) and "," not in head, head
    assert page.evaluate("localStorage.getItem('iopix.group')") == "month"
    page.select_option(".gbar .groupsel", "day")

def test_on_this_day_is_a_justified_gallery_without_scrollbars(page, server, lib):
    with page.expect_response(lambda r: "/api/photos?md=" in r.url, timeout=15000):  # the section is filled from this answer (or left hidden)
        page.goto(server + "/#/explore")
    page.wait_for_selector(".stats .stat", timeout=10000)
    g = page.locator("h2:has-text('On this day') + .jrows")
    if g.count() == 0 or g.locator(".tile").count() == 0:
        pytest.skip("no photo in the scratch library was taken on today's date")
    assert page.evaluate("(e) => e.scrollWidth <= e.clientWidth", g.element_handle()), "nothing to scroll sideways"
    box = g.bounding_box()
    tiles = g.locator(".tile")
    for i in range(tiles.count()):
        b = tiles.nth(i).bounding_box()
        assert b["x"] >= box["x"] - 1 and b["x"] + b["width"] <= box["x"] + box["width"] + 1, "tiles wrap within the page width"

def test_set_location_for_a_selection(page, server, lib):
    """Select several photos: the selection bar offers Set location, and the dialog addresses them all."""
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    _wait_flags(page)
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
    _wait_flags(page)
    assert page.locator("#nav a[data-v=folders]").count() == 1 and page.locator("#nav a[data-v=albums]").count() == 1
    # the sidebar's groups: Library for everyone, Manage for admins only
    assert page.locator("#nav .navsec", has_text="Library").count() == 1
    assert page.locator("#nav .navmanage a[data-v=trash]").count() == 1 and page.locator("#nav a[data-v=events]").count() == 1
    assert page.locator("#nav .navmanage a[data-v=quality]").count() == 0
    assert page.locator("#nav .navmanage a[data-v=damaged]").count() == 0, "the check pages live under Maintenance › Library, not in the sidebar"
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
    _wait_flags(page)
    page.locator(".tile .tcheck").nth(0).click(force=True)
    page.locator(".tile .tcheck").nth(1).click(force=True)
    page.wait_for_selector("#selbar", timeout=5000)
    assert page.locator(".gbar .selnone").is_visible() and page.locator(".gbar .selall").is_visible(), "partly selected: both links"
    page.locator("#selbar button[title='Edit']").click()
    page.wait_for_selector(".modal h3:has-text('Edit 2 photos')", timeout=5000)
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
    _wait_flags(page)
    assert page.locator(".gbar .jumpdate").count() == 1, "the chronological view has a date box"
    page.on("dialog", lambda d: d.accept("Clips") if d.type == "prompt" else d.accept())
    page.locator(".gbar button[title^='Save this search']").click()
    page.wait_for_selector(".modal input", timeout=5000); page.locator(".modal input").fill("Clips"); page.locator(".modal-btns button.on").click()
    page.wait_for_selector("#toast:has-text('Saved')", timeout=5000)
    page.wait_for_selector(".crumbs .cur:has-text('Clips')", timeout=5000)
    assert page.locator(".crumbs .cur", has_text="Clips").count() == 1, "the saved search shows in the breadcrumbs"
    page.goto(server + "/#/albums")
    page.wait_for_selector("h2:has-text('Smart albums')", timeout=10000)
    page.wait_for_selector(".card.smart .nm:has-text('Clips')", timeout=10000)
    page.wait_for_function("document.querySelector('.card.smart .sb') && !document.querySelector('.card.smart .sb').textContent.includes('…')", timeout=10000)
    # the lightbox hotkey T opens the tag dialog for an editor
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile img", timeout=15000)
    _wait_flags(page)
    page.locator(".tile").first.click()
    _viewer_ready(page)
    page.keyboard.press("t")
    page.wait_for_selector(".modal h3:has-text('Tag')", timeout=5000)
    page.locator(".modal button", has_text="Done").click()
    page.keyboard.press("Escape")
    # clean up the smart album
    page.goto(server + "/#/albums")
    page.wait_for_selector(".card.smart", timeout=10000)
    page.locator(".card.smart").first.hover()
    page.locator(".card.smart .carddl[title^='Delete']").first.click(force=True)
    page.wait_for_selector(".modal-btns .danger", timeout=5000); page.locator(".modal-btns .danger").click()  # the app's own confirmation
    page.wait_for_selector(".card.smart", state="detached", timeout=5000)

def test_consistency_bulk_sync_bar(page, server, lib):
    """Ticking photos in the consistency report brings up the bar that syncs them all at once."""
    req = urllib.request.Request(server + "/api/consistency/scan", data=b"rescan_all=1", headers={"content-type": "application/x-www-form-urlencoded"}, method="POST")
    urllib.request.urlopen(req).read()
    _eventually(lambda: not _get(server, "/api/consistency")["progress"]["running"], timeout=20)
    page.goto(server + "/#/consistency")
    page.wait_for_selector(".crec", timeout=10000)
    _wait_flags(page)
    boxes = page.locator(".crec .chead input[type=checkbox]")
    assert boxes.count() >= 1, "editors get a checkbox per photo"
    boxes.first.check()
    page.wait_for_selector("#cbulk:not([hidden]) button[data-src=sidecar]", timeout=3000)
    assert "1 selected" in page.locator("#cbulk .n").inner_text()
    page.evaluate("import('/js/main.js').then((m) => m.pollStatus())")  # one status poll with its re-render, now rather than in 20 s
    assert page.locator("#cbulk:not([hidden])").count() == 1, "the bar stays while photos are ticked"
    vp = page.viewport_size
    for b in page.locator("#cbulk button[data-src]").all():
        bb = b.bounding_box()
        assert bb["x"] + bb["width"] <= vp["width"] and bb["y"] + bb["height"] <= vp["height"], "every button on screen"
    # Select all takes every disagreeing photo, then one click syncs them from the sidecar
    page.locator(".lnk", has_text="Select all").click()
    page.wait_for_function("document.querySelector('#cbulk .n') && !document.querySelector('#cbulk .n').textContent.startsWith('1 ')", timeout=5000) if page.locator(".crec").count() > 1 else None
    page.on("dialog", lambda d: d.accept())
    page.locator("#cbulk button[data-src=sidecar]").click()
    page.wait_for_selector(".modal-btns button.on", timeout=5000); page.locator(".modal-btns button.on").click()  # the app's own confirmation
    page.wait_for_selector("#toast:has-text('Synced')", timeout=20000)
    page.wait_for_selector("#cbulk", state="hidden", timeout=5000)

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
    page.mouse.down(); page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.95); page.mouse.up()
    page.wait_for_function("document.querySelector('#main').scrollTop > 0", timeout=5000)
    assert page.locator("#main").evaluate("m => m.scrollTop") > 0
    assert page.locator("#scrub .pos").count() == 1  # the position label is still there after the jump

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
    _wait_flags(page)  # the banner is drawn, or not, by the status poll
    assert page.locator("#updbanner").count() == 0, "dismissed for this version"

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
    page.goto(server + "/#/settings?tab=server")
    page.wait_for_selector(".srow:has-text('Sync with digiKam')", timeout=10000)
    assert page.locator(".srow", has_text="Sync with digiKam").locator("button", has_text="Turn off").count() == 1

def test_server_log_dialog_opens_from_maintenance(page, server):
    page.goto(server + "/#/maintenance?tab=viewer")
    page.wait_for_selector(".srow:has-text('Server log') button", timeout=10000)
    page.locator(".srow", has_text="Server log").locator("button").click()
    page.wait_for_selector(".modal pre.logview", timeout=5000)
    page.wait_for_function("!document.querySelector('.modal pre.logview').textContent.startsWith('Loading')", timeout=5000)
    assert page.locator(".modal a", has_text="Download the log").count() == 1

def test_grid_header_stays_while_scrolling_and_delete_asks_first(page, server):
    page.set_viewport_size({"width": 1300, "height": 800})
    page.goto(server + "/#/photos")
    page.wait_for_selector("#grid .tile", timeout=15000)
    page.locator("#main").evaluate("m => m.scrollTop = 1500")
    page.wait_for_function("document.querySelector('#main').scrollTop > 0", timeout=5000)
    bar = page.locator("#grid .gbar").bounding_box()
    main = page.locator("#main").bounding_box()
    assert abs(bar["y"] - main["y"]) < 2, "the header with Select all is still at the top of the page"
    assert page.locator("#grid .selall").is_visible()
    page.locator("#grid .selall").click()
    page.wait_for_selector("#selbar", timeout=3000)
    assert page.locator("#selbar .lnk", has_text="Deselect all").count() == 1
    _wait_flags(page)
    if page.locator("#selbar button[title='Delete']").count():
        page.locator("#selbar button[title='Delete']").click()
        page.wait_for_selector(".modal .pillbtn.danger", timeout=3000)
        page.locator(".modal button", has_text="Cancel").click()
    page.locator("#selbar .lnk", has_text="Deselect all").click()
    page.wait_for_selector("#selbar", state="detached", timeout=3000)

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
        page.wait_for_function("[...document.querySelectorAll('.pgroup')].some((s) => s.querySelector('.pghead')?.innerText.startsWith('Zz Family') && s.querySelectorAll('.card').length === 2)", timeout=5000)
        # the menu: add a third person to the group
        rest.locator(".card", has_text=people[2]["name"]).first.locator(".pmenu").click()
        page.locator(".facemenu .fmi", has_text="Zz Family").click()
        group = lambda: next(x for x in call("/api/people/groups")["groups"] if x["id"] == gid)  # noqa: E731
        _eventually(lambda: len(group()["members"]) == 3, timeout=4)
        g = group()
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

def test_explore_photo_size_and_section_lengths(page, server):
    """Explore: at most 8 photos in On this day, more in Rediscover, and an S/M/L photo size that is remembered."""
    page.goto(server + "/#/explore")
    page.wait_for_selector("h2:has-text('Rediscover') + .jrows .tile", timeout=15000)
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

def test_long_file_names_stay_inside_their_cards(page, server):
    """Metadata quality and the edit log in large view: a 60-character file name wraps inside its card."""
    page.goto(server + "/#/quality?check=nolocation")
    page.wait_for_selector(".qsec[data-check=nolocation]", timeout=20000)
    page.evaluate("""() => { const list = document.querySelector('.hlist.qitems') || document.querySelector('.hlist');
        list.className = 'hlist qitems large';
        const row = document.createElement('div'); row.className = 'hrow';
        row.innerHTML = '<span class="ph"></span><div class="hmain"><b>1361232_10201731163668655_357083_n_1875465574185_10201731163668655.mp4</b> · taken</div>';
        list.prepend(row); }""")
    row = page.locator(".hlist.qitems.large .hrow").first
    box, name = row.bounding_box(), row.locator("b").bounding_box()
    assert name["x"] + name["width"] <= box["x"] + box["width"] + 1, (name, box)

def test_face_quality_page_and_adjusting_a_box(page, server):
    page.goto(server + "/#/quality")
    page.wait_for_selector("h1:has-text('Metadata quality')", timeout=10000)
    page.wait_for_selector("button:has-text('Check again')", timeout=20000)
    # the overview: a row per check; each check is its own report, reached from the tab bar
    assert page.locator(".qoverview .srow").count() == 8 and page.locator(".stabs [role=tab]").count() == 9
    assert page.locator("h2", has_text="Stored twice").count() == 0, "a report shows only when opened"
    page.locator(".stabs [role=tab]", has_text="Stored twice").click()
    assert page.locator("h2", has_text="Stored twice").count() == 1 and page.locator("h2", has_text="Outside the photo").count() == 0
    assert page.evaluate("location.hash") == "#/quality?check=near", "the report has its own link"
    page.locator(".eseg button", has_text="Larger").click()
    page.wait_for_selector(".qitems.huge, h2:has-text('Stored twice')", timeout=10000)
    if page.locator(".qitems.huge .hrow img").count():
        assert page.locator(".qitems.huge .hrow img").first.bounding_box()["height"] >= 250, "large pictures"
    page.locator(".eseg button", has_text="List").click()
    page.wait_for_selector(".qitems.huge", state="detached", timeout=10000)  # the report is drawn again at list size
    import json as _json
    faces = _get(server, "/api/faces/queue?kind=unknown&limit=5")["faces"]
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
    with page.expect_response(lambda r: r.url.endswith("/api/faces/op"), timeout=10000) as moved:
        page.locator(".adjustbar button", has_text="Save").click()
    assert moved.value.ok, f"the move failed: {moved.value.status} {moved.value.text()[:300]}"
    page.wait_for_selector(".adjustbar", state="detached", timeout=10000)  # the adjust mode ends with the save
    assert posted and posted[0]["op"] == "move" and posted[0]["new_rect"] != posted[0]["rect"], posted
    page.unroute("**/api/faces/op")
    try:
        _post(server, "/api/faces/op", {"op": "move", "image": f["photo"], "rect": posted[0]["new_rect"], "new_rect": posted[0]["rect"]})  # put the scratch library back
    except urllib.error.HTTPError as e:
        raise AssertionError(f"putting the face back failed: {e.code} {e.read()[:300]}")

def test_accept_as_it_is_removes_only_that_entry(page, server, lib):
    """Accepting one photo folds its entry away in place: the page is not redrawn, so the spot is kept."""
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
        page.goto(server + "/#/quality?check=unnamed")
        page.wait_for_selector("h2:has-text('Faces without a name')", timeout=20000)
        sec = page.locator(".qsec", has=page.locator("h2", has_text="Faces without a name"))
        before = sec.locator(".hrow").count()
        if before < 1:
            pytest.skip("the planted photos did not reach the list")
        page.evaluate("document.querySelector('.qsec').dataset.mark = 'kept'")
        sec.locator(".hrow").first.locator(".lnk", has_text="Accept as it is").click()
        expect(sec.locator(".hrow")).to_have_count(before - 1, timeout=5000)
        assert sec.locator(".hrow").count() == before - 1
        assert page.evaluate("document.querySelector('.qsec').dataset.mark") == "kept", "the page was not redrawn"
    finally:
        dk = _s.connect(lib["db"])
        for p in pids:
            dk.execute("DELETE FROM ImageTagProperties WHERE imageid = ?", (p,))
            dk.executemany("INSERT INTO ImageTagProperties VALUES (?,?,?,?)", saved[p])
        dk.commit(); dk.close()
        for p in pids:
            _post(server, "/api/quality/accept", {"photos": [p], "on": False})

def test_face_quality_loads_the_next_entries_in_place(page, server, lib):
    """A list longer than one page offers "Load the next ...", which appends entries without redrawing the page."""
    import json as _json
    ids = lib["ids"][:3]
    real = _get(server, "/api/quality")
    real["untagged"] = {"count": 3, "accepted": 0, "items": [{"photo": ids[0], "people": 2, "signature": "p2"}]}

    def first(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(real))

    def nxt(route):
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({"total": 3, "items": [{"photo": i, "people": 1, "signature": "p1"} for i in ids[1:]]}))
    page.route("**/api/quality", first)
    page.route("**/api/quality/items*", nxt)
    page.goto(server + "/#/quality?check=untagged")
    sec = page.locator(".qsec", has=page.locator("h2", has_text="People, but nobody tagged"))
    sec.wait_for(timeout=20000)
    assert sec.locator(".hrow").count() == 1
    page.evaluate("document.querySelector('.qsec').dataset.mark = 'kept'")
    sec.locator("button", has_text="Load the next").click()
    expect(sec.locator(".hrow")).to_have_count(3, timeout=5000)
    assert sec.locator(".hrow").count() == 3 and page.evaluate("document.querySelector('.qsec').dataset.mark") == "kept"
    assert not sec.locator("button", has_text="Load the next").is_visible(), "nothing left to load"

def test_explore_rediscover_and_no_settings(page, server):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(server + "/#/explore")
    page.wait_for_selector("h2:has-text('Rediscover')", timeout=15000)
    sec = page.locator("h2:has-text('Rediscover') + .jrows")
    assert 1 <= sec.locator(".tile").count() <= 36
    first = sec.locator(".tile img").first.get_attribute("src")
    with page.expect_response(lambda r: "/api/random" in r.url, timeout=10000):
        page.locator("h2:has-text('Rediscover') .lnk", has_text="Shuffle").click()
    page.wait_for_selector("h2:has-text('Rediscover') + .jrows .tile", timeout=5000)
    assert sec.locator(".tile").count() >= 1
    order = page.evaluate("[...document.querySelectorAll('#main h2')].map((e) => e.textContent)")
    i = next(k for k, t in enumerate(order) if t.startswith("Rediscover"))
    assert i == 0 or order[i - 1].startswith("On this day"), order
    nxt = order[i + 1] if not order[i + 1].startswith("Then and now") else order[i + 2]  # (shown when someone's faces span years)
    assert nxt.startswith("Places") or nxt.startswith("People"), "right before Places (or People when there are no places)"
    assert page.locator("#main h2", has_text="Editing").count() == 0 and page.locator("#main input[type=checkbox]").count() == 0, "no settings on Explore"
    assert page.locator("#main a.pill", has_text="Settings").count() == 0 and page.locator("#main a.pill", has_text="Maintenance").count() == 0
    _ = first

def test_maintenance_explains_the_model_choices(page, server):
    page.goto(server + "/#/maintenance?tab=models")
    page.wait_for_selector(".mtable tr[data-job=facedetect] .mcompare", timeout=15000)
    row = page.locator(".mtable tr[data-job=facedetect]")
    assert "Face detection" in row.locator(".mwhat").inner_text()
    row.locator(".mcompare summary").click()
    assert row.locator(".mopt").count() == 3 and "WIDER Face" in row.locator(".mopts").inner_text()

SHOWN = "(id) => { return [...document.querySelectorAll('#grid img')].some((i) => new RegExp(`/thumb/${id}(\\D|$)`).test(i.getAttribute('src'))); }"  # the photo's tile is on the page

def test_delete_key_asks_then_moves_to_the_trash_at_once_and_undo_restores(page, server, lib):
    """Delete asks first (the app's own dialog, never the browser's); on OK the tile is gone at once and a message
    says what is happening, before the server (which rebuilds its index in the background) has answered."""
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(server + "/#/photos?sort=old"); page.wait_for_selector("#grid .tile img", timeout=20000); _wait_flags(page)
    page.wait_for_timeout(500)  # a fixed wait on purpose: the grid settles its first rows (nothing in the DOM says when) before the selection click
    tile = page.locator("#grid .tile").first
    pid = int(tile.locator("img").get_attribute("src").split("/thumb/")[1].split("?")[0].split("-")[0])
    tile.hover()
    tile.locator(".tcheck").click(force=True)
    page.wait_for_selector("#selbar", timeout=5000)
    before = page.locator("#grid .tile:visible").count()
    assert page.evaluate(SHOWN, pid)
    page.keyboard.press("Delete")
    page.wait_for_selector(".modal-back", timeout=5000)
    assert "trash" in page.locator(".modal").inner_text().lower()
    page.keyboard.press("Escape")
    page.wait_for_selector(".modal-back", state="hidden", timeout=5000)
    assert page.locator("#grid .tile:visible").count() == before, "Escape keeps the photo"
    page.keyboard.press("Delete")
    page.wait_for_selector(".modal-back", timeout=5000)
    with page.expect_response(lambda r: "/api/photos/trash/" in r.url, timeout=15000) as moved:  # the background move is done
        page.locator(".modal button", has_text="Move to trash").click()
        page.wait_for_function(SHOWN.replace("return", "return !"), arg=pid, timeout=5000)  # gone at once, not when the server answers (which the block below also waits for)
    assert not page.evaluate(SHOWN, pid), f"the photo came back: the move answered {moved.value.json()}"  # a failed move brings it back on purpose
    assert not dialogs, dialogs
    assert "trash" in page.locator("#toast").inner_text().lower()
    assert any(t["id"] == pid for t in _get(server, "/api/trash")["items"])
    # another page, and back: the photo does not come back while the library catches up
    page.evaluate("location.hash = '#/explore'"); page.wait_for_selector(".stats .stat", timeout=10000)
    page.evaluate("location.hash = '#/photos?sort=old'"); page.wait_for_selector("#grid .tile img", timeout=10000)
    assert not page.evaluate(SHOWN, pid)
    with page.expect_response(lambda r: r.url.endswith("/api/trash/restore"), timeout=15000):  # the undo has reached the server
        page.locator("#toast .lnk", has_text="Undo").click()
    assert not any(t["id"] == pid for t in _get(server, "/api/trash")["items"]), "back out of the trash"

def test_import_page_plans_then_imports_a_dropped_photo(page, server, lib, tmp_path):
    from helpers import _dated, _cleanup, _src
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
    assert page.locator(".stable.wide .lnk", has_text="already in the library").count() == 1 and page.locator(".stable.wide .mst.ok", has_text="new").count() == 1
    btn = page.locator("button.pillbtn.on", has_text="Import 1")
    assert btn.count() == 1
    try:
        btn.click()
        page.wait_for_selector("#toast:has-text('1 photo imported')", timeout=20000)
        side = next(Path(lib["root"]).glob("1998/199807/*.xmp"), None)
        assert side is not None and side.with_suffix("").exists(), "the photo and its sidecar are in the dated folder"
    finally:
        _cleanup(None, lib, ["/1998"])

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
    page.add_init_script("localStorage.setItem('iopix.units', 'km')")  # the sizes below are in kilometres
    page.goto(server + "/#/map")
    try:
        page.wait_for_selector(".mapbar button:has-text('Place photos')", timeout=15000)
    except Exception:
        pytest.skip("the map needs tiles from the internet")
    assert page.locator(".mapbar .mapfind input").count() == 1, "the map has its own place search"
    page.locator(".mapbar button:has-text('Place photos')").click()
    page.wait_for_selector(".maptray .mthead", timeout=15000)
    assert page.locator(".maptray .mapsearch input").count() == 1 and page.locator(".maptray .mtsize input[type=range]").count() == 1
    lay = page.locator(".leaflet-control-layers")
    if lay.count():  # the Map / Satellite switch stays clear of the tray
        a, b = lay.first.bounding_box(), page.locator(".maptray").bounding_box()
        assert a["x"] + a["width"] <= b["x"] or a["y"] >= b["y"] + b["height"] or a["x"] >= b["x"] + b["width"] or a["y"] + a["height"] <= b["y"], (a, b)
    assert "without a location" in page.locator(".maptray .mthead b").inner_text() or page.locator(".maptray .tgrid img").count() >= 1
    # only the town is known: photos dropped on the map are saved as approximate
    tiles = page.locator(".maptray .tgrid img")  # drawn with the tray's heading, waited for above
    if tiles.count():
        pid = int(tiles.first.get_attribute("src").rsplit("/", 1)[-1])
        tiles.first.click()
        page.locator(".maptray .mttown input[type=checkbox]").check()
        page.locator(".maptray .mttown select").select_option("5000")
        page.locator(".leaflet-container").first.click(position={"x": 120, "y": 300})
        def placed():
            d = _get(server, f"/api/photo/{pid}")
            return d if d.get("extra", {}).get("gpsAccuracy") else None
        d = _eventually(placed) or _get(server, f"/api/photo/{pid}")
        assert d["extra"]["gpsAccuracy"] == 5000 and d["alt"] is None, d.get("extra")
        _post(server, "/api/edit/revert", {"ids": [pid]})
    page.locator(".mapbar button:has-text('Done placing')").click()
    page.wait_for_selector(".mapbar button:has-text('Place photos')", timeout=10000)
    assert page.locator(".maptray").count() == 0

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
    page.goto(server + "/#/quality?check=nolocation")
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
    page.wait_for_selector(".stats .stat", timeout=15000)  # drawn from /api/meta: the suggestions need it
    page.fill("#q", "yellowstone")
    page.wait_for_selector("#suggest .sg, .sg", timeout=20000)  # the suggestion list draws as you type (slow late in a full run)
    page.keyboard.press("Enter")
    page.wait_for_function("location.hash.includes('q=') || location.hash.includes('place') || location.hash.includes('tag')", timeout=10000)
    assert page.locator(".chip").count() >= 1
    assert not errors, errors

def test_update_banner_runs_the_update_and_reloads_on_the_new_version(page, server):
    """Update now on the banner starts update.sh (mocked here), says so, and watches /api/status: a new version reloads the page."""
    import json as _json
    state = {"version": None}
    def fake_status(route):
        try:
            r = route.fetch()
            body = r.json()
            body["updateAvailable"] = {"remote": "abc1234", "version": "9.9.9", "message": "v9.9.9: something new", "date": "2026-10-16"}
            if state["version"]:
                body["appVersion"] = state["version"]
            route.fulfill(response=r, body=_json.dumps(body), headers={**r.headers, "content-type": "application/json"})
        except Exception:
            pass  # a status request still in flight when the page reloads itself: its response is gone with the old page
    runs = []
    def fake_update(route):
        if route.request.method != "POST":
            route.fallback(); return
        runs.append(route.request.post_data_json)
        route.fulfill(status=200, body='{"ok": true}', headers={"content-type": "application/json"})
    page.route("**/api/status", fake_status)
    page.route("**/api/update", fake_update)
    page.goto(server + "/#/photos")
    page.wait_for_selector("#updbanner button:has-text('Update now')", timeout=10000)
    page.locator("#updbanner button:has-text('Update now')").click()
    page.locator(".modal-btns .pillbtn.on").click()  # the confirmation
    page.wait_for_function("document.querySelector('#updbanner') && document.querySelector('#updbanner').innerText.includes('Updating')", timeout=5000)
    assert runs == [{"action": "run"}]
    assert page.locator("#updbanner button:has-text('Update now')").is_hidden()
    with page.expect_event("load", timeout=15000):  # the version poll (every 5 s) sees the new one and reloads
        state["version"] = "9.9.9"
    page.wait_for_selector("#topbar", timeout=10000)

def test_several_unlocated_photos_get_one_location_together(page, server, lib):
    """Metadata quality › No location: tick photos and set their location in one go; the rows fold away."""
    unlocated = lambda: {x["photo"] for x in (_get(server, "/api/quality").get("nolocation") or {}).get("items", [])}  # noqa: E731
    ids = sorted(unlocated())[:2]
    if len(ids) < 2:  # the fixture's photos all have a place: take it off two of them for the test (reverted at the end)
        for i in lib["ids"]:
            if len(ids) >= 2:
                break
            d = _get(server, f"/api/photo/{i}")
            if i not in ids and d.get("lat") is not None and not d.get("video"):
                _post(server, "/api/edit", {"ids": [i], "changes": {"gps": None}}); ids.append(i)
        assert len(ids) == 2
        assert _eventually(lambda: set(ids) <= unlocated(), timeout=30), "the photos without a place reach the No location report"
    page.goto(server + "/#/quality?check=nolocation")
    page.wait_for_selector(".qsec[data-check=nolocation] .hrow .qpick input", timeout=20000)
    assert page.locator("#qbulk").is_hidden(), "no bar until something is ticked"
    page.evaluate("document.querySelector('.qsec[data-check=nolocation]').dataset.mark = 'kept'")
    for i in ids:
        page.locator(f".qsec[data-check=nolocation] .hrow[data-photo='{i}'] .qpick input").check()
    # the actions float at the bottom of the page (the consistency page's bar), so no scrolling back up
    assert page.locator("#qbulk").is_visible() and "2 photos ticked" in page.locator("#qbulk .n").inner_text()
    assert page.evaluate("getComputedStyle(document.querySelector('#qbulk')).position") == "fixed"
    assert page.evaluate("document.body.classList.contains('cbulk-open')"), "the page leaves room for the bar"
    page.locator("#qbulk button", has_text="Set their location").click()
    page.wait_for_selector(".modal h3:has-text('Set location')", timeout=5000)
    assert "2 selected photos" in page.locator(".modal p.muted").first.inner_text()
    page.locator(".modal input[placeholder='latitude']").fill("40.3834")
    page.locator(".modal input[placeholder='longitude']").fill("-80.0459")
    with page.expect_response(lambda r: "/api/edit" in r.url and r.request.method == "POST", timeout=15000):
        page.locator(".modal button", has_text="Save location").click()
    try:
        for i in ids:
            page.wait_for_function("(i) => !document.querySelector(`.qsec[data-check=nolocation] .hrow[data-photo='${i}']`)", arg=i, timeout=5000)
        page.wait_for_timeout(2500)  # a fixed wait on purpose: the page must NOT be drawn again, and this outlasts any delayed redraw it used to schedule
        assert page.evaluate("document.querySelector('.qsec[data-check=nolocation]')?.dataset.mark") == "kept", "the page was drawn again: the user loses their place"
        assert page.locator("#qbulk").is_hidden(), "nothing ticked after the save"
        for i in ids:
            assert _eventually(lambda: abs((_get(server, f"/api/photo/{i}").get("lat") or 0) - 40.3834) < 1e-4), "saved on the server"
    finally:
        _post(server, "/api/edit/revert", {"ids": ids})

def test_quality_page_keeps_its_place_after_an_edit_in_the_viewer(page, server, lib):
    """A photo opened from the No location report and given a place in the viewer: closing the viewer must not draw the
    report again (the library changed under it, but it is a work page, not a list)."""
    unlocated = lambda: {x["photo"] for x in (_get(server, "/api/quality").get("nolocation") or {}).get("items", [])}  # noqa: E731
    ids = sorted(unlocated())[:1]
    if not ids:
        for i in lib["ids"]:
            d = _get(server, f"/api/photo/{i}")
            if d.get("lat") is not None and not d.get("video"):
                _post(server, "/api/edit", {"ids": [i], "changes": {"gps": None}}); ids = [i]; break
        assert ids and _eventually(lambda: set(ids) <= unlocated(), timeout=30)
    pid = ids[0]
    page.goto(server + "/#/quality?check=nolocation")
    page.wait_for_selector(f".qsec[data-check=nolocation] .hrow[data-photo='{pid}'] img", timeout=20000)
    page.evaluate("document.querySelector('.qsec[data-check=nolocation]').dataset.mark = 'kept'")
    page.locator(f".qsec[data-check=nolocation] .hrow[data-photo='{pid}'] img").click()
    _viewer_ready(page)
    page.keyboard.press("i"); page.wait_for_selector(".lb-info .iactions", timeout=10000)
    page.locator(".lb-info .iactions").click()
    page.locator(".facemenu .fmi", has_text="location").first.click()
    page.wait_for_selector(".modal h3:has-text('Set location')", timeout=5000)
    page.locator(".modal input[placeholder='latitude']").fill("40.3834")
    page.locator(".modal input[placeholder='longitude']").fill("-80.0459")
    meta = page.evaluate("import('/js/state.js').then((m) => m.S.META.version)")
    try:
        with page.expect_response(lambda r: "/api/edit" in r.url and r.request.method == "POST", timeout=15000):
            page.locator(".modal button", has_text="Save location").click()
        assert _eventually(lambda: pid not in unlocated(), timeout=20), "the library changed under the report"
        page.wait_for_function("(v) => import('/js/state.js').then((m) => m.S.META.version !== v)", arg=meta, timeout=15000)  # the page polled after the edit (the viewer still open) and saw the change
        page.keyboard.press("Escape")
        page.wait_for_selector("#lightbox", state="hidden", timeout=5000)
        page.wait_for_timeout(800)  # a fixed wait on purpose: the report must NOT be drawn again, and nothing marks a redraw that does not happen
        assert page.evaluate("document.querySelector('.qsec[data-check=nolocation]')?.dataset.mark") == "kept", "closing the viewer drew the report again"
    finally:
        _post(server, "/api/edit/revert", {"ids": [pid]})

def test_badges_on_thumbnails_outside_the_photos_grid(page, server):
    """The favorite, face-tag and GPS badges of the Photos grid also mark the thumbnails of Explore › Rediscover
    (and On this day, duplicates), from the same component: /api/badges answers what /api/photos does."""
    page.goto(server + "/#/photos")
    page.wait_for_selector("#grid .tile", timeout=15000)
    d = page.evaluate("(async () => (await fetch('/api/photos')).json())()")
    geo, faces, vids = set(d["geo"]), set(d["faces"]), {int(k) for k in d["video"]}
    at = {x: i for i, x in enumerate(d["id"])}
    pick = lambda ok, but=(): next((x for x in d["id"] if ok(at[x]) and x not in but), None)  # noqa: E731
    g = pick(lambda i: i in geo)
    f = pick(lambda i: i in faces, (g,)) or pick(lambda i: i in faces)
    fav = pick(lambda i: i not in vids, (g, f))
    bare = pick(lambda i: i not in geo and i not in faces and i not in vids, (g, f, fav))
    if not (g and f and fav):
        pytest.skip("the test library needs geotagged and face-tagged photos")
    ids = [x for x in dict.fromkeys((g, f, fav, bare)) if x]
    b = page.evaluate(f"(async () => (await fetch('/api/badges?ids={','.join(map(str, ids))}')).json())()")
    assert set(b["geo"]) == {x for x in ids if at[x] in geo} and set(b["faces"]) == {x for x in ids if at[x] in faces}, b
    r = page.request.post(server + "/api/favorites", data={"ids": [fav], "on": True})
    assert r.ok
    try:
        page.route("**/api/random*", lambda route: route.fulfill(content_type="application/json", body=json.dumps(
            {"id": ids, "w": [300] * len(ids), "h": [200] * len(ids), "ts": [d["ts"][at[x]] for x in ids]})))
        page.goto(server + "/#/explore")
        page.reload()
        sec = page.locator("h2:has-text('Rediscover') + .jrows")
        page.wait_for_selector("h2:has-text('Rediscover') + .jrows .tile.badged .cbadge", timeout=15000)
        tile = lambda x: sec.locator(f".tile[data-id='{x}']")  # noqa: E731
        for x in ids:
            assert tile(x).locator(".cbadge[title='Has GPS coordinates']").count() == (at[x] in geo), x
            assert tile(x).locator(".cbadge[title='Has face tags']").count() == (at[x] in faces), x
        assert tile(fav).locator(".fbadge").count() == 1
        if bare:
            assert tile(bare).locator(".fbadge, .cbadge, .vbadge").count() == 0
        # the same look as on the Photos grid: same classes, so the same size and place
        box, img = tile(g).locator(".cbadges").bounding_box(), tile(g).bounding_box()
        assert img["x"] + img["width"] - box["x"] - box["width"] < 12 and img["y"] + img["height"] - box["y"] - box["height"] < 12, (box, img)
    finally:
        page.request.post(server + "/api/favorites", data={"ids": [fav], "on": False})

# what sits in the middle of the page: the thumbnail, card or row there, and how far from the top of the window
_WHAT_IS_THERE = """() => { const m = document.querySelector('#main').getBoundingClientRect(), x = m.left + m.width / 2, y = m.top + m.height / 2;
  for (const dy of [0, 20, -20, 40, -40, 60, -60, 80, -80]) for (const dx of [0, -200, 200]) { const e = document.elementFromPoint(x + dx, y + dy)?.closest('.tile, .ghead, .card, .pcard, .frow, .trow, .row, li, a');
    if (e) { const img = e.querySelector('img'); return [(e.textContent || '').trim().slice(0, 40), e.dataset.i || (img ? img.getAttribute('src') || '' : ''), Math.round(e.getBoundingClientRect().top)]; } }
  return null; }"""

@pytest.mark.parametrize("view", ["photos", "folders", "tags", "people"])
def test_a_library_change_does_not_redraw_the_page_being_looked_at(browser, server, view):
    """A change from elsewhere (here: an edit through the API, as digiKam or a job would make) leaves the page as it is,
    scroll position and all, and offers a note; Show changes draws it again in the same place."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 420}, bypass_csp=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    pid = _get(server, "/api/photos")["id"][2]
    call = lambda path, body: _post(server, path, body)  # noqa: E731

    poll = "import('/js/main.js').then((m) => m.pollStatus()).then(() => import('/js/state.js')).then((m) => m.S.META.version)"
    try:
        page.goto(f"{server}/#/{view}")
        page.wait_for_selector("#main .tile img, #main .tree, #main .cards, #main .page", timeout=15000)
        _wait_flags(page)
        page.evaluate(poll)  # whatever the server had waiting is taken in before the test's own change (a redraw happens inside the poll)
        page.wait_for_selector("#main .tile img, #main .tree, #main .cards, #main .page", timeout=15000)
        room = page.locator("#main").evaluate("m => m.scrollHeight - m.clientHeight")
        assert room > 60, f"{view}: the page must scroll for this test ({room}px)"
        page.locator("#main").evaluate(f"m => m.scrollTop = {int(room * 0.6)}")
        page.wait_for_function("(t) => Math.abs(document.querySelector('#main').scrollTop - t) < 2", arg=int(room * 0.6), timeout=5000)
        top = page.locator("#main").evaluate("m => m.scrollTop")
        page.wait_for_function(_WHAT_IS_THERE, timeout=5000)  # the grid draws the rows at the new position a moment after the scroll
        seen = page.evaluate(_WHAT_IS_THERE)
        page.evaluate("document.querySelector('#main').firstElementChild.dataset.old = '1'")  # a redraw replaces this element
        meta = page.evaluate("import('/js/state.js').then((m) => m.S.META.version)")
        call("/api/edit", {"ids": [pid], "changes": {"rating": 1}})
        assert _eventually(lambda: page.evaluate(poll) != meta, 15), "the page saw the change"
        page.wait_for_selector("#libnote", timeout=5000)
        assert page.evaluate("document.querySelector('#main').firstElementChild.dataset.old") == "1", f"{view} was drawn again under the user"
        assert seen and page.evaluate(_WHAT_IS_THERE) == seen, f"{view} moved under the user: {seen} / {page.evaluate(_WHAT_IS_THERE)}"
        page.click("#libnote .pillbtn")
        page.wait_for_function("!document.querySelector('#main').firstElementChild?.dataset.old", timeout=10000)
        page.wait_for_selector("#main .tile img, #main .tree, #main .cards, #main .page", timeout=10000)
        page.wait_for_selector("#libnote", state="detached", timeout=5000)
        assert page.locator("#libnote").count() == 0
        now = page.evaluate(_WHAT_IS_THERE)
        assert now == seen, f"{view}: Show changes moved the page (scroll {top} -> {page.locator('#main').evaluate('m => m.scrollTop')}): {seen} / {now}"
    finally:
        call("/api/edit/revert", {"ids": [pid]})
        ctx.close()
    assert not errors, "uncaught JS errors: " + "; ".join(errors)[:500]

def test_badges_change_at_once_on_every_thumbnail(page, server):
    """A favorite, a person added (even without a face box) and a location set show on the photo's thumbnails right
    away, on Explore too, without waiting for the index or a redraw; taking them off clears the badge just as fast."""
    page.goto(server + "/#/photos")
    page.wait_for_selector("#grid .tile", timeout=15000)
    d = page.evaluate("(async () => (await fetch('/api/photos')).json())()")
    geo, faces, vids = set(d["geo"]), set(d["faces"]), {int(k) for k in d["video"]}
    plain = next((d["id"][i] for i in range(d["count"]) if i not in faces and i not in vids), None)
    nogeo = next((d["id"][i] for i in range(d["count"]) if i not in geo and d["id"][i] != plain), None)
    person = page.evaluate("import('/js/state.js').then((m) => m.S.META.tags.find((t) => t.person && t.nAll)?.id)")
    if not (plain and person):
        pytest.skip("the test library needs a photo without people and a named person")
    ids = [x for x in (plain, nogeo) if x]
    page.route("**/api/random*", lambda route: route.fulfill(content_type="application/json", body=json.dumps(
        {"id": ids, "w": [300] * len(ids), "h": [200] * len(ids), "ts": [d["ts"][d["id"].index(x)] for x in ids]})))
    page.goto(server + "/#/explore")
    with page.expect_response(lambda r: "/api/badges" in r.url, timeout=15000):  # the tiles' marks come from this answer
        page.reload()
    page.wait_for_selector(f"h2:has-text('Rediscover') + .jrows .tile.badged[data-id='{plain}']", timeout=15000)
    tile = lambda x: page.locator(f"h2:has-text('Rediscover') + .jrows .tile[data-id='{x}']")  # noqa: E731
    fast = {"timeout": 1500}
    assert tile(plain).locator(".fbadge").count() == 0 and tile(plain).locator(".cbadge[title='Has face tags']").count() == 0
    page.evaluate(f"import('/js/actions.js').then((m) => m.setFavorite([{plain}], true))")
    tile(plain).locator(".fbadge").wait_for(**fast)
    try:
        page.evaluate(f"import('/js/faces.js').then((m) => m.faceOp({{ op: 'tagonly', image: {plain}, tag: {person} }}))")
        tile(plain).locator(".cbadge[title='Has face tags']").wait_for(**fast)
        page.evaluate(f"import('/js/faces.js').then((m) => m.faceOp({{ op: 'untag', image: {plain}, tag: {person} }}))")  # evaluate awaits faceOp: the server has answered
        tile(plain).locator(".cbadge[title='Has face tags']").wait_for(state="detached", **fast)
        if nogeo:
            page.evaluate(f"import('/js/editing.js').then((m) => m.editPhotos([{nogeo}], {{ gps: {{ lat: 48.8584, lon: 2.2945, alt: null }} }}))")
            tile(nogeo).locator(".cbadge[title='Has GPS coordinates']").wait_for(timeout=5000)
            page.evaluate(f"import('/js/editing.js').then((m) => m.editPhotos([{nogeo}], {{ gps: null }}))")
            tile(nogeo).locator(".cbadge[title='Has GPS coordinates']").wait_for(state="detached", timeout=5000)
    finally:
        page.evaluate(f"import('/js/actions.js').then((m) => m.setFavorite([{plain}], false))")
    tile(plain).locator(".fbadge").wait_for(state="detached", **fast)

def test_date_suggestions_and_quality_pages_render(page, server, lib):
    """Explore links to Date suggestions (editors), which renders without a model (a note says what to install);
    the damaged-files page and the quality filters load without errors."""
    page.goto(server + "/#/explore")
    page.wait_for_selector("a.pill[href='#/datesuggest']", timeout=10000)
    page.click("a.pill[href='#/datesuggest']")
    page.wait_for_selector("h1.ptitle:has-text('Date suggestions')", timeout=10000)
    assert "Date unknown" in page.inner_text(".intro")
    page.goto(server + "/#/explore")
    page.click("a.pill[href='#/placesuggest']")
    page.wait_for_selector("h1.ptitle:has-text('Place suggestions')", timeout=10000)
    assert "without a position" in page.inner_text(".intro")
    page.goto(server + "/#/jerseys")
    page.wait_for_selector("h1.ptitle:has-text('Jersey numbers')", timeout=10000)
    assert "shirt" in page.inner_text(".intro")
    page.goto(server + "/#/clothing")
    page.wait_for_selector("h1.ptitle:has-text('Clothing matches')", timeout=10000)
    assert "clothes" in page.inner_text(".intro")
    page.goto(server + "/#/rotation")
    page.wait_for_selector("h1.ptitle:has-text('Wrongly rotated')", timeout=10000)
    assert "upside down" in page.inner_text(".intro")
    page.goto(server + "/#/damaged")
    page.wait_for_selector("h1.ptitle:has-text('Damaged files')", timeout=10000)
    page.wait_for_selector("button:has-text('Check')", timeout=10000)
    page.goto(server + "/#/photos?rejects=1")
    page.wait_for_selector("#chips :text('Likely rejects')", timeout=10000)

def test_suggestion_cards_tag_all_and_not_them(page, server, lib):
    """With suggestions to show (the API answered here: the test library has no models), Jersey numbers draws a card per
    photo under the player's heading with Tag, Not them and a Tag all for the group; Not them posts the dismissal and the
    card leaves without the page being drawn again. Wrongly rotated shows each finding turned the way the fix would leave it."""
    import json as _json
    ids = lib["ids"][:3]
    jerseys = {"ready": True, "coverage": [5, 5], "read": 3, "named": 1, "players": [{"tag": 1, "name": "Ann", "numbers": [10]}],
               "items": [{"id": ids[0], "tag": 1, "name": "Ann", "number": 10, "basis": "folder", "n": 4}, {"id": ids[1], "tag": 1, "name": "Ann", "number": 10, "basis": "year", "n": 2}]}
    answer = lambda data: (lambda route: route.fulfill(status=200, content_type="application/json", body=_json.dumps(data)))
    page.route("**/api/jerseys*", answer(jerseys))
    posted = []
    page.route("**/api/suggestions/dismiss", lambda route: (posted.append(_json.loads(route.request.post_data)), route.fulfill(status=200, content_type="application/json", body="{}")))
    page.goto(server + "/#/jerseys")
    page.wait_for_selector(".jgroup .sugg-item .tile img", timeout=10000)
    assert page.locator(".sugg-item").count() == 2 and "4 named in this folder" in page.locator(".sugg-item").first.text_content()
    assert page.locator(".jgroup h3 .pillbtn", has_text="Tag all").count() == 1
    assert page.locator(".sugg-item button[title^='Tag Ann']").count() == 2
    page.evaluate("document.querySelector('.jgroup').dataset.mark = 'kept'")
    page.locator(".sugg-item").first.locator("button", has_text="Not them").click()
    page.wait_for_function("document.querySelectorAll('.sugg-item').length === 1", timeout=5000)
    assert posted == [{"kind": "jersey", "pairs": [[ids[0], 1]]}]
    assert page.evaluate("document.querySelector('.jgroup').dataset.mark") == "kept", "the page was drawn again"
    rotation = {"ready": True, "running": False, "coverage": [1, 1], "dismissed": 2, "items": [{"id": ids[2], "op": "right", "text": "On its left side", "score": 0.97}]}
    page.route("**/api/rotation*", answer(rotation))
    page.goto(server + "/#/rotation")
    page.wait_for_selector(".sugg-item .tile img[title^='Shown the way']", timeout=10000)
    assert "2 marked as fine are left out" in page.inner_text(".intro")
    assert page.locator(".sugg-item button", has_text="It’s fine").count() == page.evaluate("import('/js/state.js').then((m) => m.S.CAN_MANAGE ? 1 : 0)")


# --- the suggestion pages, with the models' answers mocked --------------------------------------------------------
# The test library has no AI models, so the endpoints the pages read (/api/datesuggest, /api/placesuggest, /api/jerseys,
# /api/clothing, /api/rotation) are answered by the test; everything a button posts goes to the real server and is read
# back from it, and the mocked answer leaves out what the real endpoint would leave out after that write (a photo that
# got its date or position, a person who is tagged, a pair in the dismissal store), so a reload shows what sticks.

def _dismissed(kind):
    """{(photo id, tag id)} in the browser tests' server's dismissal store (app.data.dismissed; read, never written here)."""
    con = ui_db()
    try:
        with con.cursor() as cur:
            cur.execute("SELECT photo_id, tag_id FROM suggestion_dismissed WHERE kind = %s", (kind,))
            return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        con.close()


def _answer(page, pattern, data, keep):
    """Route `pattern` to `data`, with only the items `keep(item)` still wants (what the real endpoint would answer)."""
    page.route(pattern, lambda route: route.fulfill(status=200, content_type="application/json",
                                                     body=json.dumps(dict(data, items=[x for x in data["items"] if keep(x)]))))


def _cards(page):
    return page.locator(".sugg-item")


def _page_kept(page):
    """Mark the page; `page.evaluate(_MARK)` then says whether it is still the same element (not drawn again)."""
    page.evaluate("document.querySelector('#main .page').dataset.mark = 'kept'")


_MARK = "document.querySelector('#main .page')?.dataset.mark === 'kept'"


def test_date_suggestions_use_saves_the_year_only(page, server, lib):
    """Date suggestions: Use saves the photo's date as that year only (through /api/edit, undone at the end), the card
    leaves without the page being drawn again, and after a reload the photo, dated now, is no longer suggested."""
    pid, other = [i for i in lib["ids"] if i not in lib["videos"]][:2]
    assert _get(server, f"/api/photo/{pid}").get("datePrecision") != "year", "the scratch photo has an exact date to start from"
    data = {"total": 2, "offset": 0, "ready": True, "sources": {"ages": True, "look": False}, "faces": [1, 1], "people": [],
            "items": [{"id": pid, "year": 1992, "lo": 1990, "hi": 1994, "clues": [{"kind": "age", "text": "Ann looks about 12", "year": 1992, "sd": 2}], "people": []},
                      {"id": other, "year": 1995, "lo": 1993, "hi": 1997, "clues": [{"kind": "camera", "text": "the camera of 1993 to 1997", "year": 1995, "sd": 2}], "people": []}]}
    _answer(page, "**/api/datesuggest*", data, lambda x: _get(server, f"/api/photo/{x['id']}").get("datePrecision") != "year")  # a dated photo is no longer "Date unknown"
    try:
        page.goto(server + "/#/datesuggest")
        page.wait_for_selector(".sugg-item .tile img", timeout=10000)
        assert _cards(page).count() == 2 and "About 1992" in _cards(page).first.text_content()
        _page_kept(page)
        with page.expect_response(lambda r: r.url.endswith("/api/edit") and r.request.method == "POST", timeout=15000) as saved:
            _cards(page).first.locator("button", has_text="Use 1992").click()
        assert saved.value.ok
        expect(_cards(page)).to_have_count(1, timeout=5000)
        assert page.evaluate(_MARK), "the page was drawn again"
        assert _cards(page).locator(f".tile[data-id='{other}']").count() == 1, "the other card stays"
        assert _eventually(lambda: (lambda d: d.get("datePrecision") == "year" and d["taken"].startswith("1992"))(_get(server, f"/api/photo/{pid}")), timeout=20), "saved as the year 1992 only"
        page.reload()
        page.wait_for_selector("h1.ptitle:has-text('Date suggestions')", timeout=10000)
        expect(_cards(page)).to_have_count(1, timeout=10000)
        assert _cards(page).locator(f".tile[data-id='{other}']").count() == 1, "only the undated photo is suggested now"
    finally:
        _post(server, "/api/edit/revert", {"ids": [pid]})


def test_place_suggestions_use_saves_an_approximate_position(page, server, lib):
    """Place suggestions: Use saves the suggested position with its accuracy (through /api/edit, undone at the end), the
    card leaves without a redraw, and after a reload the photo, placed now, is no longer suggested."""
    pid, other = [i for i in lib["ids"] if i not in lib["videos"]][:2]
    item = lambda i, lat, lon, km: {"id": i, "lat": lat, "lon": lon, "km": km, "place": "Paris", "region": "Île-de-France", "country": "France",  # noqa: E731
                                    "basis": "between", "text": "between two photos taken shortly before and after", "from": []}
    data = {"total": 2, "offset": 0, "items": [item(pid, 48.8584, 2.2945, 0.3), item(other, 48.8606, 2.3376, 1.2)]}
    placed = lambda x: abs((_get(server, f"/api/photo/{x['id']}").get("lat") or 0) - x["lat"]) < 1e-4  # noqa: E731
    assert not placed(data["items"][0]), "the scratch photo is not at the suggested spot to start with"
    _answer(page, "**/api/placesuggest*", data, lambda x: not placed(x))  # a photo with a position is no longer suggested
    try:
        page.goto(server + "/#/placesuggest")
        page.wait_for_selector(".sugg-item .tile img", timeout=10000)
        assert _cards(page).count() == 2 and "within 300 m" in _cards(page).first.text_content()
        _page_kept(page)
        with page.expect_response(lambda r: r.url.endswith("/api/edit") and r.request.method == "POST", timeout=15000) as saved:
            _cards(page).first.locator("button", has_text="Use").click()
        assert saved.value.ok
        expect(_cards(page)).to_have_count(1, timeout=5000)
        assert page.evaluate(_MARK), "the page was drawn again"
        assert _eventually(lambda: (lambda d: abs((d.get("lat") or 0) - 48.8584) < 1e-4 and d.get("extra", {}).get("gpsAccuracy") == 300)(_get(server, f"/api/photo/{pid}")), timeout=20), \
            "saved at the suggested spot, within 300 m"
        page.reload()
        page.wait_for_selector("h1.ptitle:has-text('Place suggestions')", timeout=10000)
        expect(_cards(page)).to_have_count(1, timeout=10000)
        assert _cards(page).locator(f".tile[data-id='{other}']").count() == 1, "only the photo without a position is suggested now"
    finally:
        _post(server, "/api/edit/revert", {"ids": [pid]})


def _person_and_photos(server, lib, n):
    """A named person and `n` photos (no videos) they are not tagged on: (tag id, name, [photo ids])."""
    meta = _get(server, "/api/meta")
    person = next(t for t in meta["tags"] if t.get("person") and t.get("nAll") and not any(c.get("pid") == t["id"] for c in meta["tags"]))
    ids = []
    for i in lib["ids"]:
        if i not in lib["videos"] and all(t["id"] != person["id"] for t in _get(server, f"/api/photo/{i}")["tags"]):
            ids.append(i)
        if len(ids) == n:
            break
    assert len(ids) == n, "the scratch library needs photos the person is not on"
    return person["id"], person["name"], ids


def _tagged(server, pid, tag):
    return any(t["id"] == tag for t in _get(server, f"/api/photo/{pid}")["tags"])


def test_jersey_numbers_tag_and_not_them(page, server, lib):
    """Jersey numbers: Tag adds the player to the photo (no face region; /api/faces/batch, undone at the end), Not them
    goes into the server's dismissal store, each card leaves without a redraw, and after a reload neither is suggested:
    the tagged photo because the person is on it, the dismissed one because the store says so."""
    tag, name, (a, b) = _person_and_photos(server, lib, 2)
    data = {"total": 2, "offset": 0, "ready": True, "coverage": [5, 5], "read": 3, "named": 1, "players": [{"tag": tag, "name": name, "numbers": [10]}],
            "items": [{"id": a, "tag": tag, "name": name, "number": 10, "basis": "folder", "n": 4}, {"id": b, "tag": tag, "name": name, "number": 10, "basis": "year", "n": 2}]}
    _answer(page, "**/api/jerseys*", data, lambda x: not _tagged(server, x["id"], x["tag"]) and (x["id"], x["tag"]) not in _dismissed("jersey"))
    try:
        page.goto(server + "/#/jerseys")
        page.wait_for_selector(".jgroup .sugg-item .tile img", timeout=10000)
        assert _cards(page).count() == 2 and page.locator(".jgroup h3", has_text=name).count() == 1
        _page_kept(page)
        with page.expect_response(lambda r: r.url.endswith("/api/faces/batch") and r.request.method == "POST", timeout=15000) as tagged:
            _cards(page).locator(f".tile[data-id='{a}']").locator("xpath=..").locator("button", has_text="Tag").click()
        assert tagged.value.ok and not tagged.value.json().get("failed")
        expect(_cards(page)).to_have_count(1, timeout=5000)
        assert _eventually(lambda: _tagged(server, a, tag), timeout=20), f"{name} is on the photo"
        with page.expect_response(lambda r: r.url.endswith("/api/suggestions/dismiss") and r.request.method == "POST", timeout=15000) as dismissed:
            _cards(page).first.locator("button", has_text="Not them").click()
        assert dismissed.value.json() == {"dismissed": 1}
        expect(_cards(page)).to_have_count(0, timeout=5000)
        assert page.evaluate(_MARK), "the page was drawn again"
        assert (b, tag) in _dismissed("jersey") and (a, tag) not in _dismissed("jersey"), "only Not them is in the store"
        page.reload()
        page.wait_for_selector("h1.ptitle:has-text('Jersey numbers')", timeout=10000)
        page.wait_for_selector(".empty", timeout=10000)
        assert _cards(page).count() == 0, "neither photo is suggested after the reload"
    finally:
        _post(server, "/api/faces/op", {"op": "untag", "image": a, "tag": tag})
        _post(server, "/api/suggestions/dismiss", {"kind": "jersey", "pairs": [[b, tag]], "on": False})
    assert (b, tag) not in _dismissed("jersey"), "on: false takes the dismissal back"


def _clothing(page, server, tag, name, ids):
    item = lambda i: {"id": i, "tag": tag, "name": name, "n": len(ids), "score": 0.91, "lead": ids[0], "box": [0, 0, 100, 100], "event": "Sports day", "text": "the same red shirt"}  # noqa: E731
    data = {"total": len(ids), "offset": 0, "ready": True, "coverage": [9, 9], "items": [item(i) for i in ids]}
    _answer(page, "**/api/clothing*", data, lambda x: not _tagged(server, x["id"], x["tag"]) and (x["id"], x["tag"]) not in _dismissed("clothing"))
    page.goto(server + "/#/clothing")
    page.wait_for_selector(".jgroup .sugg-item .tile img", timeout=10000)
    assert _cards(page).count() == len(ids) and "Sports day" in _cards(page).first.text_content()


def test_clothing_matches_not_them_sticks(page, server, lib):
    """Clothing matches: Not them drops the card without a redraw and goes into the server's dismissal store, so after a
    reload only the other photo is suggested; on: false takes it back."""
    tag, name, (a, b) = _person_and_photos(server, lib, 2)
    _clothing(page, server, tag, name, [a, b])
    try:
        _page_kept(page)
        with page.expect_response(lambda r: r.url.endswith("/api/suggestions/dismiss") and r.request.method == "POST", timeout=15000) as dismissed:
            _cards(page).locator(f".tile[data-id='{a}']").locator("xpath=..").locator("button", has_text="Not them").click()
        assert dismissed.value.json() == {"dismissed": 1}
        expect(_cards(page)).to_have_count(1, timeout=5000)
        assert page.evaluate(_MARK), "the page was drawn again"
        assert (a, tag) in _dismissed("clothing") and (b, tag) not in _dismissed("clothing")
        page.reload()
        page.wait_for_selector("h1.ptitle:has-text('Clothing matches')", timeout=10000)
        expect(_cards(page)).to_have_count(1, timeout=10000)
        assert _cards(page).locator(f".tile[data-id='{b}']").count() == 1, "the dismissed photo stays out after the reload"
    finally:
        _post(server, "/api/suggestions/dismiss", {"kind": "clothing", "pairs": [[a, tag]], "on": False})
    assert (a, tag) not in _dismissed("clothing"), "on: false takes the dismissal back"


def test_clothing_matches_tag_all_tags_every_card(page, server, lib):
    """Clothing matches: Tag all on the person's section asks first, tags every photo of the section (undone at the end)
    and draws the page again, which then has nothing left to suggest."""
    tag, name, (a, b) = _person_and_photos(server, lib, 2)
    _clothing(page, server, tag, name, [a, b])
    try:
        _page_kept(page)
        page.locator(".jgroup h3 .pillbtn", has_text="Tag all").click()
        page.wait_for_selector(".modal-btns button.on", timeout=5000)  # the app's own question
        assert f"Tag {name} on these 2 photos" in page.locator(".modal").text_content()
        with page.expect_response(lambda r: r.url.endswith("/api/faces/batch") and r.request.method == "POST", timeout=15000) as tagged:
            page.locator(".modal-btns button.on").click()
        assert tagged.value.ok and not tagged.value.json().get("failed")
        assert _eventually(lambda: _tagged(server, a, tag) and _tagged(server, b, tag), timeout=20), f"{name} is on both photos"
        assert _eventually(lambda: not page.evaluate(_MARK), timeout=10), "a group action draws the page again"
        # the redraw can come before the index carries the new tags (the list is read from the index); after a reload nothing is left
        page.reload()
        page.wait_for_selector("h1.ptitle:has-text('Clothing matches')", timeout=10000)
        page.wait_for_selector(".empty", timeout=10000)
        assert _cards(page).count() == 0
    finally:
        for i in (a, b):
            if _tagged(server, i, tag):
                _post(server, "/api/faces/op", {"op": "untag", "image": i, "tag": tag})
        errors = list(page.errors); page.errors.clear()  # judged here, not by the fixture: the known error is the reason this test is expected to fail
    assert not errors, errors


def test_wrongly_rotated_its_fine_leaves_the_photo_out(page, server, lib):
    """Wrongly rotated: It's fine posts the photo to /api/rotation/dismiss (the real server: with no model, it has no
    finding to pin and answers dismissed: 0), the card leaves without a redraw, and the list that comes back after a
    reload leaves the photo out and says so, as the real endpoint does for a photo whose finding it holds."""
    a, b = [i for i in lib["ids"] if i not in lib["videos"]][:2]
    fine = []  # what the page said is fine: the (mocked) list leaves those out, as rotation.findings does for a pinned photo
    data = {"total": 2, "offset": 0, "ready": True, "running": False, "phase": None, "coverage": [2, 2], "dismissed": 0,
            "items": [{"id": a, "turn": 90, "op": "right", "text": "On its left side", "score": 0.97}, {"id": b, "turn": 180, "op": "180", "text": "Upside down", "score": 0.93}]}
    page.route("**/api/rotation?*", lambda route: route.fulfill(status=200, content_type="application/json",
                                                                 body=json.dumps(dict(data, dismissed=len(fine), items=[x for x in data["items"] if x["id"] not in fine]))))
    def through(route):  # the real server answers; the test remembers what was posted
        r = route.fetch()
        fine.extend(route.request.post_data_json["photos"])
        route.fulfill(response=r)
    page.route("**/api/rotation/dismiss", through)
    page.goto(server + "/#/rotation")
    page.wait_for_selector(".sugg-item .tile img[title^='Shown the way']", timeout=10000)
    assert _cards(page).count() == 2 and "marked as fine" not in page.inner_text(".intro")
    _page_kept(page)
    with page.expect_response(lambda r: r.url.endswith("/api/rotation/dismiss") and r.request.method == "POST", timeout=15000) as answered:
        _cards(page).locator(f".tile[data-id='{a}']").locator("xpath=..").locator("button", has_text="It’s fine").click()
    assert answered.value.ok and answered.value.json() == {"dismissed": 0}, "nothing checked by a model: nothing to pin, but the request is accepted"
    assert fine == [a]
    expect(_cards(page)).to_have_count(1, timeout=5000)
    assert page.evaluate(_MARK), "the page was drawn again"
    page.reload()
    page.wait_for_selector("h1.ptitle:has-text('Wrongly rotated')", timeout=10000)
    page.wait_for_selector(".sugg-item .tile img", timeout=10000)
    assert _cards(page).count() == 1 and _cards(page).locator(f".tile[data-id='{b}']").count() == 1
    assert "1 marked as fine is left out" in page.inner_text(".intro") or "1 marked as fine are left out" in page.inner_text(".intro")
