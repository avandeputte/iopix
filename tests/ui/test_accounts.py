"""Accounts and the shell: the sign-in dialog, Settings › Suite (proxy sign-in, the sign-in list), Require sign-in, the Content Security Policy, the
service worker and a lost connection."""
from helpers import make_admin, need_tool

need_tool("playwright", module_level=True)

from ui.conftest import _eventually, _get, _open_photo, _post  # noqa: E402


def test_pwa_service_worker_caches_the_shell(page, server):
    page.goto(server + "/#/photos")
    out = page.evaluate("""async () => { const r = await navigator.serviceWorker.ready; const c = await caches.open('iopix-shell-v5');
        return { state: r.active && r.active.state, cached: (await c.keys()).map((k) => new URL(k.url).pathname) }; }""")
    assert out["state"] in ("activating", "activated")  # ready resolves while the worker may still be activating for a tick
    assert {"/", "/static/app.js", "/static/js/core.js", "/static/js/lightbox.js", "/static/app.css", "/manifest.webmanifest", "/static/icons/icon-192.png"} <= set(out["cached"]), "the shell and every module are cached"
    assert not any(p.startswith("/api") or p.startswith("/thumb") for p in out["cached"]), "photos and the API are never cached"

def test_users_flow_in_the_browser(page, server, browser):
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
    page.goto(server + "/#/settings?tab=suite"); page.wait_for_selector(".iosa .sa-t", timeout=10000)
    assert page.locator(".stab[data-tab=users]").count() == 0, "no Accounts tab: the person in photos is in Settings › Suite › People"
    assert page.locator("#nav a[data-v=maintenance]").is_visible()
    # a visitor in a fresh context: read-only, no maintenance icon, sign-in dialog
    ctx = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 900}); visitor = ctx.new_page()
    pid = _get(server, "/api/photos")["id"][0]
    _open_photo(visitor, server, pid)
    if visitor.locator(".lb-info").is_hidden():
        visitor.locator('[title^="Info"]').click()
    visitor.wait_for_selector(".lb-info h4", timeout=10000)
    assert visitor.locator(".lb-info h4", has_text="People").count() == 1, "the panel is open"
    assert visitor.locator(".lb-info input[placeholder='Title']").count() == 0
    assert not visitor.locator("#nav a[data-v=maintenance]").is_visible()
    assert not visitor.locator("#nav .navmanage").is_visible() and visitor.locator("#nav a[data-v=events]").is_visible()
    visitor.keyboard.press("Escape"); visitor.wait_for_selector("#lightbox", state="hidden", timeout=5000)  # the lightbox covers the top bar
    visitor.locator("#usermenu button").click()
    visitor.wait_for_selector(".modal .account", timeout=5000)
    visitor.locator(".modal .account", has_text="alex").click()  # a protected account: the tile reveals the password field
    visitor.locator(".modal input[type=password]").fill("wrong")
    visitor.locator(".modal button", has_text="Sign in").click(); visitor.wait_for_selector("#toast:not([hidden])", timeout=5000)  # the error is a toast
    assert visitor.locator(".modal").count() == 1, "a wrong password keeps the dialog open"
    visitor.locator(".modal input[type=password]").fill("correct horse"); visitor.locator(".modal button", has_text="Sign in").click()
    visitor.wait_for_selector("#usermenu .avatar", timeout=5000)
    visitor.wait_for_function("import('/static/js/state.js').then((m) => m.S.EDITING)", timeout=10000)  # the sign-in's status poll: an admin edits
    _open_photo(visitor, server, pid)
    if visitor.locator(".lb-info").is_hidden():
        visitor.locator('[title^="Info"]').click()
    visitor.wait_for_selector(".lb-info h4", timeout=10000)
    assert visitor.locator(".lb-info input[placeholder='Title']").count() == 1, "the admin can edit"
    visitor.keyboard.press("Escape"); visitor.wait_for_selector("#lightbox", state="hidden", timeout=5000)
    # a display setting now lives on the server
    visitor.goto(server + "/#/settings"); visitor.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    with visitor.expect_response(lambda r: r.url.endswith("/api/auth/settings") and r.request.method == "POST", timeout=5000):  # saved to the account (core.js flushes 400 ms after the change)
        visitor.locator("input[type=checkbox]").first.uncheck()
    me = visitor.evaluate("async () => (await (await fetch('/api/auth/settings', {method:'POST', headers:{'content-type':'application/json'}, body:'{}'})).json())")
    assert me.get("badges") == "0"
    ctx.close()
    # leave the scratch server open again for the other tests
    _get(server, "/api/status")

def _add_kid(page, server):
    """Settings › Suite › People › Add someone: kid, no password (a one-click viewer)."""
    page.goto(server + "/#/settings?tab=suite&part=people"); page.wait_for_selector(".iosa button:has-text('Add someone')", timeout=10000)
    page.locator(".iosa button", has_text="Add someone").click(); page.wait_for_selector(".iosa-back .sa-dialog", timeout=5000)
    page.locator(".iosa-back label:has-text('User name') input").fill("kid")
    page.locator(".iosa-back button", has_text="Add").click()
    page.wait_for_selector(".iosa td:has-text('One click')", timeout=5000)

