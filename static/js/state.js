/** What the whole app shares: user and settings, the library's metadata, the route and the navigation, status polling. */
import { $, api, fill, h, main, norm, store } from './core.js';
import { crumbs, renderChips } from './search.js';
import { G, removeScrubber, showPhotos } from './grid.js';
import { clearSelection, showHistory, showImport, showQuality, showTrash } from './actions.js';
import { MORE_VIEWS, currentNav, showAlbums, showAnalysis, showBursts, showDetected, showEvents, showExplore, showFolders, showPeople, showPlaces } from './pages.js';
import { showConsistency, showDuplicates, showFaces, showMaintenance, showSettings, showTags } from './admin.js';
import { M, showMap } from './map.js';
import { renderFilters, toggleFilters } from './filters.js';
import { LB, syncLightbox } from './lightbox.js';
import { pollStatus } from './main.js';

/** Mutable state every module reads and writes (one object, so modules can assign it; ES module bindings cannot be assigned from outside). */
export const S = {
  RELOAD_PENDING: false,
  STALE_VIEW: false,
  USER: null,
  META: null,
  tagById: new Map(),
  albumById: new Map(),
  route: { view: 'photos', params: new URLSearchParams() },
  viewKey: null,
  SEM: { enabled: false },
  CONCEPTS: null,
  APP_STAMP: null,
  dragTag: null,
  IS_ADMIN: true,
  USERS_EXIST: false,
  REQUIRE_LOGIN: false,
  BOOTED: false,
  DIGIKAM_CHECK: 'pgrep',
  APP_VERSION: '',
  APP_COMMIT: '',
  APP_NAME: 'iopix',
  LOSSLESS: null,
  EDITING: false,
  WRITE_FILES: false,
  CAN_MANAGE: false,
  FACE_DETECT: false,
  SYNC_OFF: false,
  manageTags: false,
  PENDING: 0,
  pollTimer: 0,
  keepView: false,
  sgItems: [],
  sgSel: 0,
  scrollRaf: 0,
  RECENT_TAGS: null,
  TEMPLATES: null,
  leafletP: null,
  clusterP: null,
  MAPS: { provider: 'osm', url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', attribution: '© OpenStreetMap contributors', maxZoom: 19, link: 'https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=15/{lat}/{lon}', linkLabel: 'OpenStreetMap' },
  mapsLoaded: false,
  facetToken: 0,
  placeRows: [],
};

/** A person's badge picture; the version key busts the cache when digiKam's tag icon changes. */
export const faceUrl = (tagId) => `/face/${tagId}?v=${S.tagById.get(tagId)?.icon || 0}`;
// an account's avatar: the linked person's face badge from People, else the first letter
export const avatarOf = (u, big) => u.person ? h('img', { class: 'avatar' + (big ? ' big' : ''), src: faceUrl(u.person), alt: '' }) : h('span', { class: 'avatar' + (big ? ' big' : '') }, u.name[0].toUpperCase());
export function toast(msg, action, fn, ms = 6000) {
  const t = $('#toast');
  fill(t, msg, action ? h('a', { onclick: () => { t.hidden = true; fn(); } }, action) : null);
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { t.hidden = true; }, ms);
}


export const FILTER_VIEWS = ['photos', 'map'];
export const NAV = [
  ['explore', 'Explore', 'explore'], ['photos', 'Photos', 'photo'], ['favorites', 'Favorites', 'heartO', '#/photos?fav=1'],
  ['people', 'People', 'person'], ['places', 'Places', 'place'], ['folders', 'Folders', 'folder'], ['albums', 'Albums', 'album'], ['tags', 'Tags', 'tag'], ['map', 'Map', 'map'],
];
// hash S.route name -> page renderer (function declarations are hoisted, so this can sit above them)
export const VIEW_FNS = { photos: showPhotos, explore: showExplore, people: showPeople, places: showPlaces, folders: showFolders, albums: showAlbums, tags: showTags, map: showMap, duplicates: showDuplicates,
  detected: showDetected, bursts: showBursts, events: showEvents, history: showHistory, analysis: showAnalysis, settings: showSettings, maintenance: showMaintenance, consistency: showConsistency, quality: showQuality, trash: showTrash, import: showImport, faces: showFaces };
export const VIEWS = Object.keys(VIEW_FNS);
export const PARAM_VIEWS = ['places', 'bursts', 'faces', 'history', 'import'];  // views whose own query parameters (not photo filters) pick what they show
// favorites and app albums (stored by the app, not in digiKam)
export const ST = { rev: -1, fav: new Set(), albums: [], searches: [] };


export function loadConcepts() { if (S.CONCEPTS || !S.SEM.enabled) return; S.CONCEPTS = []; api('/api/concepts').then((c) => { S.CONCEPTS = c.concepts.map((x) => ({ ...x, _n: norm(x.name) })); }).catch(() => { S.CONCEPTS = null; }); }

