/** Startup: metadata, state, the first route. */
import { $, NET, NO_CONNECTION, api, fill, fmtWhen, h, healSoon, icon, main, nf, norm, plural, post, sleep, store } from './core.js';
import { FILTER_VIEWS, NAV, S, ST, applyAppName, applyBadgePref, applyTheme, loadState, onRoute, parseHash, pollSoon, toast, toastError } from './state.js';
import { initSearch } from './search.js';
import { confirmDialog } from './actions.js';
import { G } from './grid.js';
import { dbDialog } from './editing.js';
import { loadUser, loginDialog, moreSheet, renderUserMenu, showAlbums } from './pages.js';
import { M } from './map.js';
import { toggleFilters } from './filters.js';
import { LB, details } from './lightbox.js';

export function indexMeta(meta) {
  S.META = meta;
  S.tagById = new Map(meta.tags.map((t) => [t.id, t]));
  S.albumById = new Map(meta.albums.map((a) => [a.id, a]));
  for (const t of meta.tags) { t._n = norm(t.full.replaceAll('/', ' ')); if (t.pid != null && S.tagById.has(t.pid)) S.tagById.get(t.pid).hasKids = true; }
  for (const a of meta.albums) a._n = norm(a.path);
  const rows = new Map();
  for (const [city, region, country, n] of meta.places) {
    for (const [key, name, sub] of [['place', city, [region, country].filter(Boolean).join(', ')], ['region', region, country], ['country', country, '']]) {
      if (!name) continue;
      const r = rows.get(key + name) || rows.set(key + name, { key, name, sub, n: 0, _n: norm(name) }).get(key + name);
      r.n += n;
    }
  }
  S.placeRows = [...rows.values()];
}
/**
 * After a change to digiKam's database, wait for the server to rebuild its index and load the new metadata.
 * `until` can demand more than a version bump (e.g. that a new tag is present). Returns whether it happened.
 */
export async function awaitReindex(until = () => true, tries = 20, ms = 400) {
  let seen = S.META.version;
  for (let i = 0; i < tries; i++) {
    await sleep(ms);
    try {
      const s = await api('/api/status');
      if (s.version === seen) continue;
      seen = s.version;
      const m = await api('/api/meta');
      if (until(m)) { indexMeta(m); return true; }
    } catch { /* retry */ }
  }
  return false;
}
/** For admins: digiKam's database is unreachable (the viewer works from its copy of the library) or Sync with digiKam is off.
 *  Dismissed for this browser session, per episode. */