def test_account_picker_one_click_sign_in(page, server, browser):
    # an admin and a passwordless viewer, made through the API in open mode
    _post(server, "/api/auth/setup", {"name": "alex", "password": "correct horse"})
    # the admin (a suite admin: setup makes one) adds the passwordless viewer in Settings › Suite › People
    page.goto(server + "/#/settings"); page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    page.locator("#usermenu button").click(); page.wait_for_selector(".modal h3:has-text('Sign in')", timeout=5000)
    page.locator(".modal .account", has_text="alex").click(); page.locator(".modal input[type=password]").fill("correct horse"); page.locator(".modal button", has_text="Sign in").click()
    page.wait_for_selector("#usermenu .avatar", timeout=5000)
    _add_kid(page, server)
    page.goto(server + "/#/settings?tab=suite"); page.wait_for_selector(".iosa .sa-t td:has-text('kid')", timeout=10000)
    # a visitor: two tiles, one click as kid
    ctx = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 900}); v = ctx.new_page()
    v.goto(server + "/#/photos"); v.wait_for_selector(".tile img", timeout=15000)
    v.locator("#usermenu button").click(); v.wait_for_selector(".modal .account", timeout=5000)
    names = v.locator(".modal .account .nm").all_inner_texts()
    assert names == ["alex", "kid", "Other"]
    assert v.locator(".modal input[type=password]").is_hidden(), "no password field until a protected tile is picked"
    v.locator(".modal .account", has_text="kid").click()
    v.wait_for_selector("#usermenu .avatar", timeout=5000)
    assert v.evaluate("async () => (await (await fetch('/api/auth/me')).json()).user.name") == "kid"
    ctx.close()

def test_require_sign_in_shows_only_the_login_dialog(page, server, browser):
    """With Require sign-in on, a visitor loading the app sees the sign-in dialog and nothing of
    the library; signing in from it loads the library in place."""
    make_admin(page, "alex", "correct horse")
    assert page.request.post(server + "/api/settings", data={"require_login": True}).ok
    try:
        ctx = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 900}); visitor = ctx.new_page()
        visitor.goto(server + "/#/photos")
        visitor.wait_for_selector(".modal-back.required .modal h3:has-text('Sign in')", timeout=10000)
        visitor.wait_for_timeout(800)  # a fixed wait on purpose: nothing of the library must load behind the dialog, and nothing marks a load that does not happen
        assert visitor.locator(".tile").count() == 0 and visitor.locator("#main").is_hidden(), "nothing of the library before sign-in"
        assert visitor.locator(".modal button", has_text="Cancel").count() == 0
        visitor.mouse.click(5, 5)  # the backdrop does not dismiss it
        assert visitor.locator(".modal-back.required").count() == 1
        visitor.locator(".modal .account", has_text="alex").click()
        visitor.locator(".modal input[type=password]").fill("correct horse")
        visitor.locator(".modal button", has_text="Sign in").click()
        visitor.wait_for_selector(".tile img", timeout=15000)
        assert visitor.locator(".modal-back").count() == 0 and visitor.locator("#main").is_visible()
        # the setting is under Settings › Suite › Apps (Sign-in required), for every app
        visitor.goto(server + "/#/settings?tab=suite&part=apps"); visitor.wait_for_selector(".iosa h2:has-text('Apps')", timeout=10000)
        assert visitor.locator(".iosa tr:has-text('iopix') input[type=checkbox]").is_checked()
        ctx.close()
    finally:
        assert page.request.post(server + "/api/settings", data={"require_login": False}).ok

def test_proxy_sign_in_settings(page, server):
    """Settings › Suite › Sign-in at the proxy: picking Cloudflare shows the team and AUD fields, saving them turns
    verification on, and Off puts it back."""
    make_admin(page, "alex", "correct horse")
    try:
        page.goto(server + "/#/settings?tab=suite&part=signin")
        page.wait_for_selector(".iosa h2:has-text('Sign-in at the proxy')", timeout=10000)
        box = page.locator(".iosa .sa-box")
        assert box.locator("input").first.is_hidden(), "Off: nothing else to fill in"
        box.locator("select").first.select_option("cloudflare")
        box.locator("input[placeholder='myteam.cloudflareaccess.com']").fill("myteam.cloudflareaccess.com")
        box.locator("textarea").fill("aud-tag")
        box.locator("button", has_text="Save for every app").click()
        page.wait_for_selector(".iosa b:has-text('Cloudflare Access tokens are verified')", timeout=10000)
        d = page.request.get(server + "/api/settings/proxy").json()
        assert d["mode"] == "cloudflare" and d["team"] == "myteam.cloudflareaccess.com" and d["aud"] == "aud-tag"
        assert page.request.post(server + "/api/settings/proxy", data={"CF_ACCESS_AUD": ""}).status == 400, "team without AUD refused"
    finally:
        page.request.post(server + "/api/settings/proxy", data={"AUTH_PROXY": "off", "CF_ACCESS_TEAM": "", "CF_ACCESS_AUD": ""})
    assert page.request.get(server + "/api/settings/proxy").json()["mode"] == "off"