export const applyBadgePref = () => document.body.classList.toggle('nobadges', store.get('badges', '1') === '0');
// The photo info panel's sections, each one a user can hide (Settings › Display › Info panel, or the gear in the
// panel). What is stored is the hidden set, so a section added later shows by default.
export const INFO_SECTIONS = [
  ['caption', 'Title and caption'], ['date', 'Date and time'], ['file', 'File name, size and folder'], ['people', 'People'], ['tags', 'Tags'],
  ['location', 'Location and map'], ['details', 'Details: rating, creator, copyright, video'], ['camera', 'Camera, lens and exposure'],
  ['ai', 'AI analysis: description, detected content, objects, text, sharpness'], ['history', 'Edit log: every change made here, with undo'],
];
export const infoHidden = () => new Set(store.get('info_hide', '').split(',').filter(Boolean));
export function infoPrefsEditor(onchange) {
  const hid = infoHidden();
  return h('div', { class: 'iprefs' }, INFO_SECTIONS.map(([k, label]) => h('label', null, h('input', { type: 'checkbox', checked: !hid.has(k), onchange: (e) => {
    const s = infoHidden(); if (e.target.checked) s.delete(k); else s.add(k); store.set('info_hide', [...s].join(',')); onchange?.(); } }), label)));
}
/** Appearance: light, dark, or follow the system (the default). Set on <html> so the stylesheet
 *  picks the palette; the browser chrome (theme-color) follows the page background. */
export const THEMES = [['light', 'Light'], ['dark', 'Dark'], ['system', 'System']];  // light unless chosen otherwise
export function applyTheme() {
  const t = store.get('theme', 'light');
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  requestAnimationFrame(() => { const m = document.querySelector('meta[name=theme-color]'); if (m) m.content = getComputedStyle(document.body).backgroundColor; });
}


export function applyAppName() {
  const b = $('.brand-name'); if (b) b.textContent = S.APP_NAME;
  document.title = S.APP_NAME;
  const meta = document.querySelector('meta[name="apple-mobile-web-app-title"]'); if (meta) meta.content = S.APP_NAME;
}


/** Re-check server status soon (after an edit) instead of waiting for the regular poll. */
export function pollSoon(ms = 1500) { clearTimeout(S.pollTimer); S.pollTimer = setTimeout(pollStatus, ms); }
export async function loadState() {
  const s = await api('/api/state');
  ST.rev = s.rev; ST.fav = new Set(s.favorites); ST.albums = s.albums; ST.searches = s.searches || []; ST.canShare = !!s.canShareAlbums; ST.canPrivate = !!s.canPrivateAlbums;
}
export const ualbumById = (id) => ST.albums.find((a) => a.id === +id);

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs = ''] = raw.split('?');
  return { view: VIEWS.includes(path) ? path : 'explore', params: new URLSearchParams(qs) };  // Explore is the home page
}
export function hashFor(view, params) {
  const qs = params ? params.toString() : '';
  return `#/${view}${qs ? '?' + qs : ''}`;
}
export function go(view, params, replace = false) {
  history[replace ? 'replaceState' : 'pushState'](null, '', hashFor(view, params));
  onRoute();
}
export function filterParams() {
  const p = new URLSearchParams(FILTER_VIEWS.includes(S.route.view) ? S.route.params : '');
  p.delete('p');
  return p;
}
/** The smart album whose query is exactly this filter set (sort ignored), if any. */
export function savedSearchFor(p) {
  const q = new URLSearchParams(p); q.delete('sort'); q.delete('p');
  const key = [...q.entries()].sort().map((kv) => kv.join('=')).join('&');
  return ST.searches.find((s) => [...new URLSearchParams(s.query).entries()].sort().map((kv) => kv.join('=')).join('&') === key) || null;
}
/** Change the active filters, staying on the map if that is where we are. */
export function applyFilter(mutate, view) {
  const p = filterParams();
  mutate(p);
  go(view || (S.route.view === 'map' ? 'map' : 'photos'), p);
}
export function toggle(p, key, value) {
  value = String(value);
  const all = p.getAll(key);
  p.delete(key);
  if (all.includes(value)) all.filter((v) => v !== value).forEach((v) => p.append(key, v));
  else [...all, value].forEach((v) => p.append(key, v));
}
export function setOne(p, key, value) {
  if (value == null || value === '' || p.get(key) === String(value)) p.delete(key);
  else p.set(key, value);
}
export const photosWith = (obj) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) p.append(k, v);
  return p;
};

export function onRoute() {
  if (S.RELOAD_PENDING && !LB.open && !G.sel.size) { location.reload(); return; }  // a new version waits: a change of page is a good moment
  S.route = parseHash();
  const fp = filterParams();
  const vp = new URLSearchParams(S.route.params); vp.delete('p');
  const key = S.route.view + '?' + (PARAM_VIEWS.includes(S.route.view) ? vp : fp).toString();
  const cur = currentNav();
  document.querySelectorAll('#nav a, #topbar a.tool').forEach((a) => a.classList.toggle('on', a.dataset.v === cur));
  $('#nav .navmore')?.classList.toggle('on', MORE_VIEWS.includes(cur));
  renderChips();
  if (key !== S.viewKey) {
    S.viewKey = key;
    if (S.keepView) G.restore = main.scrollTop; else clearSelection(true);
    S.keepView = false;
    main.scrollTop = 0;
    removeScrubber();
    if (M.map && S.route.view !== 'map') { M.map.remove(); M.map = null; }  // Leaflet keeps window listeners until the map is removed
    crumbs(null);  // each view sets its own trail
    // the filter pane belongs to the photo and map views: it steps aside elsewhere and comes back with them
    if (!FILTER_VIEWS.includes(S.route.view)) { if (!$('#filters').hidden) toggleFilters(false, false); }
    else if ($('#filters').hidden && store.get('filters', '0') === '1' && innerWidth >= 1100) toggleFilters(true, false);
    VIEW_FNS[S.route.view](PARAM_VIEWS.includes(S.route.view) ? vp : fp);
    if (!$('#filters').hidden) renderFilters();
  }
  syncLightbox();
}
window.addEventListener('popstate', onRoute);
window.addEventListener('hashchange', onRoute);
