"""Layout and appearance: the phone layout, the sidebar, breadcrumbs, the filter pane, units, the theme and the app's name."""
from helpers import need_tool

need_tool("playwright", module_level=True)

from ui.conftest import _get, _open_photo, _post, _wait_flags  # noqa: E402


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
    assert page.evaluate("localStorage.getItem('iopix.theme')") == "system"

def test_light_is_the_default_theme(browser, server):
    ctx = browser.new_context(bypass_csp=True, color_scheme="dark")  # even on a device set to dark
    pg = ctx.new_page()
    pg.goto(server + "/#/photos")
    pg.wait_for_selector("#topbar", timeout=10000)
    assert pg.evaluate("document.documentElement.dataset.theme") == "light"
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    ctx.close()

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
    page.wait_for_selector("#grid .tile", timeout=15000)  # the grid under the trail is drawn
    assert page.evaluate(js, "#crumbbar") == a
    page.goto(server + "/#/people")
    page.wait_for_selector("#main h1:has-text('People')", timeout=15000)
    assert page.locator("#crumbbar").is_hidden(), "a page without a trail has no strip"

def test_a_top_level_folder_names_itself_in_the_trail(page, server):
    m = page.request.get(server + "/api/meta").json()
    top = next(a for a in m["albums"] if a["pid"] is None)
    page.goto(server + f"/#/photos?album={top['id']}&sort=old")
    page.wait_for_selector("#crumbbar:not([hidden]) .cur", timeout=10000)
    assert page.locator("#crumbbar .cur").inner_text() == top["name"]
    assert page.locator("#crumbbar a", has_text="Folders").count() == 1

def test_sidebar_docks_hides_and_slides_over(browser, server):
    """Wide screen: the sidebar (name, destinations, Settings) is docked beside the page; its button puts it away
    (remembered), the menu button then slides it over the page and choosing a destination puts it away again.
    A narrower screen never docks it."""
    ctx = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 860}); page = ctx.new_page()
    try:
        page.goto(server + "/#/photos"); page.wait_for_selector("body.side-docked #nav a.on", timeout=10000)
        assert page.locator(".navhead .brand-name").is_visible() and page.locator(".navhead .logo").count() == 0
        assert page.locator(".navfoot a[data-v=settings]").is_visible() and not page.locator("#menubtn").is_visible()
        nav, main = page.locator("#nav").bounding_box(), page.locator("#main").bounding_box()
        assert abs(main["x"] - (nav["x"] + nav["width"])) < 1.5, (nav, main)
        page.locator("#sidebtn").click(); page.wait_for_selector("#menubtn", state="visible", timeout=5000); page.wait_for_selector("#nav", state="hidden", timeout=5000)  # the sidebar has slid away
        assert page.locator("#menubtn").is_visible() and not page.locator("#nav").is_visible()
        assert page.locator("#main").bounding_box()["x"] == 0
        page.reload(); page.wait_for_selector("#menubtn", state="visible", timeout=10000)
        assert not page.evaluate("document.body.classList.contains('side-docked')"), "put away stays put away"
        page.locator("#menubtn").click(); page.wait_for_selector("body.side-open #nav a[data-v=people]", state="visible", timeout=3000)
        page.locator("#nav a[data-v=people]").click()  # (the click waits for the slide-over to come to rest)
        page.wait_for_function("!document.body.classList.contains('side-open')", timeout=3000)
        assert page.locator("#navscrim").is_hidden()
        page.locator("#menubtn").click(); page.wait_for_selector("body.side-open #sidebtn", state="visible", timeout=3000); page.locator("#sidebtn").click()  # the pin docks it again
        page.wait_for_selector("body.side-docked", timeout=3000)
        page.set_viewport_size({"width": 1000, "height": 760}); page.wait_for_function("!document.body.classList.contains('side-docked')", timeout=5000)
        assert not page.evaluate("document.body.classList.contains('side-docked')") and page.locator("#menubtn").is_visible()
    finally:
        ctx.close()

def test_app_name_shows_in_the_top_bar_and_tab(page, server):
    set_name = lambda v: _post(server, "/api/settings", {"app_name": v}, timeout=5)  # noqa: E731
    set_name("iothing Photos")
    try:
        page.goto(server + "/#/photos")
        page.wait_for_function("document.querySelector('.brand-name') && document.querySelector('.brand-name').textContent === 'iothing Photos'", timeout=10000)
        assert page.locator(".brand-name .io").count() == 0  # another name is one colour
        assert page.title() == "iothing Photos"
        page.goto(server + "/#/settings")
        page.wait_for_selector(".srow:has-text('Name')", timeout=10000)
        assert page.locator(".srow", has_text="calls itself").locator("input").input_value() == "iothing Photos"
    finally:
        set_name("")