def test_csp_lets_the_app_and_the_vendored_map_run(browser, server):
    """The page carries a Content Security Policy: the inline theme script is hashed, Leaflet comes from /vendor (and finds
    its marker images there), and nothing the pages do is refused. Its own context: the shared fixture bypasses the policy."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors, blocked, vendor = [], [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: blocked.append(m.text) if "Content Security Policy" in m.text else None)
    page.on("response", lambda r: vendor.append((r.url, r.status)) if "/vendor/" in r.url else None)
    try:
        page.goto(server + "/#/explore")
        page.wait_for_selector("#main h1", timeout=15000)
        assert page.locator("meta[http-equiv='Content-Security-Policy']").count() == 1
        assert page.evaluate("document.documentElement.dataset.theme") == "light", "the inline theme script ran (its hash is in the policy)"
        page.goto(server + "/#/map")
        page.wait_for_selector(".leaflet-container", timeout=15000)
        assert page.evaluate("window.L.version") == "1.9.4" and page.evaluate("typeof L.markerClusterGroup") == "function"
        assert page.evaluate("new L.Icon.Default()._getIconUrl('icon')").endswith("/static/vendor/leaflet/images/marker-icon.png")
        page.goto(server + "/#/settings")
        page.wait_for_selector("#main h1", timeout=15000)
        page.wait_for_timeout(500)  # a fixed wait on purpose: a policy report would come after the page is up, and nothing marks a report that does not come
        assert not blocked, blocked
        assert vendor and all(st == 200 for _, st in vendor), vendor
        assert not errors, errors
    finally:
        ctx.close()

def test_an_account_can_be_taken_off_the_sign_in_list(page, server, browser):
    """Settings › Suite › People › On the sign-in list: unticked, the account is not a tile on the sign-in screen; "Other" with the
    name and password still signs it in."""
    _post(server, "/api/auth/setup", {"name": "alex", "password": "correct horse"})
    page.goto(server + "/#/settings"); page.wait_for_selector("h1:has-text('Settings')", timeout=10000)
    page.locator("#usermenu button").click(); page.wait_for_selector(".modal h3:has-text('Sign in')", timeout=5000)
    page.locator(".modal .account", has_text="alex").click(); page.locator(".modal input[type=password]").fill("correct horse"); page.locator(".modal button", has_text="Sign in").click()
    page.wait_for_selector("#usermenu .avatar", timeout=5000)
    _add_kid(page, server)
    page.locator(".iosa tr:has-text('alex') button", has_text="Manage").click(); page.wait_for_selector(".iosa-back .sa-dialog", timeout=5000)
    box = page.locator(".iosa-back label:has-text('On the sign-in list') input")
    assert box.is_checked(), "every account starts on the list"
    box.uncheck()
    with page.expect_response(lambda r: "/api/suite-admin/users/" in r.url and r.request.method == "PATCH", timeout=5000):
        page.locator(".iosa-back button", has_text="Save").click()
    assert _eventually(lambda: [a["name"] for a in _get(server, "/api/auth/accounts")["accounts"]] == ["kid"]), "the sign-in list no longer offers alex"
    ctx = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 900}); v = ctx.new_page()
    v.goto(server + "/#/photos"); v.wait_for_selector(".tile img", timeout=15000)
    v.locator("#usermenu button").click(); v.wait_for_selector(".modal .account", timeout=5000)
    assert v.locator(".modal .account .nm").all_inner_texts() == ["kid", "Other"], "alex is not offered"
    v.locator(".modal .account", has_text="Other").click()
    v.locator(".modal input[placeholder='User name']").fill("alex"); v.locator(".modal input[type=password]").fill("correct horse"); v.locator(".modal button", has_text="Sign in").click()
    v.wait_for_selector("#usermenu .avatar", timeout=5000)
    assert v.evaluate("async () => (await (await fetch('/api/auth/me')).json()).user.name") == "alex"
    ctx.close()

def test_a_lost_connection_is_retried_and_the_page_recovers(page, server):
    # leaving home: Wi-Fi drops, requests fail with "Load failed"; reads try again by themselves
    page.goto(server + "/#/explore")
    page.wait_for_selector("#nav > a", timeout=10000)
    fails = {"n": 0}
    def flaky(route):
        if fails["n"] < 2:
            fails["n"] += 1
            return route.abort("internetdisconnected")
        return route.continue_()
    page.route("**/api/photos?*", flaky)
    page.goto(server + "/#/photos")
    page.wait_for_selector(".tile, #grid .tile, [data-i]", timeout=15000)
    assert fails["n"] == 2
    assert "No connection" not in page.locator("#main").text_content()
    page.unroute("**/api/photos?*")
    # down for longer than the retries: the page says so, and draws itself again once the server answers
    page.route("**/api/**", lambda r: r.abort("internetdisconnected"))
    page.goto(server + "/#/folders")
    page.wait_for_function("document.querySelector('#main').textContent.includes('No connection to the server')", timeout=20000)
    page.unroute("**/api/**")
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_function("!document.querySelector('#main').textContent.includes('No connection to the server')", timeout=20000)