export function dkBanner(d) {
  let b = $('#dkbanner');
  const show = d && S.IS_ADMIN && (!d.sync || !d.reachable || d.conflict);
  const key = !show ? null : d.conflict ? 'conflict:' + d.localChanges : d.sync ? 'offline:' + d.since : 'off';
  let dismissed = null; try { dismissed = sessionStorage.getItem('iopix.digikamBannerDismissed'); } catch { /* private mode */ }
  if (!show || dismissed === key) { b?.remove(); return; }
  if (b && b.dataset.key === key) return;
  b?.remove();
  const asOf = d.mirror?.taken ? fmtWhen(d.mirror.taken) : 'the last read';
  const text = d.conflict
    ? ['Changes made while sync was off', `${plural(d.localChanges, 'change')} made with Sync with digiKam off ${d.localChanges === 1 ? 'is' : 'are'} not in digiKam yet, so ${S.APP_NAME} keeps using its own library. Settings › Sync with digiKam merges them into digiKam, or replaces them with digiKam’s library.`]
    : !d.sync
    ? ['Sync with digiKam is off', `The library lives in ${S.APP_NAME}’s own tables and everything works there; digiKam’s database is not opened.`]
    : ['digiKam’s database is unreachable', `Since ${fmtWhen(d.since)}. Showing the library as of ${asOf}; edits are saved to the sidecars and will be applied to digiKam when it is back.`];
  b = h('div', { id: 'dkbanner', class: 'sysbanner', 'data-key': key },
    h('span', { class: 'ic-wrap', style: 'background:var(--c-orange)' }, icon('wrench')),
    h('div', null, h('b', null, text[0]), h('div', { class: 'muted' }, text[1], ' ', h('a', { href: '#/settings', class: 'lnk' }, 'Settings'))),
    h('button', { class: 'iconbtn', title: 'Hide for now', onclick: () => { try { sessionStorage.setItem('iopix.digikamBannerDismissed', key); } catch { /* private mode */ } b.remove(); } }, icon('close')));
  document.body.append(b);
}
/** The "a newer version is out" banner, for admins, when the daily check found one; dismissed per version in this browser. */
export function updateBanner(u) {
  let b = $('#updbanner');
  let gone = '';
  try { gone = localStorage.getItem('iopix.updateDismissed') || ''; } catch { /* private mode */ }
  if (!u || gone === u.remote) { b?.remove(); return; }
  if (b && b.dataset.remote === u.remote) return;
  b?.remove();
  const text = h('div', null, h('b', null, u.version ? `Version ${u.version} is available` : 'A newer build is available'),
    h('div', { class: 'muted' }, `You have v${S.APP_VERSION || '?'}. ${u.version ? '' : 'Latest change: '}${(u.message || '').replace(/^v\d+\.\d+\.\d+:\s*/, '').slice(0, 140)}`));
  // the update runs from here; the banner then says so and the page reloads with the new version (details and Docker's way: Maintenance)
  const run = h('button', { class: 'pillbtn on', onclick: async () => {
    if (!await confirmDialog(`Fetch ${u.version ? 'version ' + u.version : 'the latest version'}, refresh the packages and restart ${S.APP_NAME}? The page reloads when the new version is up.`)) return;
    runUpdate((msg, failed) => {
      run.hidden = more.hidden = true;
      fill(text, h('b', null, failed ? 'The update did not finish' : 'Updating…'), h('div', { class: 'muted' }, msg, failed ? [' ', h('a', { class: 'lnk', href: '#/maintenance?tab=viewer', onclick: () => b.remove() }, `Maintenance › ${S.APP_NAME} version`)] : null));
    });
  } }, 'Update now');
  const more = h('a', { class: 'pillbtn', href: '#/maintenance?tab=viewer', onclick: () => b.remove() }, 'Details');
  b = h('div', { id: 'updbanner', 'data-remote': u.remote }, h('span', { class: 'ic-wrap' }, icon('spark')), text, run, more,
    h('button', { class: 'iconbtn', title: 'Not now (hidden until the next version)', onclick: () => { try { localStorage.setItem('iopix.updateDismissed', u.remote); } catch { /* ignore */ } b.remove(); } }, icon('close')));
  document.body.append(b);
}
/** Run update.sh on the server (POST /api/update run), then watch for the new version: /api/status every 5 s until its
 *  appVersion or commit differs from what runs here, and the page reloads. After 3 minutes without a change the update
 *  is reported as not finished. onState(text, failed) gets the words for the screen. Returns whether the update started. */
export async function runUpdate(onState) {
  try { await post('/api/update', { action: 'run' }); } catch (e) { toastError(e, 10000); return false; }
  onState('Updating… the page reloads when the new version is up.', false);
  const was = `${S.APP_VERSION}|${S.APP_COMMIT}`, until = Date.now() + 180000;
  const check = async () => {
    try { const s = await api('/api/status'); if (s.appVersion && `${s.appVersion}|${s.commit || ''}` !== was) { location.reload(); return; } } catch { /* restarting */ }
    if (Date.now() < until) setTimeout(check, 5000); else onState(`The update did not finish; see Maintenance › ${S.APP_NAME} version.`, true);
  };
  setTimeout(check, 5000);
  return true;
}
const LIVE_VIEWS = ['photos', 'people', 'places', 'folders', 'albums', 'tags', 'duplicates', 'detected', 'bursts', 'events', 'favorites'];
/** A list page the library changed under is drawn again in place, keeping what was typed in its filter box. Not while
 *  the user is typing in the page or arranging People: then it waits for the next poll. */