def test_the_name_iopix_is_written_in_two_colours(page, server):
    page.goto(server + "/#/photos")
    page.wait_for_selector(".brand-name .io", timeout=10000)
    assert page.locator(".brand-name").text_content() == "iopix"
    io, pix = page.evaluate("[...document.querySelectorAll('.brand-name .io, .brand-name .pix')].map((e) => getComputedStyle(e).color)")
    assert io != pix  # gold and indigo, from the --name-io/--name-pix tokens

def test_filter_pane_steps_aside_on_pages_it_does_not_apply_to(page, server):
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(server + "/#/photos")
    page.wait_for_selector("#filterbtn", timeout=10000)
    if page.locator("#filters").is_hidden():
        page.locator("#filterbtn").click()
    page.wait_for_selector("#filters:not([hidden])", timeout=5000)
    page.goto(server + "/#/maintenance")
    page.wait_for_selector("h1:has-text('Maintenance')", timeout=10000)
    page.wait_for_selector("#filters", state="hidden", timeout=5000)
    assert page.locator("#filters").is_hidden(), "no filter pane on Maintenance"
    page.goto(server + "/#/photos")
    page.wait_for_selector("#filters:not([hidden])", timeout=5000)
    page.locator("#filterbtn").click()  # leave it closed for the other tests

def test_phone_layout_fits_the_screen(browser, server):
    """At phone width: the bottom bar shows four destinations and More (the rest in a sheet), the selection
    bar keeps every action on screen as icons, and nothing in the page is wider than the screen."""
    ctx = browser.new_context(bypass_csp=True, viewport={"width": 393, "height": 852}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    try:
        page.goto(server + "/#/photos"); page.wait_for_selector("#grid .tile", timeout=20000); _wait_flags(page)
        visible = [a for a in page.locator("#nav a").all() if a.is_visible()]
        assert len(visible) == 4 and page.locator("#nav .navmore").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= 393
        page.locator("#nav .navmore").tap(); page.wait_for_selector(".sheetgrid a", timeout=3000)
        assert page.locator(".sheetgrid a", has_text="Folders").count() == 1 and page.locator(".sheetgrid a", has_text="Places").count() == 0
        page.locator(".modal button", has_text="Close").tap(); page.wait_for_selector(".modal-back", state="hidden", timeout=3000)
        page.locator("#grid .selall").tap(); page.wait_for_selector("#selbar button.pillbtn", timeout=3000); _wait_flags(page)
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
        _open_photo(page, server, _get(server, "/api/photos")["id"][0])
        page.evaluate("document.querySelectorAll('#lightbox .lb-top .iconbtn').forEach((b) => { b.hidden = false; if (b.style.display === 'none') b.style.display = ''; })")
        shown = [b.bounding_box() for b in page.locator("#lightbox .lb-top .iconbtn").all() if b.is_visible()]
        assert len(shown) >= 10, len(shown)
        assert all(box["x"] + box["width"] <= 393 + 0.5 and box["width"] >= 30 for box in shown), shown
    finally:
        ctx.close()

def test_explore_is_home_and_menu_icons_are_coloured(page, server):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server + "/")
    page.wait_for_selector("#nav a[data-v=explore].on", timeout=10000)
    assert page.locator("#nav > a").first.get_attribute("data-v") == "explore"
    grey = page.evaluate("getComputedStyle(document.body).getPropertyValue('--fg2')")
    col = page.evaluate("getComputedStyle(document.querySelector('#nav a[data-v=people] .ic')).color")
    assert col and "rgb" in col and page.evaluate("(c) => { const d = document.createElement('div'); d.style.color = c; document.body.append(d); const v = getComputedStyle(d).color; d.remove(); return v; }", grey.strip()) != col, col
    page.hover("#nav a[data-v=people]")
    page.wait_for_timeout(250)  # a fixed wait on purpose: a hover colour must NOT appear, and nothing marks a style that stays away
    assert page.evaluate("getComputedStyle(document.querySelector('#nav a[data-v=people]')).backgroundColor") in ("rgba(0, 0, 0, 0)", "transparent")

def test_page_title_takes_focus_and_the_status_box_is_live(page, server):
    """After a change of page its title has focus (screen readers announce it), unless a field is being typed in;
    the status box is a live region; the query keeps everything after the first question mark."""
    page.goto(server + "/#/explore")
    page.wait_for_selector("#main h1", timeout=15000)
    assert page.locator("#status[role=status][aria-live=polite]").count() == 1
    page.locator("#nav a[data-v=people]").click()
    page.wait_for_selector("#main h1:has-text('People')", timeout=15000)
    page.wait_for_function("document.activeElement === document.querySelector('#main h1')", timeout=5000)
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "none"
    page.focus("#q")
    page.evaluate("location.hash = '#/folders'")
    page.wait_for_selector("#main h1:has-text('Folders')", timeout=15000)
    page.wait_for_timeout(300)  # a fixed wait on purpose: the focus must NOT move to the title, and nothing marks a move that does not happen
    assert page.evaluate("document.activeElement.id") == "q", "typing is not interrupted"
    assert page.evaluate("history.replaceState(null, '', '#/photos?q=why?'); import('/js/state.js').then((m) => m.parseHash().params.get('q'))") == "why?"

def test_floating_notes_take_the_width_their_words_need_on_a_phone(browser, server):
    """A note centred with left: 50% used to be sized to the right half of the screen, so its words wrapped into a
    tall column on phones; the update and digiKam cards stack in one corner instead of overlapping."""
    ctx = browser.new_context(bypass_csp=True, viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    # the status poll draws the update card from what the server says, so without this the card drawn below is taken away again
    def with_update(route):
        r = route.fetch(); body = r.json(); body["updateAvailable"] = {"version": "9.9.9", "remote": "r", "message": "v9.9.9: words"}
        route.fulfill(response=r, json=body)
    page.route("**/api/status*", with_update)
    try:
        page.goto(server + "/#/explore"); page.wait_for_selector("#nav > a", timeout=10000)
        page.evaluate("() => { import('/js/state.js').then((st) => st.toast('3 photos moved to the trash. 1 could not be: the file is not on disk.', 'Undo', () => {}, 60000)); }")
        page.evaluate("() => { const n = document.createElement('div'); n.id = 'reloadnote'; n.innerHTML = '<span>A new version of iopix is ready.</span><button class=\"pillbtn on\">Load it now</button><button class=\"iconbtn\">x</button>'; document.body.append(n); }")
        page.wait_for_selector("#toast:not([hidden])", timeout=5000); page.wait_for_selector("#reloadnote", timeout=5000)
        for sel in ("#toast", "#reloadnote"):
            box = page.locator(sel).bounding_box()
            assert box["width"] > 300 and box["height"] < 90, (sel, box)
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390, (sel, box)
        page.evaluate("() => { import('/js/main.js').then((m) => { m.updateBanner({ version: '9.9.9', remote: 'r', message: 'v9.9.9: words' }); m.dkBanner({ sync: true, reachable: false, since: 1760000000 }); }); }")
        page.wait_for_selector("#updbanner", timeout=5000)  # both cards are drawn in the same call
        if page.locator("#dkbanner").count():  # admins only
            u, d = page.locator("#updbanner").bounding_box(), page.locator("#dkbanner").bounding_box()
            assert u["y"] + u["height"] <= d["y"], "the update card sits above the digiKam card, not on it"
        assert page.locator("#updbanner").bounding_box()["width"] > 300
    finally:
        page.unroute_all(behavior="ignoreErrors")
        ctx.close()

def _units_page(browser, server, locale):
    ctx = browser.new_context(locale=locale, viewport={"width": 1280, "height": 900}, bypass_csp=True)
    pg = ctx.new_page()
    pg.goto(server + "/#/settings")
    pg.wait_for_selector("#unitseg", timeout=10000)
    return ctx, pg

_FMT = """async (u) => { const c = await import('/js/core.js'); localStorage.setItem('iopix.units', u);
    return [c.fmtDist(100), c.fmtDist(1609.344), c.fmtDist(25000), c.fmtHeight(300), c.heightUnit(), c.accuracyChoices()[0],
            c.fromHeight(c.toHeight(123.4)) ]; }"""

def test_units_follow_the_browser_region_and_the_setting(browser, server):
    """Settings > Display > Units: Automatic gives miles and feet to a US browser and kilometres to a German one;
    Miles or Kilometres fixes it. Distances are kept in metres either way."""
    ctx, pg = _units_page(browser, server, "en-US")
    try:
        assert pg.locator("#unitseg [data-units=auto]").inner_text() == "Automatic (miles here)"
        assert "on" in pg.locator("#unitseg [data-units=auto]").get_attribute("class"), "Automatic is the default"
        mi = pg.evaluate(_FMT, "auto")
        assert mi[:5] == ["328 ft", "1 mi", "15.5 mi", "984 ft", "ft"], mi
        assert mi[5] == 1609 and abs(mi[6] - 123.4) < 0.05, mi
        km = pg.evaluate(_FMT, "km")
        assert km[:5] == ["100 m", "1.6 km", "25 km", "300 m", "m"] and km[5] == 1000, km
        pg.evaluate("localStorage.removeItem('iopix.units')")
        pg.reload()
        pg.click("#unitseg [data-units=km]")
        assert pg.evaluate("localStorage.getItem('iopix.units')") == "km"
        assert "on" in pg.locator("#unitseg [data-units=km]").get_attribute("class")
    finally:
        ctx.close()
    ctx, pg = _units_page(browser, server, "de-DE")
    try:
        assert pg.locator("#unitseg [data-units=auto]").inner_text() == "Automatic (kilometres here)"
        assert pg.evaluate(_FMT, "auto")[1] == "1,6 km"
        assert pg.evaluate(_FMT, "mi")[1] == "1 mi"
    finally:
        ctx.close()

def test_location_dialog_speaks_the_chosen_units(browser, server, lib):
    """The location dialog's altitude field and "Only the town is known" sizes follow Units; an altitude
    left alone is saved in metres exactly as it was."""
    pid = next(i for i in lib["ids"] if i not in lib["videos"])
    for units, alt_ph, first in (("mi", "altitude ft", "within about 1 mi"), ("km", "altitude m", "within about 1 km")):
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900}, bypass_csp=True)
        pg = ctx.new_page()
        try:
            pg.goto(server + "/#/settings")
            pg.evaluate(f"localStorage.setItem('iopix.units', '{units}')")
            _open_photo(pg, server, pid)
            pg.evaluate("import('/js/editing.js').then((m) => m.locationDialog([%d], {lat: 40.44, lon: -79.96, alt: 250.37}))" % pid)
            pg.wait_for_selector(f".modal input[placeholder='{alt_ph}']", timeout=5000)
            opts = pg.locator(".modal select.sharepick option").all_inner_texts()
            assert opts[0] == first and len(opts) == 5, opts
            shown = pg.locator(f".modal input[placeholder='{alt_ph}']").input_value()
            assert shown == ("821.4" if units == "mi" else "250.37"), shown
            pg.locator(".modal button", has_text="Cancel").click()
            # a stored approximate position that is not one of the round sizes stays a choice, selected
            pg.evaluate("import('/js/editing.js').then((m) => m.locationDialog([%d], {lat: 40.44, lon: -79.96, accuracy: 3000}))" % pid)
            pg.wait_for_selector(".modal select.sharepick", timeout=5000)
            assert pg.locator(".modal select.sharepick").input_value() == "3000"
            assert len(pg.locator(".modal select.sharepick option").all_inner_texts()) == 6
        finally:
            ctx.close()