function redrawLive() {
  const r = S.liveRedraw;
  if (S.viewKey !== r.key) { S.liveRedraw = null; return; }  // drawn again since (a change of page): nothing stale left
  const a = document.activeElement;
  if (S.arrangePeople || (a && main.contains(a) && a.matches('input, textarea'))) return;
  S.liveRedraw = null;
  const pf = main.querySelector('.pagefilter'), kept = pf && pf.value ? { value: pf.value, start: pf.selectionStart, end: pf.selectionEnd } : null;
  const quiet = r.quiet || S.keepView;
  S.keepView = true; S.viewKey = null;
  onRoute().then(() => {
    const el = main.querySelector('.pagefilter');
    if (!kept || !el || el.value) return;
    el.value = kept.value; el.dispatchEvent(new Event('input'));
    try { el.setSelectionRange(kept.start, kept.end); } catch { /* not a text field */ }
  });
  if (!quiet && S.route.view === 'photos') toast('Library updated from digiKam.');
}
/** A new version of the viewer is on the server: a small note to load it now; it also loads at the next change of page. */
function updateReadyNote() {
  if ($('#reloadnote')) return;
  document.body.append(h('div', { id: 'reloadnote' }, h('span', null, `A new version of ${S.APP_NAME} is ready.`),
    h('button', { class: 'pillbtn on', onclick: () => location.reload() }, 'Load it now'),
    h('button', { class: 'iconbtn', title: 'Later: it loads when you next change page', onclick: (e) => e.currentTarget.parentElement.remove() }, icon('close'))));
}
export async function pollStatus() {
  let delay = 20000;
  try {
    const s = await api('/api/status');
    if (NET.lost) reconnected();
    if (s.requireLogin && !s.user && S.USER) { location.reload(); return; }  // the session ended: the sign-in screen, not a dead page
    const el = $('#status');
    const progress = (label, p, ms) => { el.replaceChildren(label, h('br'), `${nf.format(p.done)} / ${nf.format(p.total)}`, h('div', { class: 'bar' }, h('i', { style: `width:${(100 * p.done / p.total).toFixed(1)}%` }))); delay = ms; };
    el.replaceChildren();
    if (s.thumbs.running && s.thumbs.total) progress('Preparing thumbnails', s.thumbs, 3000);
    else if (s.semantic.running && s.semantic.total) progress('Learning photo content', s.semantic, 4000);
    const an = s.analysis || {};
    for (const [key, label] of [['quality', 'Scoring sharpness'], ['vision', 'Reading text in photos'], ['objects', 'Finding objects'], ['captions', 'Describing photos']]) {
      if (an[key]?.running && an[key].total && !el.childNodes.length) progress(label, an[key], 5000);
    }
    if (s.tagJobs.pending) {
      el.replaceChildren('Updating tag in sidecars', h('br'), `${nf.format(s.tagJobs.pending)} left`);
      delay = 3000;
    }
    // the viewer was updated: the new version loads when it cannot interrupt anything (the tab is in the
    // background, or at the next change of page); meanwhile a quiet note offers to load it now
    if (s.app && S.APP_STAMP && s.app !== S.APP_STAMP) {
      if (document.hidden && !LB.open && !G.sel.size) { location.reload(); return; }
      if (!S.RELOAD_PENDING) { S.RELOAD_PENDING = true; updateReadyNote(); }
    }
    if (s.app && !S.RELOAD_PENDING) S.APP_STAMP = s.app;
    S.SEM = s.semantic; S.EDITING = s.editing; S.FACE_DETECT = !!s.faceDetect; S.LOSSLESS = s.lossless || null; S.SYNC_OFF = !!(s.digikam && !s.digikam.sync); S.PENDING = s.pendingEdits; S.WRITE_FILES = s.writeFiles; S.CAN_MANAGE = s.editing && s.dbWrite;
    S.AUTO_DB = !!(s.dbWrite && s.digikam && s.digikam.sync && s.digikam.reachable);  // edits go into digiKam's database by themselves, a minute later
    S.IS_ADMIN = s.isAdmin; S.USERS_EXIST = s.usersExist; S.DIGIKAM_CHECK = s.digikamCheck || 'pgrep';
    if (s.appVersion) { S.APP_VERSION = s.appVersion; S.APP_COMMIT = s.commit || ''; const b = $('.brand'); if (b) b.title = `${S.APP_NAME} v${S.APP_VERSION}`; } if ((s.user && s.user.id) !== (S.USER && S.USER.id)) { S.USER = s.user; renderUserMenu(); }  // before the banner, which names the installed version
    updateBanner(s.updateAvailable);
    dkBanner(s.digikam);
    if (s.appName && s.appName !== S.APP_NAME) { S.APP_NAME = s.appName; applyAppName(); }
    document.body.classList.toggle('noadmin', !S.IS_ADMIN);
    if (S.PENDING && s.dbWrite && !el.childNodes.length) el.replaceChildren(h('button', { type: 'button', class: 'lnk', style: 'cursor:pointer', title: 'Edits saved in sidecars that digiKam’s database does not have yet', onclick: dbDialog },
      `${nf.format(S.PENDING)} ${S.PENDING === 1 ? 'edit' : 'edits'} not in digiKam yet`));
    if (s.rev !== ST.rev && ST.rev >= 0) { const mine = ST.rev; await loadState(); if (mine !== ST.rev && S.route.view === 'albums') showAlbums(); }
    if (S.META && s.version && s.version !== S.META.version) {
      indexMeta(await api('/api/meta'));
      details.clear();
      // pages that are only lists of the library are redrawn in place; pages holding work in progress (the map
      // while placing, forms, dialogs, the edit log's search) keep what is on screen and refresh their data quietly
      if (LB.open) { if (LIVE_VIEWS.includes(S.route.view)) S.viewKey = null; else S.STALE_VIEW = true; }  // a list is drawn again when the viewer closes; a work page (quality, consistency, the edit log) keeps its place
      else if (S.route.view === 'map' && M.update) M.update();
      else if (LIVE_VIEWS.includes(S.route.view)) S.liveRedraw = { key: S.viewKey, quiet: S.keepView || !!S.liveRedraw?.quiet };
      else S.STALE_VIEW = true;
    }
    if (S.liveRedraw) redrawLive();
  } catch { if (NET.lost) delay = 4000; }  // server restarting, or no network: look again soon
  pollSoon(delay);
}
/** The server answers again after a lost connection: what failed for want of it is loaded again. Only a page that
 *  shows the "no connection" message is drawn again (a page in use is left as it is); broken thumbnails reload. */