def test_inside_the_app_the_web_navigation_steps_aside(browser, server):
    """?app=1 (the iPhone, iPad and Mac apps): no sidebar, menu button or bottom bar, the page reaches the bottom
    edge, the mode stays for the tab, and every change of page is posted to the app's iopixRoute handler."""
    for vp in ({"width": 393, "height": 852}, {"width": 1280, "height": 860}):
        ctx = browser.new_context(bypass_csp=True, viewport=vp); page = ctx.new_page()
        page.add_init_script("window.__routes = []; window.webkit = { messageHandlers: { iopixRoute: { postMessage: (h) => window.__routes.push(h) } } };")
        try:
            page.goto(server + "/?app=1#/photos"); page.wait_for_selector("#grid .tile", timeout=20000)
            assert page.evaluate("document.documentElement.classList.contains('inapp')")
            assert not page.locator("#nav").is_visible() and not page.locator("#menubtn").is_visible()
            main = page.locator("#main").bounding_box()
            assert main["x"] == 0 and abs(main["y"] + main["height"] - vp["height"]) < 1.5, main
            page.evaluate("location.hash = '#/people'"); page.wait_for_function("window.__routes.includes('#/people')", timeout=5000)
            assert "#/photos" in page.evaluate("window.__routes")
            page.goto(server + "/#/explore"); page.wait_for_selector("#main h1", timeout=15000)
            assert page.evaluate("document.documentElement.classList.contains('inapp')"), "remembered for the tab"
        finally:
            ctx.close()