function reconnected() {
  NET.lost = false;
  if (!S.META) { boot(); return; }  // the library never loaded
  if (main.textContent.includes(NO_CONNECTION)) { S.viewKey = null; onRoute(); }
  document.querySelectorAll('img').forEach((i) => { if (i.complete && !i.naturalWidth && i.src) { const src = i.src; i.src = ''; i.src = src; } });
}
const lookAgain = () => { if (NET.lost) pollSoon(300); };
window.addEventListener('online', lookAgain);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') lookAgain(); });
export async function start() {
  await loadUser();
  $('#nav').replaceChildren(...NAV.map(([v, label, ic, href]) => h('a', { href: href || `#/${v}`, 'data-v': v }, icon(ic), label)),
    h('button', { class: 'navmore', 'data-v': 'more', title: 'More', onclick: moreSheet }, icon('more'), 'More'), h('div', { id: 'status', role: 'status', 'aria-live': 'polite' }));
  initSearch();
  applyBadgePref(); applyTheme();
  if (S.REQUIRE_LOGIN && !S.USER) { document.body.classList.add('locked'); loginDialog(true); return; }  // boot() runs after sign-in
  await boot();
}
/** Load the library and show the first view; once per page load, after sign-in when that is required. */
export async function boot() {
  S.BOOTED = true; document.body.classList.remove('locked');
  // (a search typed before the metadata arrived draws its list once it is here)
  try { indexMeta(await api('/api/meta')); await loadState(); const q = $('#q'); if (q && q.value && document.activeElement === q) q.dispatchEvent(new Event('input')); } catch (e) { main.replaceChildren(h('div', { class: 'empty' }, 'Could not reach the server: ' + e.message)); if (NET.lost) pollSoon(4000); return; }
  if (store.get('filters', '0') === '1' && innerWidth >= 1100 && FILTER_VIEWS.includes(parseHash().view)) toggleFilters(true, false);
  await pollStatus();
  onRoute();
}
healSoon();
start();
