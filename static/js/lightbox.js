/** The photo viewer: navigation, zoom, info panel, faces, objects, sharing, lossless edits, slideshow. */
import { $, api, date, dfDay, dfLong, dfTime, fill, fmtBytes, fmtDist, fmtDur, fmtHeight, h, icon, nf, norm, plural, post, store } from './core.js';
import { S, ST, faceUrl, go, hashFor, infoHidden, infoPrefsEditor, onRoute, parseHash, photosWith, pollSoon, toast, toastError } from './state.js';
import { G, renderGrid, scrollToIndex, setBadges } from './grid.js';
import { albumDialog, clearSelection, histEntry, modal, movePhotos, renamePhoto, setFavorite, trashPhotos } from './actions.js';
import { dateEditor, detectFaces, editControls, editPhotos, locationDialog, metadataEditor, tagDialog, tagPicker, templatePicker } from './editing.js';
import { tagOp } from './admin.js';
import { M, loadLeaflet, loadMaps, mapLink, streetView, tiles } from './map.js';
import { awaitReindex, pollStatus } from './main.js';

// src = the list being browsed ({id, w, h, ts, video}); faces/objects = overlay toggles; pushed = we added a history entry for ?p=
export const LB = { open: false, src: null, i: 0, token: 0, info: store.get('info', '0') === '1', prefs: false, faces: false, objects: false, scale: 1, tx: 0, ty: 0, ar: 1.5, pushed: false, detail: null, play: 0, mini: null, hires: false };
export const details = new Map();  // photo id -> promise of /api/photo/:id, bounded LRU-ish cache
export const NATIVE = new Set(['JPG', 'PNG', 'GIF', 'WEBP']);
// face changes made here in the last minute, per photo: a copy of the photo fetched from the server while the
// index has not caught up yet must not show the old faces for a moment, so they are applied to it again
export const RECENT_FACE_OPS = new Map();  // photo id -> [{op, at}]
const reapplyRecent = (id, d) => {
  const ops = (RECENT_FACE_OPS.get(id) || []).filter((x) => Date.now() - x.at < 60000);
  if (!ops.length) { RECENT_FACE_OPS.delete(id); return d; }
  RECENT_FACE_OPS.set(id, ops);
  if (d && d.faces) ops.forEach((x) => applyFaceLocally(d, x.op));
  return d;
};
export function getDetail(id) {
  if (!details.has(id)) {
    details.set(id, api(`/api/photo/${id}`).then((d) => reapplyRecent(id, d)).catch((e) => { details.delete(id); throw e; }));  // not cached: asked again next time
    if (details.size > 400) details.delete(details.keys().next().value);
  }
  return details.get(id);
}
export function currentSource() { return S.route.view === 'map' ? M.data : S.route.view === 'photos' ? G.data : { id: [] }; }
export function syncLightbox() {
  const p = +S.route.params.get('p');
  if (!p) { if (LB.open) closeLightbox(false); return; }
  if (LB.open && LB.src.id[LB.i] === p) return;
  const src = currentSource();
  if (!src) return; // still loading; called again when data arrives
  const idx = src.id.indexOf(p);
  if (idx >= 0) openLightbox(src, idx, false); else openLightbox({ id: [p] }, 0, false);
}
export function setPhotoParam(id, push) {
  const p = new URLSearchParams(S.route.params);
  p.set('p', id);
  history[push ? 'pushState' : 'replaceState'](null, '', hashFor(S.route.view, p));
  S.route = parseHash(); S.lastHash = location.hash;  // drawn already: Back from here is a change (state.js onNav)
}

export function buildLightbox() {
  const lb = $('#lightbox');
  const btn = (name, title, fn, cls = '') => h('button', { class: 'iconbtn ' + cls, title, 'aria-label': title, onclick: (e) => { e.stopPropagation(); fn(); } }, icon(name));
  LB.el = {
    frame: h('div', { class: 'lb-frame' }), ttl: h('div', { class: 'ttl' }),
    info: h('aside', { class: 'lb-info', hidden: !LB.info }),
    favBtn: btn('heartO', 'Favorite (L)', () => setFavorite([LB.src.id[LB.i]], !ST.fav.has(LB.src.id[LB.i]))),
    albumBtn: btn('albumAdd', 'Add to album', () => albumDialog([LB.src.id[LB.i]])),
    editBtn: btn('rotate', 'Rotate, flip or crop without loss ([ and ] rotate)', () => editMenu([LB.src.id[LB.i]], LB.el.editBtn, true)),
    delBtn: btn('trash', 'Move to trash (Delete key)', () => deleteCurrent()),
    playBtn: btn('play', 'Slideshow (space)', toggleSlideshow), facesBtn: btn('face', 'Show faces (F)', toggleFaces), addFaceBtn: btn('faceAdd', 'Add a face: drag a rectangle around it (P names the next unnamed face)', startDrawFace), objectsBtn: btn('dup', 'Show detected objects (O)', toggleObjects),
    infoBtn: btn('info', 'Info (I)', toggleInfo, LB.info ? 'on' : ''),
    shareBtn: btn('share', 'Share', shareNow),
    dl: h('a', { class: 'iconbtn', title: 'Download original (D)', download: '' }, icon('download')),
  };
  const e = LB.el;
  e.prev = h('button', { class: 'lb-nav prev', title: 'Previous (←)', onclick: (ev) => { ev.stopPropagation(); step(-1); } }, icon('left'));
  e.next = h('button', { class: 'lb-nav next', title: 'Next (→)', onclick: (ev) => { ev.stopPropagation(); step(1); } }, icon('right'));
  // the film strip: thumbnails of the photos around the current one, along the bottom edge.
  // Like a taskbar set to auto-hide it stays out of the way, slides up when the pointer reaches
  // the bottom edge (or on a tap of its grip on touch screens) and drops again when it leaves.
  e.strip = h('div', { class: 'lb-strip' }, h('div', { class: 'grip', title: 'Film strip', onclick: () => e.strip.classList.toggle('show') }), h('div', { class: 'reel' }));
  e.stage = h('div', { class: 'lb-stage' },
    h('div', { class: 'lb-top' }, btn('close', 'Close (Esc): back to the list', () => closeLightbox(true)), e.ttl, h('span', { class: 'sp' }), e.playBtn, e.facesBtn, e.addFaceBtn, e.objectsBtn, e.shareBtn, e.dl, e.favBtn, e.albumBtn, e.editBtn, e.delBtn, e.infoBtn),
    e.frame, e.prev, e.next, e.strip);
  lb.replaceChildren(e.stage, e.info);
  initGestures(e.stage);
  let idle, stripTimer = 0;
  const holdStrip = () => { clearTimeout(stripTimer); stripTimer = 0; };
  e.stage.addEventListener('pointermove', (ev) => {
    e.stage.classList.remove('lb-idle');
    clearTimeout(idle);
    idle = setTimeout(() => e.stage.classList.add('lb-idle'), 2800);
    if (ev.pointerType !== 'mouse') return;
    const fromBottom = e.stage.getBoundingClientRect().bottom - ev.clientY;
    if (e.strip.classList.contains('show')) {
      // open: it stays while the pointer is on it, and drops once the pointer is clearly above it
      if (!ev.target.closest('.lb-strip') && fromBottom > e.strip.offsetHeight + 8) { holdStrip(); e.strip.classList.remove('show'); }
    } else if (fromBottom < 24 && !LB.detail?.video && !LB.faces) {
      // only the very bottom edge, after a short rest there: a video's controls and faces low in the photo stay
      // reachable (with a video playing or the faces shown, the grip opens it instead)
      if (!stripTimer) stripTimer = setTimeout(() => { stripTimer = 0; e.strip.classList.add('show'); }, 300);
    } else holdStrip();
  });
  e.stage.addEventListener('pointerleave', () => { holdStrip(); e.strip.classList.remove('show'); unpeek(); });
  e.strip.addEventListener('pointerleave', unpeek);
}

/** Share: the device's own share sheet (AirDrop, Messages, Mail and the rest on a Mac or phone)
 *  with the picture attached, as PhotoStructure does. The screen-size JPEG is fetched on the click
 *  (same origin, quick: the click still counts as the gesture the sheet requires), not for every
 *  photo shown. Where the browser has no share sheet (Firefox on the desktop, plain http) the menu
 *  below opens instead. */
export async function shareNow() {
  const anchor = LB.el.shareBtn;
  if (!navigator.share) { shareMenu(anchor); return; }
  const id = LB.src.id[LB.i], d = LB.detail, name = d?.name || `photo-${id}.jpg`;
  const data = { title: name, url: location.href };
  const file = await shareFile(id, d);
  if (!LB.open || LB.src.id[LB.i] !== id) return;  // moved on meanwhile
  if (file && (!navigator.canShare || navigator.canShare({ files: [file] }))) { data.files = [file]; delete data.url; }
  navigator.share(data).catch((e) => {
    if (e.name === 'AbortError') return;  // the sheet was dismissed
    shareMenu(anchor);
  });
}
/** The picture a share attaches (the screen-size JPEG, named after the photo); null for a video, or when it cannot be fetched (the link is shared then). */
async function shareFile(id, d) {
  if (!d || d.video) return null;
  try { const blob = await (await fetch(`/preview/${id}`)).blob(); return new File([blob], d.name.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' }); } catch { return null; }
}

/** The fallback menu: a link to the photo here, the original or a smaller JPEG as a download,
 *  the original in a tab, and an email with the link. Anchored under the share button. */
export function shareMenu(anchor) {
  const open = $('.sharemenu');
  if (open) { open.remove(); return; }
  const id = LB.src.id[LB.i], d = LB.detail, name = d?.name || `photo ${id}`, url = location.href;
  const item = (label, attrs, sub) => h('a', { class: 'fmi', ...attrs }, label, sub ? h('div', { class: 'muted smaller' }, sub) : null);
  const done = (msg) => { menu.remove(); if (msg) toast(msg); };
  const menu = h('div', { class: 'facemenu sharemenu' }, h('div', { class: 'fmh' }, 'Share'),
    item('Copy link', { onclick: async () => { try { await navigator.clipboard.writeText(url); done('Link copied.'); } catch { done('Could not copy; the address bar has the link.'); } } }, `to this photo in ${S.APP_NAME}`),
    item('Download original', { href: `/orig/${id}?dl=1`, download: name, onclick: () => menu.remove() }, d?.bytes ? `${name} · ${fmtBytes(d.bytes)}` : name),
    !d?.video ? item('Download preview', { href: `/preview/${id}?dl=1`, onclick: () => menu.remove() }, 'a smaller JPEG for messages and email') : null,
    item('Open original in a new tab', { href: `/orig/${id}`, target: '_blank', rel: 'noopener', onclick: () => menu.remove() }),
    item('Email a link', { href: `mailto:?subject=${encodeURIComponent(name)}&body=${encodeURIComponent(url)}`, onclick: () => menu.remove() }));
  const { stage } = LB.el;
  stage.append(menu);
  // under the button's right edge; the bar sits at the stage's top-left, so the button's offsets are stage coordinates
  menu.style.top = (anchor.offsetTop + anchor.offsetHeight + 4) + 'px';
  menu.style.left = Math.max(8, Math.min(anchor.offsetLeft + anchor.offsetWidth - menu.offsetWidth, stage.clientWidth - menu.offsetWidth - 8)) + 'px';
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
  setTimeout(() => document.addEventListener('pointerdown', close, true), 0);
}

/** Hovering a strip thumbnail shows that photo over the stage, without leaving the current one;
 *  the thumbnail appears at once and the full preview replaces it when loaded. */
export function peek(id) {
  clearTimeout(LB.peekT);
  LB.peekT = setTimeout(() => {
    if (!LB.open) return;
    const { stage } = LB.el;
    let im = LB.peekEl;
    if (!im) { im = LB.peekEl = h('img', { class: 'lb-peek', alt: '', draggable: 'false' }); stage.append(im); }
    im.dataset.id = id;
    im.src = `/thumb/${id}`;
    const full = new Image();
    full.onload = () => { if (LB.peekEl === im && im.dataset.id === String(id)) im.src = full.src; };
    full.src = `/preview/${id}`;
  }, 90);
}
export function unpeek() {
  clearTimeout(LB.peekT);
  LB.peekEl?.remove(); LB.peekEl = null;
}

/** Rebuild the strip around the current photo when it drifts out of the rendered window, and
 *  keep the current one marked and centered. */
export function renderStrip() {
  const { src, i, el } = LB, reel = el.strip.querySelector('.reel');
  if (!src || src.id.length < 2) { el.strip.hidden = true; return; }
  el.strip.hidden = false;
  const W = 80;  // thumbnails rendered on each side; a window keeps a 30,000-photo grid cheap
  if (LB.stripSrc !== src || LB.stripLo == null || i < LB.stripLo + 15 && LB.stripLo > 0 || i > LB.stripHi - 15 && LB.stripHi < src.id.length - 1) {
    const lo = Math.max(0, i - W), hi = Math.min(src.id.length - 1, i + W);
    LB.stripSrc = src; LB.stripLo = lo; LB.stripHi = hi;
    const kids = [];
    for (let k = lo; k <= hi; k++) {
      const ar = src.w ? Math.max(0.6, Math.min(2.2, src.w[k] / (src.h[k] || 1))) : 1;
      kids.push(h('img', { src: `/thumb/${src.id[k]}`, alt: '', loading: 'lazy', draggable: 'false', 'data-i': k, style: `width:${Math.round(56 * ar)}px`,
        onpointerenter: (ev) => { if (ev.pointerType === 'mouse' && k !== LB.i) peek(src.id[k]); else unpeek(); },
        onclick: () => { unpeek(); if (LB.i === k) return; LB.i = k; setPhotoParam(src.id[k], false); showItem(); } }));
    }
    reel.replaceChildren(...kids);
  }
  for (const im of reel.children) im.classList.toggle('cur', +im.dataset.i === i);
  const cur = reel.querySelector('.cur');
  // scroll the reel itself: scrollIntoView would also scroll the stage to reach a parked strip
  if (cur) reel.scrollTo({ left: cur.offsetLeft - reel.clientWidth / 2 + cur.offsetWidth / 2, behavior: el.strip.classList.contains('show') ? 'smooth' : 'instant' });
}

export function openLightbox(src, i, fromUser) {
  if (!LB.el) buildLightbox();
  const wasOpen = LB.open;
  LB.open = true; LB.src = src; LB.i = i;
  if (!wasOpen) {  // the info panel opens the way it was last left (the account's setting, or this browser's)
    LB.info = store.get('info', '0') === '1'; LB.prefs = false;
    LB.el.info.hidden = !LB.info; LB.el.infoBtn.classList.toggle('on', LB.info);
  }
  $('#lightbox').hidden = false;
  if (fromUser) { setPhotoParam(src.id[i], !wasOpen); if (!wasOpen) LB.pushed = true; }
  showItem();
}
/** Rotate, flip and crop without loss. JPEG pixels are turned by jpegtran (nothing re-encoded); other files by
 *  their orientation flag. Faces turn with the picture. Each edit keeps a copy, so the toast and the change
 *  log offer Undo. Cached pictures of the photo are fetched afresh, so the browser shows the new one. */
export const TRANSFORMS = [['right', 'Rotate right'], ['left', 'Rotate left'], ['180', 'Turn upside down'], ['fliph', 'Flip horizontally'], ['flipv', 'Flip vertically']];
export function editMenu(ids, anchor, withCrop) {
  $('.facemenu')?.remove();
  const item = (label, fn, cls = '') => h('div', { class: 'fmi ' + cls, onclick: (e) => { e.stopPropagation(); menu.remove(); fn(); } }, label);
  const menu = h('div', { class: 'facemenu' }, h('div', { class: 'fmh' }, ids.length > 1 ? `${plural(ids.length, 'photo')}, without loss` : 'Without loss'),
    TRANSFORMS.map(([op, label]) => item(label, () => transformPhotos(ids, op))),
    withCrop ? item('Crop…', () => startCrop()) : null,
    withCrop ? item('Adjust (new version)…', () => startAdjust()) : null,
    h('div', { class: 'fmi muted', style: 'font-size:12px;cursor:default' }, S.LOSSLESS?.jpegtran ? 'JPEG pixels are turned as they are; other files by their orientation flag.' : 'By the orientation flag (jpegtran is not on the server, so JPEG pixels are not rewritten).'));
  const host = LB.open ? LB.el.stage : document.body;
  const sr = LB.open ? host.getBoundingClientRect() : { left: 0, top: 0, width: innerWidth, height: innerHeight }, br = anchor.getBoundingClientRect();
  if (!LB.open) menu.style.position = 'fixed';
  host.append(menu);
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = br.left - sr.left, top = br.bottom - sr.top + 4;
  if (left + mw > sr.width - 8) left = Math.max(8, sr.width - mw - 8);
  if (top + mh > sr.height - 8) top = Math.max(8, br.top - sr.top - mh - 4);
  menu.style.left = left + 'px'; menu.style.top = top + 'px';
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
  setTimeout(() => document.addEventListener('pointerdown', close, true), 0);
}
/** A menu of actions under a button, anywhere on the page (kept inside the window). items: [label, fn, cls] or a
 *  heading string; null entries are skipped. */
export function popMenu(anchor, items) {
  $('.facemenu')?.remove();
  const menu = h('div', { class: 'facemenu', style: 'position:fixed' }, items.filter(Boolean).map((it) => typeof it === 'string' ? h('div', { class: 'fmsep' }, it)
    : h('div', { class: 'fmi ' + (it[2] || ''), onclick: (e) => { e.stopPropagation(); menu.remove(); it[1](e); } }, it[0])));
  document.body.append(menu);
  const br = anchor.getBoundingClientRect(), mw = menu.offsetWidth, mh = Math.min(menu.offsetHeight, innerHeight - 16);
  menu.style.maxHeight = `${innerHeight - 16}px`; menu.style.overflowY = 'auto';
  let left = br.right - mw, top = br.bottom + 4;
  if (left < 8) left = Math.min(br.left, innerWidth - mw - 8);
  if (top + mh > innerHeight - 8) top = Math.max(8, br.top - mh - 4);
  menu.style.left = Math.max(8, left) + 'px'; menu.style.top = top + 'px';
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
  setTimeout(() => document.addEventListener('pointerdown', close, true), 0);
  return menu;
}
/** The info panel's Actions menu: everything that can be done with this photo, in one place. */
export function photoActions(anchor, d) {
  const editing = S.EDITING, manage = S.CAN_MANAGE;
  const dateBox = () => { const box = LB.el.info.querySelector('.dateedit')?.parentElement; if (box) { box.hidden = false; box.scrollIntoView({ block: 'nearest' }); box.querySelector('input')?.focus(); } };
  popMenu(anchor, [
    manage ? (d.video ? 'People' : 'Faces') : null,
    manage && S.FACE_DETECT && !d.video ? ['Detect faces', () => detectFaces([d.id])] : null,
    manage && !d.video ? ['Add a face by hand…', () => startDrawFace()] : null,
    manage && !d.video && faceRegions(d).some((f) => f.kind === 'unknown') ? [`Remove all unnamed face regions (${faceRegions(d).filter((f) => f.kind === 'unknown').length})`, () => removeUnnamedFaces(d)] : null,
    manage ? [d.video ? 'Add a person to this video…' : 'Add a person without a face box…', () => personPicker((t) => faceOp({ op: 'tagonly', image: d.id, tag: t.id }, () => faceOp({ op: 'untag', image: d.id, tag: t.id }), `Added ${t.name}.`), d.id)] : null,
    d.lat != null ? 'Around this photo' : null,
    d.lat != null ? ['Day’s trail on the map', () => { closeLightbox(false); LB.pushed = false; go('map', photosWith({ from: d.day, to: d.day, trail: d.day })); }] : null,
    d.lat != null ? ['Photos nearby', () => filterFromLightbox({ bbox: [d.lat - 0.01, d.lon - 0.015, d.lat + 0.01, d.lon + 0.015].map((x) => x.toFixed(4)).join(',') })] : null,
    editing ? 'Metadata' : null,
    editing ? ['Change the date…', dateBox] : null,
    editing ? [d.lat != null ? 'Change the location…' : 'Set the location…', () => locationDialog([d.id], d.lat != null ? { lat: d.lat, lon: d.lon, alt: d.alt, accuracy: d.extra?.gpsAccuracy } : null)] : null,
    editing ? ['Apply a metadata template…', () => templatePicker([d.id])] : null,
    manage ? ['All metadata…', () => metadataEditor(d)] : null,
    manage ? 'File' : null,
    manage ? ['Rename…', () => renamePhoto(d)] : null,
    manage ? ['Move to a folder…', () => movePhotos([d.id], () => { LB.detail = null; details.delete(d.id); setTimeout(showItem, 1500); })] : null,
    manage && S.LOSSLESS && !d.video ? ['Rotate, flip or crop…', () => editMenu([d.id], anchor, true)] : null,
    manage && d.album && !d.video ? ['Use as folder cover', async () => { try { await post(`/api/folders/${d.album.id}/properties`, { icon: d.id }); toast(`Now the cover of “${d.album.name}” in Folders.`); S.keepView = true; pollSoon(600); } catch (e) { toastError(e); } }] : null,
    'Share',
    ['Add to album…', () => albumDialog([d.id])],
    ['Download the original', () => LB.el.dl.click()],
    editing ? ['Edit log', () => { location.hash = `#/history?photo=${d.id}`; }] : null,
  ]);
}
export async function refreshImages(id) {
  await Promise.all([fetch(`/thumb/${id}`, { cache: 'reload' }), fetch(`/preview/${id}`, { cache: 'reload' })]).catch(() => {});
  document.querySelectorAll(`img[src$="/thumb/${id}"], img[src$="/preview/${id}"]`).forEach((img) => { const u = img.src; img.src = ''; img.src = u; });
}
export async function afterFileChange(id, d) {
  details.delete(id);
  await refreshImages(id);
  if (LB.open) {
    const i = LB.src.id.indexOf(id);
    if (i >= 0 && LB.src.w && d?.upright) { LB.src.w[i] = d.upright[0]; LB.src.h[i] = d.upright[1]; }
    if (i === LB.i) { LB.detail = null; showItem(); }
  }
  S.keepView = true; pollSoon(600);
}
export async function undoLast(id) {
  try {
    const e = (await api(`/api/history?photo=${id}&limit=1`)).entries[0];
    if (!e || e.action !== 'lossless') throw new Error('Nothing to undo');
    await post(`/api/history/${e.id}/restore`, { kind: 'undo' });
    await afterFileChange(id, null); toast('Put back as it was.');
  } catch (err) { toastError(err, 8000); }
}
export async function transformPhotos(ids, op) {
  let r;
  try { r = await post('/api/photos/transform', { ids, op }); } catch (e) { toastError(e); return; }
  for (const d of r.done) await afterFileChange(d.id, d);
  const label = TRANSFORMS.find((t) => t[0] === op)[1].toLowerCase();
  if (r.failed.length) toast(`${r.done.length ? `${plural(r.done.length, 'photo')} ${label.replace('rotate', 'rotated').replace('flip', 'flipped').replace('turn', 'turned')}; ` : ''}${plural(r.failed.length, 'photo')} not: ${r.failed[0].error}`, null, null, 10000);
  else toast(ids.length > 1 ? `${plural(r.done.length, 'photo')}: ${label}.` : `${label[0].toUpperCase()}${label.slice(1)}.`, r.done.length === 1 ? 'Undo' : null, () => undoLast(r.done[0].id));
}
/** Adjust: exposure, contrast, saturation, warmth, straighten and auto levels, previewed live on screen and saved
 *  as a new version next to the original (digiKam's "save as new version"); the original stays as it is. */
/** The one editing mode in progress on the photo shown (adjust, crop, draw a face, move a face). It captures the
 *  photo it started on: stepping to another photo or closing the viewer ends it, so it can never act on the wrong photo. */
function setMode(stop) { endMode(); LB.mode = { stop }; }
export function endMode() { const m = LB.mode; LB.mode = null; if (m) m.stop(); }
function startAdjust() {
  const { stage, frame } = LB.el, d = LB.detail;
  if (!d || d.video) return;
  stage.querySelector('.adjbar')?.remove();
  const img = frame.querySelector('img');
  const v = { exposure: 0, contrast: 0, saturation: 0, warmth: 0, angle: 0, autolevel: false };
  const preview = () => {
    if (!img) return;
    const warm = v.warmth / 100;
    img.style.filter = `brightness(${2 ** v.exposure}) contrast(${1 + v.contrast / 100}) saturate(${1 + v.saturation / 100})${warm > 0 ? ` sepia(${warm * 0.35})` : warm < 0 ? ` hue-rotate(${warm * 12}deg)` : ''}${v.autolevel ? ' contrast(1.08)' : ''}`;
    const a = Math.abs(v.angle) * Math.PI / 180, r = img.naturalWidth / (img.naturalHeight || 1);
    const s = v.angle ? Math.cos(a) + Math.sin(a) * Math.max(r, 1 / r) : 1;
    img.style.transform = v.angle ? `rotate(${v.angle}deg) scale(${s})` : '';
  };
  const slider = (key, label, min, max, step) => {
    const out = h('span', { class: 'adjv' }, '0');
    const inp = h('input', { type: 'range', min, max, step, value: 0, oninput: () => { v[key] = +inp.value; fill(out, (v[key] > 0 ? '+' : '') + v[key] + (key === 'angle' ? '°' : key === 'exposure' ? ' EV' : '')); preview(); } });
    return h('label', { class: 'adjrow' }, h('span', null, label), inp, out);
  };
  const auto = h('input', { type: 'checkbox', onchange: () => { v.autolevel = auto.checked; preview(); } });
  frame.style.overflow = 'hidden';  // the straightened preview shows what the saved crop keeps
  const stop = () => { if (img) { img.style.filter = ''; img.style.transform = ''; } frame.style.overflow = ''; bar.remove(); if (LB.mode?.stop === stop) LB.mode = null; };
  setMode(stop);
  const save = async () => {
    fill(saveBtn, 'Saving…'); saveBtn.disabled = true;
    let r;
    try { r = await post('/api/photos/adjust', { id: d.id, ...v }); } catch (e) { toastError(e); saveBtn.disabled = false; fill(saveBtn, 'Save as new version'); return; }
    stop(); S.keepView = true; pollSoon(600); details.delete(d.id);
    toast(`Saved as ${r.name}, next to the original (which is unchanged).`, 'Show', () => openLightbox({ id: [r.id, d.id] }, 0, true), 10000);
  };
  const saveBtn = h('button', { class: 'pillbtn on', onclick: (e) => { e.stopPropagation(); save(); } }, 'Save as new version');
  const bar = h('div', { class: 'adjbar', onclick: (e) => e.stopPropagation(), onpointerdown: (e) => e.stopPropagation() },
    slider('exposure', 'Exposure', -2, 2, 0.1), slider('contrast', 'Contrast', -60, 60, 1), slider('saturation', 'Saturation', -100, 100, 1),
    slider('warmth', 'Warmth', -100, 100, 1), slider('angle', 'Straighten', -15, 15, 0.5),
    h('label', { class: 'adjrow' }, h('span', null, 'Auto levels'), auto),
    h('div', { class: 'muted smaller' }, 'The preview is approximate; the saved version is rendered at full size.'),
    h('div', { style: 'display:flex;gap:8px;justify-content:flex-end' }, h('button', { class: 'pillbtn', onclick: (e) => { e.stopPropagation(); stop(); } }, 'Cancel'), saveBtn));
  stage.append(bar);
}
/** Crop: drag a rectangle over the photo, adjust it by its corners, then Crop. The dark edge shows what goes. */
export function startCrop() {
  const { stage, frame } = LB.el, d = LB.detail;
  if (!d || d.video || !S.LOSSLESS) return;
  const W = d.w, H = d.h;
  const ext = (d.name || '').split('.').pop().toLowerCase();
  if (S.LOSSLESS.crop && !S.LOSSLESS.crop.includes('.' + ext)) { toast(`Cropping here works for ${S.LOSSLESS.crop.map((x) => x.slice(1).toUpperCase()).join(', ')} files.`, null, null, 8000); return; }
  stage.querySelector('.cropbar')?.remove(); frame.querySelector('.cropbox')?.remove();
  toast('Drag a rectangle over the part to keep.', null, null, 4000);
  let r = null, start = null, drag = null;
  const box = h('div', { class: 'facebox face-adjust cropbox', hidden: true }, ...['nw', 'ne', 'sw', 'se'].map((c) => h('i', { class: 'fh fh-' + c, 'data-c': c })));
  const size = h('span', null, '');
  const bar = h('div', { class: 'cropbar' }, size, h('button', { class: 'pillbtn on', onclick: (e) => { e.stopPropagation(); apply(); } }, icon('crop'), 'Crop'), h('button', { class: 'pillbtn', onclick: (e) => { e.stopPropagation(); stop(); } }, 'Cancel'));
  const place = () => { box.hidden = false; box.style.cssText = `left:${r.x / W * 100}%;top:${r.y / H * 100}%;width:${r.w / W * 100}%;height:${r.h / H * 100}%`; size.textContent = `${Math.round(r.w)} × ${Math.round(r.h)}`; };
  const toImg = (e) => { const b = frame.getBoundingClientRect(); return [Math.min(W, Math.max(0, (e.clientX - b.left) / b.width * W)), Math.min(H, Math.max(0, (e.clientY - b.top) / b.height * H))]; };
  const down = (e) => {
    if (bar.contains(e.target)) return;
    e.preventDefault(); e.stopPropagation(); stage.setPointerCapture(e.pointerId);
    if (e.target.dataset?.c || (r && e.target === box)) { drag = { c: e.target.dataset?.c || 'move', p: toImg(e), r: { ...r } }; return; }
    start = toImg(e); r = null; box.hidden = true;
  };
  const move = (e) => {
    if (drag) {
      const [px, py] = toImg(e), dx = px - drag.p[0], dy = py - drag.p[1], o = drag.r, min = 16;
      let { x, y, w, h: hh } = o;
      if (drag.c === 'move') { x = Math.min(Math.max(0, o.x + dx), W - o.w); y = Math.min(Math.max(0, o.y + dy), H - o.h); }
      else {
        if (drag.c.includes('w')) { x = Math.min(Math.max(0, o.x + dx), o.x + o.w - min); w = o.x + o.w - x; }
        if (drag.c.includes('e')) w = Math.min(Math.max(min, o.w + dx), W - o.x);
        if (drag.c.includes('n')) { y = Math.min(Math.max(0, o.y + dy), o.y + o.h - min); hh = o.y + o.h - y; }
        if (drag.c.includes('s')) hh = Math.min(Math.max(min, o.h + dy), H - o.y);
      }
      r = { x, y, w, h: hh }; place(); return;
    }
    if (!start) return;
    const p = toImg(e);
    r = { x: Math.min(start[0], p[0]), y: Math.min(start[1], p[1]), w: Math.abs(p[0] - start[0]), h: Math.abs(p[1] - start[1]) };
    place();
  };
  const up = () => { if (drag) { drag = null; return; } if (!start) return; start = null; if (!r || r.w < 16 || r.h < 16) { r = null; box.hidden = true; } };
  const stop = () => { stage.removeEventListener('pointerdown', down, true); stage.removeEventListener('pointermove', move, true); stage.removeEventListener('pointerup', up, true); stage.style.cursor = ''; box.remove(); bar.remove(); if (LB.mode?.stop === stop) LB.mode = null; };
  setMode(stop);
  const apply = async () => {
    if (!r || r.w < 16 || r.h < 16) { toast('Drag a rectangle first.'); return; }
    const id = d.id, rect = [Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)];
    stop();
    let res;
    try { res = await post('/api/photos/crop', { id, rect }); } catch (e) { toastError(e); return; }
    await afterFileChange(id, res);
    toast(`Cropped to ${res.w} × ${res.h}${res.how === 're-encoded' ? ' (re-encoded: this format cannot be cut without it)' : ''}${res.faces_dropped ? `; ${plural(res.faces_dropped, 'face')} outside it removed` : ''}.`, 'Undo', () => undoLast(id), 10000);
  };
  stage.style.cursor = 'crosshair';
  frame.append(box); stage.append(bar);
  stage.addEventListener('pointerdown', down, true); stage.addEventListener('pointermove', move, true); stage.addEventListener('pointerup', up, true);
}
/** A source list without item k: the id list and every array alongside it (w, h, ts...), index lists (geo, faces)
 *  and index-keyed maps (video) shifted, so what is shown stays the photo at that position. */
const INDEX_LISTS = new Set(['geo', 'faces']);  // lists of positions into id (every other array runs alongside id)
export function dropIndex(src, k) {
  const out = {};
  for (const [key, v] of Object.entries(src)) {
    if (Array.isArray(v) || ArrayBuffer.isView(v)) {
      if (!INDEX_LISTS.has(key)) out[key] = Array.from(v).filter((_, i) => i !== k);
      else out[key] = Array.from(v).filter((i) => i !== k).map((i) => (i > k ? i - 1 : i));  // a list of indices
    } else if (v instanceof Set) out[key] = new Set([...v].filter((i) => i !== k).map((i) => (i > k ? i - 1 : i)));
    else if (v && typeof v === 'object' && Object.keys(v).every((x) => /^\d+$/.test(x))) {
      out[key] = {};
      for (const [i, x] of Object.entries(v)) { const j = +i; if (j < k) out[key][j] = x; else if (j > k) out[key][j - 1] = x; }
    } else out[key] = v;
  }
  return out;
}
export function deleteCurrent() {
  if (!S.CAN_MANAGE || !LB.open) return;
  const id = LB.src.id[LB.i];
  trashPhotos([id], () => {  // drop it from the sequence and show the next one (or close when it was the last)
    const k = LB.src.id.indexOf(id);
    if (k >= 0) LB.src = dropIndex(LB.src, k);
    if (!LB.src.id.length) { closeLightbox(true); return; }
    LB.i = Math.min(LB.i, LB.src.id.length - 1); setPhotoParam(LB.src.id[LB.i], false); showItem();
  });
}
export function closeLightbox(fromUser) {
  if (!LB.open) return;
  endMode(); stopSlideshow();
  LB.open = false; LB.token++;
  LB.el.frame.replaceChildren();
  unpeek();
  LB.mini?.remove(); LB.mini = null; LB.stripSrc = null;
  $('.facemenu')?.remove();
  $('#lightbox').hidden = true;
  const src = LB.src, i = LB.i;
  // the grid shows the photo just seen; when the library changed meanwhile the grid is drawn again, keeping that photo where it is
  if (src === G.data && S.route.view === 'photos') { scrollToIndex(i); renderGrid(true); if (S.viewKey === null) G.keepAt = i; }
  if (fromUser) {
    if (LB.pushed) { LB.pushed = false; history.back(); }
    else { const p = new URLSearchParams(S.route.params); p.delete('p'); history.replaceState(null, '', hashFor(S.route.view, p)); S.route = parseHash(); S.lastHash = location.hash; if (S.viewKey === null) onRoute(); }
  }
  LB.pushed = false;
}
export function step(d) {
  if (LB.mode) return;  // a crop or face box in progress belongs to this photo: finish or cancel it first
  const n = LB.i + d;
  if (n < 0 || n >= LB.src.id.length) { if (LB.play) stopSlideshow(); return; }
  LB.i = n;
  setPhotoParam(LB.src.id[n], false);
  showItem();
}

export function showItem() {
  const { src, i, el } = LB;
  const id = src.id[i], token = ++LB.token;
  endMode(); unpeek(); $('.sharemenu')?.remove();
  LB.scale = 1; LB.tx = LB.ty = 0; LB.detail = null; LB.hires = false;
  const img = h('img', { src: `/thumb/${id}`, alt: '', draggable: 'false' });
  LB.img = img;
  el.frame.replaceChildren(img);
  if (src.w) LB.ar = src.w[i] / (src.h[i] || 1);
  else img.onload = () => { if (token === LB.token && !LB.detail) { LB.ar = img.naturalWidth / img.naturalHeight; sizeFrame(); } };
  sizeFrame();
  el.ttl.textContent = src.ts ? dfDay.format(date(src.ts[i])) : '';
  // the strip's thumbnails come after the photo and its details, not in competition with them
  setTimeout(() => { if (token === LB.token) renderStrip(); }, LB.stripSrc === src ? 0 : 350);
  updateFavBtn();
  el.dl.href = `/orig/${id}?dl=1`;
  el.prev.style.visibility = i > 0 ? '' : 'hidden';
  el.next.style.visibility = i < src.id.length - 1 ? '' : 'hidden';
  let isVideo = src.video ? i in src.video : undefined;
  const loadPreview = () => {
    const pre = new Image();
    pre.onload = () => { if (token === LB.token && !LB.hires) img.src = pre.src; };
    pre.src = `/preview/${id}`;
  };
  const mountVideo = () => {
    const v = h('video', { src: `/orig/${id}`, poster: `/preview/${id}`, controls: true, autoplay: true, playsinline: true });
    v.onerror = () => { if (token === LB.token) { v.remove(); toast('This video format cannot play in the browser.', 'Download', () => el.dl.click()); } };
    v.onended = () => { if (LB.play) step(1); };
    el.frame.append(v);
  };
  if (isVideo === true) mountVideo(); else if (isVideo === false) loadPreview();
  getDetail(id).then((d) => {
    if (token !== LB.token) return;
    LB.detail = d;
    LB.ar = d.w / (d.h || 1);
    sizeFrame();
    if (isVideo === undefined) { isVideo = !!d.video; d.video ? mountVideo() : loadPreview(); }
    renderTitle();
    el.facesBtn.style.display = d.faces.length || d.faces_other?.length ? '' : 'none';
    el.addFaceBtn.style.display = S.CAN_MANAGE && !d.video ? '' : 'none';
    el.delBtn.style.display = S.CAN_MANAGE ? '' : 'none';
    el.editBtn.style.display = S.CAN_MANAGE && S.LOSSLESS && !d.video ? '' : 'none';
    el.objectsBtn.style.display = d.boxes?.length ? '' : 'none';
    renderFaces();
    if (LB.info) renderInfo();
  }).catch((e) => {
    if (token !== LB.token) return;
    el.frame.replaceChildren(h('div', { class: 'empty error', style: 'color:#fff' }, /404/.test(e.message) ? 'This photo is no longer in the library.' : e.message));
    [el.facesBtn, el.addFaceBtn, el.delBtn, el.editBtn, el.objectsBtn].forEach((b) => { b.style.display = 'none'; });
  });
  // warm the neighbors so stepping feels instant
  for (const j of [i + 1, i - 1, i + 2]) {
    if (j < 0 || j >= src.id.length) continue;
    new Image().src = `/thumb/${src.id[j]}`;
    if (src.video && !(j in src.video) && Math.abs(j - i) === 1) new Image().src = `/preview/${src.id[j]}`;
  }
}
/** The title and date over the photo, from LB.detail: drawn on opening and again after an edit of either. */
export function renderTitle() {
  const d = LB.detail;
  if (d) LB.el.ttl.replaceChildren(d.title || d.name, h('small', null, `${dfDay.format(date(d.ts))} · ${dfTime.format(date(d.ts))}`));
}
export function updateFavBtn() {
  const on = ST.fav.has(LB.src.id[LB.i]);
  LB.el.favBtn.replaceChildren(icon(on ? 'heart' : 'heartO'));
  LB.el.favBtn.classList.toggle('faved', on);
}
export function sizeFrame() {
  const { stage, frame } = LB.el;
  const sw = stage.clientWidth, sh = stage.clientHeight;
  const w = Math.min(sw, sh * LB.ar), hgt = w / LB.ar;
  LB.fw = w; LB.fh = hgt;
  frame.style.width = w + 'px'; frame.style.height = hgt + 'px';
  frame.style.marginLeft = -w / 2 + 'px'; frame.style.marginTop = -hgt / 2 + 'px';
  applyZoom();
}
export function applyZoom() {
  const { stage, frame } = LB.el;
  if (LB.scale <= 1.001) { LB.scale = 1; LB.tx = LB.ty = 0; }
  const mx = Math.max(0, (LB.fw * LB.scale - stage.clientWidth) / 2), my = Math.max(0, (LB.fh * LB.scale - stage.clientHeight) / 2);
  LB.tx = Math.min(mx, Math.max(-mx, LB.tx)); LB.ty = Math.min(my, Math.max(-my, LB.ty));
  frame.style.transform = `translate(${LB.tx}px, ${LB.ty}px) scale(${LB.scale})`;
  stage.style.cursor = LB.scale > 1 ? 'grab' : '';
  // past the preview's resolution, switch to the original file if the browser can show it
  const d = LB.detail;
  if (LB.scale > 1.4 && d && !d.video && !LB.hires && NATIVE.has(d.fmt) && Math.max(d.w, d.h) > 2560) {
    LB.hires = true;
    const token = LB.token, full = new Image();
    full.onload = () => { if (token === LB.token) LB.img.src = full.src; };
    full.src = `/orig/${d.id}`;
  }
}
export function zoomAt(factor, cx, cy) {
  const { stage } = LB.el, r = stage.getBoundingClientRect();
  const s2 = Math.min(10, Math.max(1, LB.scale * factor));
  const dx = cx - r.left - r.width / 2 - LB.tx, dy = cy - r.top - r.height / 2 - LB.ty;
  LB.tx += dx * (1 - s2 / LB.scale); LB.ty += dy * (1 - s2 / LB.scale);
  LB.scale = s2;
  applyZoom();
}
export function initGestures(stage) {
  const pts = new Map();
  let start = null, pinch = 0, moved = false;
  stage.addEventListener('wheel', (e) => {
    if (e.target.closest('.lb-top, .lb-strip')) return;
    e.preventDefault();
    zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0022)), e.clientX, e.clientY);
  }, { passive: false });
  stage.addEventListener('dblclick', (e) => {
    if (e.target.closest('button, a, video')) return;
    LB.scale > 1 ? (LB.scale = 1, applyZoom()) : zoomAt(3, e.clientX, e.clientY);
  });
  stage.addEventListener('pointerdown', (e) => {
    // the face menu lives inside the stage; capturing its pointer would swallow its clicks
    if (e.target.closest('button, a, video, .facebox, .facemenu, .lb-strip')) return;
    pts.set(e.pointerId, e);
    stage.setPointerCapture(e.pointerId);
    moved = false;
    if (pts.size === 1) start = { x: e.clientX, y: e.clientY, tx: LB.tx, ty: LB.ty };
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); }
  });
  stage.addEventListener('pointermove', (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, e);
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      if (pinch) zoomAt(dist / pinch, (a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2);
      pinch = dist; moved = true;
    } else if (start && LB.scale > 1) {
      LB.tx = start.tx + e.clientX - start.x; LB.ty = start.ty + e.clientY - start.y;
      moved = true;
      applyZoom();
    }
  });
  const up = (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (start && pts.size === 0 && LB.scale === 1 && !moved) {
      const dx = e.clientX - start.x, dy = e.clientY - start.y;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
      else if (dy > 90 && Math.abs(dy) > Math.abs(dx) * 1.5 && e.pointerType === 'touch') closeLightbox(true);
    }
    if (pts.size === 0) { start = null; pinch = 0; }
  };
  stage.addEventListener('pointerup', up);
  stage.addEventListener('pointercancel', up);
}
export function toggleInfo() {
  LB.info = !LB.info;
  store.set('info', LB.info ? '1' : '0');
  LB.el.info.hidden = !LB.info;
  LB.el.infoBtn.classList.toggle('on', LB.info);
  if (LB.info) renderInfo();
  sizeFrame();
}
export function toggleObjects() {
  LB.objects = !LB.objects;
  LB.el.objectsBtn.classList.toggle('on', LB.objects);
  renderFaces();
}
export function toggleFaces() {
  LB.faces = !LB.faces;
  LB.el.facesBtn.classList.toggle('on', LB.faces);
  renderFaces();
}
/** Face regions on the photo (confirmed, suggested, unknown), shown when LB.faces is on; ignored ones are never drawn. */
export const faceRegions = (d) => [...d.faces.map((f) => ({ ...f, kind: 'confirmed' })), ...(d.faces_other || [])].filter((f) => f.kind !== 'ignored');
/** Redraw the face and object overlays on the picture from the toggles in LB.faces / LB.objects. */
export function renderFaces() {
  const { frame } = LB.el, d = LB.detail;
  frame.querySelectorAll('.facebox').forEach((x) => x.remove());
  if (!d || d.video) return;
  for (const b of LB.objects ? d.boxes || [] : []) {
    frame.append(h('div', { class: 'facebox objbox', style: `left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%` }, h('span', null, b.cls)));
  }
  const pct = (f) => `left:${(f.x / d.w) * 100}%;top:${(f.y / d.h) * 100}%;width:${(f.w / d.w) * 100}%;height:${(f.h / d.h) * 100}%`;
  for (const f of LB.faces ? faceRegions(d) : []) {
    const label = f.kind === 'confirmed' ? f.name : f.kind === 'unknown' ? 'Unknown' : f.kind === 'suggested' ? `${f.name}?` : 'Ignored';
    frame.append(h('div', { class: 'facebox face-' + f.kind, style: pct(f), title: label + (S.CAN_MANAGE ? ' · click for options' : ''),
      onclick: (e) => { e.stopPropagation(); S.CAN_MANAGE ? faceMenu(e.currentTarget, f, d) : (f.kind === 'confirmed' && filterFromLightbox({ tag: f.tag })); } }, h('span', null, label)));
  }
}
export function faceMenu(box, f, d) {
  $('.facemenu')?.remove();
  const rect = () => [f.x, f.y, f.w, f.h];
  const item = (label, fn, danger) => h('div', { class: 'fmi' + (danger ? ' danger' : ''), onclick: (e) => { e.stopPropagation(); menu.remove(); fn(); } }, label);
  const nameIt = () => personPicker((t) => faceOp({ op: 'name', image: d.id, rect: rect(), tag: t.id }), d.id);
  const items = f.kind === 'confirmed' ? [
    item(`Show all photos of ${f.name}`, () => filterFromLightbox({ tag: f.tag })),
    d.id !== S.tagById.get(f.tag)?.icon ? item(`Use as ${f.name}’s badge picture`, () => setTagIcon(S.tagById.get(f.tag) || { id: f.tag, name: f.name, person: 1 }, d.id)) : null,
    item('This is someone else…', nameIt),
    item('Forget the name (keep as unknown face)', () => faceOp({ op: 'unknown', image: d.id, rect: rect() })),
    item('Not a face to name (ignore)', () => faceOp({ op: 'ignore', image: d.id, rect: rect() })),
    item('Adjust the box (move or resize)…', () => adjustFace(f, d)),
    item('Remove this face region', () => removeFace(f, d), true),
  ] : f.kind === 'suggested' ? [
    item(`Yes, this is ${f.name}`, () => faceOp({ op: 'confirm', image: d.id, rect: rect() })),
    item('No, this is someone else…', nameIt),
    item('Not a face to name (ignore)', () => faceOp({ op: 'ignore', image: d.id, rect: rect() })),
    item('Adjust the box (move or resize)…', () => adjustFace(f, d)),
    item('Remove this region', () => removeFace(f, d), true),
  ] : [
    item('Who is this?…', nameIt),
    f.kind !== 'ignored' ? item('Not a face to name (ignore)', () => faceOp({ op: 'ignore', image: d.id, rect: rect() })) : item('Keep as unknown face', () => faceOp({ op: 'unknown', image: d.id, rect: rect() })),
    item('Adjust the box (move or resize)…', () => adjustFace(f, d)),
    item('Remove this region', () => removeFace(f, d), true),
  ];
  const menu = h('div', { class: 'facemenu' }, h('div', { class: 'fmh' }, f.kind === 'confirmed' ? f.name : f.kind === 'suggested' ? `Suggestion: ${f.name}` : f.kind === 'unknown' ? 'Unknown face' : 'Ignored face'), items);
  const inPanel = !!box.closest('.lb-info');
  // a menu opened from the info panel is fixed to the viewport, but it has to live inside #lightbox
  // (its own stacking context) or the lightbox paints over it
  const stage = inPanel ? $('#lightbox') : LB.el.stage, sr = inPanel ? { left: 0, top: 0, width: innerWidth, height: innerHeight } : stage.getBoundingClientRect(), br = box.getBoundingClientRect();
  if (inPanel) menu.style.position = 'fixed';
  stage.append(menu);
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = br.left - sr.left, top = br.bottom - sr.top + 4;
  if (left + mw > sr.width - 8) left = Math.max(8, sr.width - mw - 8);
  if (top + mh > sr.height - 8) top = Math.max(8, br.top - sr.top - mh - 4);
  menu.style.left = left + 'px'; menu.style.top = top + 'px';
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
  setTimeout(() => document.addEventListener('pointerdown', close, true), 0);
}
/** A face change shows at once: the photo's details are changed here first, the request follows in the
 *  background (one at a time per photo, in order), and only a failure brings the server's version back. */
export const FACE_Q = new Map();  // photo id -> promise of the last change sent for it
/** A person tagged on the photo without a face box: show their photos, mark their face, or take them off the photo. */
export function tagOnlyMenu(anchor, f, d) {
  popMenu(anchor, [
    `${f.name} · no face box`,
    ['Show all photos of ' + f.name, () => filterFromLightbox({ tag: f.tag })],
    !d.video ? [`Mark ${f.name}’s face…`, () => startDrawFace()] : null,
    [`Not in this photo: take ${f.name} off`, () => faceOp({ op: 'untag', image: d.id, tag: f.tag }, () => faceOp({ op: 'tagonly', image: d.id, tag: f.tag }, null, `${f.name} put back.`)), 'danger'],
  ]);
}
export const hasPeople = (d) => !!(d.faces?.length || d.tags?.some((t) => t.person));
export function applyFaceLocally(d, op) {
  const same = (f) => f.x === op.rect?.[0] && f.y === op.rect?.[1] && f.w === op.rect?.[2] && f.h === op.rect?.[3];
  const t = op.tag ? S.tagById.get(op.tag) : null;
  const confirmed = (d.faces || []).find(same), other = (d.faces_other || []).find(same);
  const was = confirmed ? { ...confirmed, kind: 'confirmed' } : other;
  d.faces = (d.faces || []).filter((f) => !same(f));
  d.faces_other = (d.faces_other || []).filter((f) => !same(f));
  const at = (r) => ({ x: r[0], y: r[1], w: r[2], h: r[3] });
  const keepTag = (id, name) => { if (id && !d.tags.some((x) => x.id === id)) { const tt = S.tagById.get(id); d.tags.push({ id, name: name || tt?.name, full: tt?.full || name, person: 1 }); } };
  const dropTag = (id) => { if (id && !d.faces.some((f) => f.tag === id)) d.tags = d.tags.filter((x) => x.id !== id); };
  if (op.op === 'name' || op.op === 'add') { d.faces.push({ tag: op.tag, name: t?.name || '…', ...at(op.rect) }); keepTag(op.tag, t?.name); if (was && was.tag !== op.tag && was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'confirm' && was) { d.faces.push({ tag: was.tag, name: was.name, ...at(op.rect) }); keepTag(was.tag, was.name); }
  else if (op.op === 'ignore' && was) { d.faces_other.push({ ...at(op.rect), kind: 'ignored', tag: null, name: null }); if (was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'unknown' && was) { d.faces_other.push({ ...at(op.rect), kind: 'unknown', tag: null, name: null }); if (was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'remove' && was) { if (was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'detected') d.faces_other.push({ ...at(op.rect), kind: 'unknown', tag: null, name: null });
  else if (op.op === 'suggest') d.faces_other.push({ ...at(op.rect), kind: 'suggested', tag: op.tag, name: t?.name || '…' });
  else if (op.op === 'untag') d.tags = d.tags.filter((x) => x.id !== op.tag);
  else if (op.op === 'tagonly') keepTag(op.tag);
  else if (op.op === 'move' && was) { const moved = { ...was, ...at(op.new_rect) }; if (was.kind === 'confirmed') { delete moved.kind; d.faces.push(moved); } else d.faces_other.push(moved); }
}
export function redrawPhoto(id) {
  if (LB.open && LB.src.id[LB.i] === id && LB.detail) { renderFaces(); if (LB.info) renderInfo(); }
}
/** Remove a face region at once (no question); the message offers Undo, which puts the same region back. */
export function removeFace(f, d) {
  const rect = [f.x, f.y, f.w, f.h];
  const back = f.kind === 'confirmed' ? { op: 'add', tag: f.tag } : f.kind === 'suggested' ? { op: 'suggest', tag: f.tag } : f.kind === 'unknown' ? { op: 'detected' } : null;
  faceOp({ op: 'remove', image: d.id, rect }, back ? () => faceOp({ ...back, image: d.id, rect }) : null);
}
/** Every unnamed (unknown) face region on the photo, removed in one go; Undo puts them all back. */
export function removeUnnamedFaces(d) {
  const rects = faceRegions(d).filter((f) => f.kind === 'unknown').map((f) => [f.x, f.y, f.w, f.h]);
  if (!rects.length) { toast('No unnamed face regions on this photo.'); return; }
  const ops = (op) => rects.map((rect) => ({ op, image: d.id, rect }));
  faceOp(ops('remove'), () => faceOp(ops('detected'), null, `${plural(rects.length, 'unnamed face')} put back.`), `${plural(rects.length, 'unnamed face region')} removed.`);
}
/** One face change, or several on the same photo (an array, sent as one batch): shown at once, sent in order, undone from the message. */
export function faceOp(op, undo, said) {
  const ops = Array.isArray(op) ? op : [op];
  op = ops[0];
  const id = op.image;
  const recs = ops.map((o) => ({ op: o, at: Date.now() }));
  RECENT_FACE_OPS.set(id, [...(RECENT_FACE_OPS.get(id) || []), ...recs]);
  const shown = LB.open && LB.src.id[LB.i] === id ? LB.detail : null, cached = details.get(id);  // the cache holds promises
  if (shown) ops.forEach((o) => applyFaceLocally(shown, o));
  if (cached) cached.then((cd) => { if (cd && cd !== shown && cd.faces) ops.forEach((o) => applyFaceLocally(cd, o)); }).catch(() => {});  // a copy the viewer is not showing (a still pending one gets the change from reapplyRecent)
  redrawPhoto(id);
  // the face badge on its thumbnails follows at once: a named face, or a person tagged without a box
  if (shown) setBadges([id], { faces: hasPeople(shown) });
  else if (cached) cached.then((cd) => { if (cd?.faces) setBadges([id], { faces: hasPeople(cd) }); }).catch(() => {});
  else if (ops.some((o) => ['name', 'add', 'confirm', 'tagonly'].includes(o.op))) setBadges([id], { faces: true });
  else getDetail(id).then((cd) => setBadges([id], { faces: hasPeople(cd) })).catch(() => {});  // a removal: does anyone stay? (the fetched copy gets this change re-applied)
  const name = op.tag ? S.tagById.get(op.tag)?.name : null;
  said = said || { name: `Named ${name}.`, confirm: 'Confirmed.', ignore: 'Face ignored.', unknown: 'Name removed; the face stays as unknown.', remove: 'Face region removed.', add: `Added ${name}.`, move: 'Region moved.', detected: 'Face put back.', suggest: 'Suggestion put back.', untag: `${name} taken off this photo.`, tagonly: `Added ${name}.` }[op.op];
  if (said) undo ? toast(said, 'Undo', undo, 7000) : toast(said);
  const prev = FACE_Q.get(id) || Promise.resolve();
  const send = () => ops.length === 1 ? post('/api/faces/op', op) : post('/api/faces/batch', { ops }).then((r) => { if (r.failed?.length) throw new Error(r.failed[0].error); });
  const mine = prev.then(send).then(() => true, (e) => {
    toast(`That face change did not go through: ${e.message}`, null, null, 9000);
    RECENT_FACE_OPS.set(id, (RECENT_FACE_OPS.get(id) || []).filter((x) => !recs.includes(x)));
    details.delete(id);
    getDetail(id).then((fresh) => { setBadges([id], { faces: hasPeople(fresh) }); if (LB.open && LB.src.id[LB.i] === id) { LB.detail = fresh; redrawPhoto(id); } }).catch(() => {});
    return false;
  });
  FACE_Q.set(id, mine);
  // once the index has it, take the server's version (unless another change for this photo is still on its way)
  mine.then(async (ok) => {
    if (!ok) return;
    await awaitReindex(() => true, 30, 500);
    if (FACE_Q.get(id) !== mine) return;
    FACE_Q.delete(id);
    details.delete(id);
    if (LB.open && LB.src.id[LB.i] === id) getDetail(id).then((fresh) => { if (LB.src.id[LB.i] === id && !FACE_Q.has(id)) { LB.detail = fresh; redrawPhoto(id); } });
  });
}
export function personPicker(onPick, photoId) {
  // the "People" root carries digiKam's person property too, but it is not someone a face can be named after
  // people: person tags, and single tags filed under People that digiKam has not marked as persons yet (naming a face
  // with one marks it), so an existing "People/Belcufine/Nathan Belcufine" is offered instead of a new person being made
  const people = S.META.tags.filter((t) => t.full !== 'People' && t.full.split('/')[0] === 'People' && (t.person || (!t.hasKids && t.full.split('/').length > 2)));
  const inp = h('input', { class: 'pagefilter', style: 'width:100%', placeholder: 'Type a name…' });
  const list = h('div', { class: 'alist', style: 'max-height:40vh;margin-top:8px' });
  const families = S.META.tags.filter((t) => !t.person && t.full.startsWith('People/') && t.full.split('/').length === 2);
  // the people of the nearby photos in the same folder, nearest in time first: most likely the same people
  let near = [];
  if (photoId) api(`/api/faces/nearby?photo=${photoId}`).then((r) => { near = r.people.map((x) => x.tag).filter((id) => S.tagById.has(id)); draw(); }).catch(() => {});
  const draw = () => {
    const f = norm(inp.value);
    const rank = (t) => { const k = near.indexOf(t.id); return k < 0 ? 1e9 : k; };
    const hits = people.filter((t) => !f || t._n.includes(f)).sort((a, b) => (rank(a) - rank(b)) || (b.nAll - a.nAll)).slice(0, 30);
    const nearCount = hits.filter((t) => near.includes(t.id)).length;
    fill(list, nearCount ? h('div', { class: 'muted pphead' }, 'In nearby photos of this folder') : null,
      hits.map((t, k) => [k === nearCount && nearCount ? h('div', { class: 'muted pphead' }, 'Everyone') : null,
        h('div', { class: 'frow' + (k < nearCount ? ' near' : ''), onclick: () => { back.remove(); onPick(t); } }, h('img', { src: faceUrl(t.id), loading: 'lazy', alt: '' }), h('span', { class: 'nm' }, t.name, h('span', { hidden: true }, '  ' + t.full.split('/').slice(1, -1).join(' › '))), h('span', { class: 'ct' }, nf.format(t.nAll)))]),
      f && !hits.some((t) => t._n === f) ? h('div', { class: 'frow', onclick: () => createPerson(inp.value.trim()) }, icon('add'), h('span', { class: 'nm' }, `New person “${inp.value.trim()}”…`)) : null);
  };
  const createPerson = (name) => {
    const fam = h('select', null, h('option', { value: 0 }, 'directly under People'), families.map((t) => h('option', { value: t.id }, t.name)));
    const sub = modal(`New person “${name}”`, h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Family'), fam),
      h('p', { class: 'muted small' }, 'Created in digiKam’s database as a person tag.'),
      h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => sub.remove() }, 'Cancel'), h('button', { class: 'pillbtn on', onclick: async () => {
        let r;
        try { r = await post('/api/faces/person', { name, parent: +fam.value || (S.META.tags.find((t) => t.full === 'People')?.id || 0) }); } catch (e) { toastError(e); return; }
        sub.remove(); back.remove();
        await awaitReindex((m) => m.tags.some((t) => t.id === r.id), 50, 300);
        const t = S.tagById.get(r.id); if (t) onPick(t); else toast('Person created; it will appear shortly.');
      } }, 'Create')));
  };
  inp.oninput = draw;
  inp.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') { const f = norm(inp.value); const t = people.find((x) => x._n === f); if (t) { back.remove(); onPick(t); } } };
  const back = modal('Who is this?', inp, list, h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel')));
  draw(); inp.focus();
}
/** P in the lightbox: name the first face digiKam has not named yet (its picker opens), or draw one when there is none. */
export function nameNextFace() {
  const d = LB.detail;
  if (!d || d.video) return;
  const f = faceRegions(d).find((x) => x.kind === 'unknown' || x.kind === 'suggested');
  if (!f) { startDrawFace(); return; }
  if (!LB.faces) toggleFaces();
  personPicker((t) => faceOp({ op: 'name', image: d.id, rect: [f.x, f.y, f.w, f.h], tag: t.id }), d.id);
}
export function startDrawFace() {
  const { stage, frame } = LB.el, d = LB.detail;
  if (!d || d.video) return;
  if (!LB.faces) toggleFaces();
  toast('Drag a rectangle around the face.', null, null, 4000);
  const box = h('div', { class: 'facebox face-draw' });
  let start = null;
  const toImg = (e) => { const r = frame.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * d.w, Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) * d.h]; };
  const down = (e) => { e.preventDefault(); e.stopPropagation(); start = toImg(e); frame.append(box); stage.setPointerCapture(e.pointerId); };
  const move = (e) => { if (!start) return; const p = toImg(e); const x = Math.min(start[0], p[0]), y = Math.min(start[1], p[1]), w = Math.abs(p[0] - start[0]), hh = Math.abs(p[1] - start[1]); box.style.cssText = `left:${x / d.w * 100}%;top:${y / d.h * 100}%;width:${w / d.w * 100}%;height:${hh / d.h * 100}%`; };
  const up = (e) => {
    if (!start) return;
    const p = toImg(e); const x = Math.round(Math.min(start[0], p[0])), y = Math.round(Math.min(start[1], p[1])), w = Math.round(Math.abs(p[0] - start[0])), hh = Math.round(Math.abs(p[1] - start[1]));
    start = null; stop();
    if (w < 8 || hh < 8) { box.remove(); return; }
    personPicker((t) => { box.remove(); faceOp({ op: 'add', image: d.id, rect: [x, y, w, hh], tag: t.id }); }, d.id);
  };
  const stop = () => { stage.removeEventListener('pointerdown', down, true); stage.removeEventListener('pointermove', move, true); stage.removeEventListener('pointerup', up, true); stage.style.cursor = ''; box.remove(); if (LB.mode?.stop === stop) LB.mode = null; };
  setMode(stop);
  stage.style.cursor = 'crosshair';
  stage.addEventListener('pointerdown', down, true); stage.addEventListener('pointermove', move, true); stage.addEventListener('pointerup', up, true);
}
/** Move or resize a face box: drag inside it to move, drag a corner to resize; it stays inside the photo.
 *  Saved as a move of the region (digiKam's database and the sidecar), with undo in the edit log. */
export function adjustFace(f, d) {
  const { frame } = LB.el;
  if (!d || d.video) return;
  const W = d.w, H = d.h;
  let r = { x: Math.max(0, f.x), y: Math.max(0, f.y), w: Math.min(f.w, W), h: Math.min(f.h, H) };  // starts fitted inside the photo
  r.w = Math.min(r.w, W - r.x); r.h = Math.min(r.h, H - r.y);
  frame.querySelectorAll('.facebox').forEach((x) => { x.style.visibility = 'hidden'; });
  const box = h('div', { class: 'facebox face-adjust' }, ...['nw', 'ne', 'sw', 'se'].map((c) => h('i', { class: 'fh fh-' + c, 'data-c': c })));
  const place = () => { box.style.cssText = `left:${r.x / W * 100}%;top:${r.y / H * 100}%;width:${r.w / W * 100}%;height:${r.h / H * 100}%`; };
  place(); frame.append(box);
  const toImg = (e) => { const b = frame.getBoundingClientRect(); return [(e.clientX - b.left) / b.width * W, (e.clientY - b.top) / b.height * H]; };
  let drag = null;
  box.addEventListener('pointerdown', (e) => { e.preventDefault(); e.stopPropagation(); drag = { c: e.target.dataset.c || 'move', p: toImg(e), r: { ...r } }; box.setPointerCapture(e.pointerId); });
  box.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const [px, py] = toImg(e), dx = px - drag.p[0], dy = py - drag.p[1], o = drag.r, min = 16;
    let { x, y, w, h: hh } = o;
    if (drag.c === 'move') { x = Math.min(Math.max(0, o.x + dx), W - o.w); y = Math.min(Math.max(0, o.y + dy), H - o.h); }
    else {
      if (drag.c.includes('w')) { x = Math.min(Math.max(0, o.x + dx), o.x + o.w - min); w = o.x + o.w - x; }
      if (drag.c.includes('e')) w = Math.min(Math.max(min, o.w + dx), W - o.x);
      if (drag.c.includes('n')) { y = Math.min(Math.max(0, o.y + dy), o.y + o.h - min); hh = o.y + o.h - y; }
      if (drag.c.includes('s')) hh = Math.min(Math.max(min, o.h + dy), H - o.y);
    }
    r = { x, y, w, h: hh }; place();
  });
  box.addEventListener('pointerup', () => { drag = null; });
  const done = () => { bar.remove(); box.remove(); frame.querySelectorAll('.facebox').forEach((x) => { x.style.visibility = ''; }); document.removeEventListener('keydown', esc, true); if (LB.mode?.stop === done) LB.mode = null; };
  setMode(done);
  const esc = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(); } };
  document.addEventListener('keydown', esc, true);
  const bar = h('div', { class: 'adjustbar' }, h('span', null, `Adjusting ${f.name || 'this face'}: drag to move, drag a corner to resize.`),
    h('button', { class: 'pillbtn', onclick: done }, 'Cancel'),
    h('button', { class: 'pillbtn on', onclick: () => {
      const nr = [Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)];
      done();
      if (nr.join() === [f.x, f.y, f.w, f.h].join()) return;
      faceOp({ op: 'move', image: d.id, rect: [f.x, f.y, f.w, f.h], new_rect: nr });
    } }, 'Save'));
  LB.el.stage.append(bar);
}
export async function setTagIcon(t, photoId) {
  await tagOp({ op: 'icon', id: t.id, photo: photoId }, t);
  if (LB.open) { details.delete(LB.src.id[LB.i]); showItem(); }
}
export function toggleSlideshow() { LB.play ? stopSlideshow() : (LB.play = setInterval(() => { if (!LB.el.frame.querySelector('video')) step(1); }, 4000), LB.el.playBtn.replaceChildren(icon('pause'))); }
export function stopSlideshow() { clearInterval(LB.play); LB.play = 0; LB.el?.playBtn.replaceChildren(icon('play')); }
export function filterFromLightbox(obj) {
  closeLightbox(false);
  LB.pushed = false;
  go('photos', photosWith(obj));
}
export function renderInfo() {
  const d = LB.detail, el = LB.el.info;
  if (!d) return;
  LB.mini?.remove(); LB.mini = null;
  const hid = infoHidden(), show = (k) => !hid.has(k);
  const lnk = (text, obj) => h('button', { type: 'button', class: 'lnk', onclick: () => filterFromLightbox(obj) }, text);
  const saveOne = (ch) => editPhotos([d.id], ch);
  const irow = (ic, a, b) => h('div', { class: 'irow' }, icon(ic), h('div', null, h('div', { class: 'a' }, a), b ? h('div', { class: 'b' }, b) : null));
  const sub = (t) => h('div', { class: 'sub' }, t);
  const exposure = [d.aperture ? `ƒ/${+d.aperture.toFixed(1)}` : null,
    d.exposure ? (d.exposure < 1 ? `1/${Math.round(1 / d.exposure)} s` : `${+d.exposure.toFixed(1)} s`) : null,
    d.focal ? `${+d.focal.toFixed(1)} mm${d.focal35 && Math.round(d.focal35) !== Math.round(d.focal) ? ` (${Math.round(d.focal35)} mm eq.)` : ''}` : null,
    d.iso ? `ISO ${d.iso}` : null, d.flash != null && (d.flash & 1) ? 'Flash' : null].filter(Boolean).join(' · ');
  const mp = d.w * d.h / 1e6;
  const peopleTags = d.tags.filter((t) => t.person), tags = d.tags.filter((t) => !t.person);
  const chip = (t) => h('span', { class: 'ichip', title: t.full.replaceAll('/', ' › '), onclick: () => filterFromLightbox({ tag: t.id }) },
    t.person ? h('img', { src: faceUrl(t.id), alt: '' }) : null, t.name,
    S.CAN_MANAGE && d.id !== S.tagById.get(t.id)?.icon ? h('button', { class: 'tx', title: t.person ? `Use this photo as ${t.name}’s badge picture (digiKam must be closed)` : `Use this photo as the thumbnail for “${t.name}” (digiKam must be closed)`,
      onclick: (e) => { e.stopPropagation(); setTagIcon(t, d.id); } }, icon('photo')) : null);
  const stars = d.rating > 0 ? '★'.repeat(d.rating) + '☆'.repeat(5 - d.rating) : null;
  const ex = d.extra || {}, cr = ex.copyright || {}, vid = ex.video || {};
  const kv = [];
  const addKv = (k, v) => { if (v != null && v !== '') kv.push(h('span', null, k), h('span', null, v)); };
  if (!S.EDITING) addKv('Rating', stars);
  if (d.creator) kv.push(h('span', null, 'Creator'), h('span', null, lnk(d.creator, { creator: d.creator })));
  addKv('Copyright', d.copyright);
  for (const [k, v] of Object.entries(cr)) if (!['creator', 'copyrightNotice'].includes(k)) addKv(k.replace('creatorContactInfo.', '').replace(/([A-Z])/g, ' $1').toLowerCase(), v);
  for (const [k, v] of Object.entries(ex.properties || {})) addKv(k, v);
  if (d.video) { addKv('Duration', d.duration ? fmtDur(d.duration) : null); addKv('Video codec', vid.codec); addKv('Frame rate', vid.fps ? `${+(+vid.fps).toFixed(2)} fps` : null); addKv('Audio', vid.audio); }
  addKv('File modified', d.modified?.replace('T', ' '));
  // the camera sits low in the panel, in the same small type as the details: most people never look at it
  const ckv = [];
  if (d.camera || exposure) ckv.push(h('span', null, 'Camera'), h('span', null, d.camera ? lnk(d.camera, { camera: d.camera }) : 'Unknown camera'));
  if (d.lens) ckv.push(h('span', null, 'Lens'), h('span', null, lnk(d.lens, { lens: d.lens })));
  if (exposure) ckv.push(h('span', null, 'Exposure'), h('span', null, exposure));
  const mapEl = d.lat != null && show('location') ? h('div', { class: 'imap' }) : null;
  // everything a model produced, together at the end: description, detected content, objects, text, sharpness, bursts, look-alikes
  const ai = show('ai') ? [
    d.caption_ai ? h('div', { class: 'muted', style: 'margin:2px 0 6px;font-style:italic' }, d.caption_ai.charAt(0).toUpperCase() + d.caption_ai.slice(1)) : null,
    d.concepts?.length ? [sub('Detected'), h('div', { class: 'ichips' }, d.concepts.map((c) => h('span', { class: 'ichip', title: `The model’s guess (${Math.round(c.p * 100)}% of its vote)`, onclick: () => filterFromLightbox({ concept: c.name }) }, icon('spark'), c.name)))] : null,
    d.boxes?.length ? [sub('Objects'), h('div', { class: 'ichips' }, [...new Set(d.boxes.map((b) => b.cls))].map((c) => h('span', { class: 'ichip', onclick: () => filterFromLightbox({ obj: c }) },
      c, h('b', { style: 'font-weight:400;color:var(--fg2)' }, d.boxes.filter((b) => b.cls === c).length > 1 ? ` ×${d.boxes.filter((b) => b.cls === c).length}` : ''))))] : null,
    d.ocr ? h('div', { class: 'irow' }, icon('text'), h('div', null, h('div', { class: 'b' }, 'Text in the picture'), h('div', { class: 'a small' }, d.ocr.slice(0, 600).split(/\n+/).map((t) => t.trim()).filter(Boolean).join(' · ')))) : null,
    d.sharpn != null ? h('div', { class: 'irow' }, icon('spark'), h('div', null, h('div', { class: 'a' }, `Sharper than ${Math.round(d.sharpn * 100)}% of your photos`),
      h('div', { class: 'b' }, [d.faceq != null ? `Face quality ${Math.round(d.faceq * 100)} / 100` : null, d.bright != null && (d.bright < 0.2 || d.clip_lo > 0.5) ? 'Very dark' : null,
        d.bright != null && (d.bright > 0.8 || d.clip_hi > 0.3) ? 'Very bright' : null].filter(Boolean).join(' · ')))) : null,
    d.burst ? h('div', { class: 'irow' }, icon('dup'), h('div', null, h('div', { class: 'a' }, lnk(`Part of a burst of ${d.burst_size} frames`, { burst: d.burst, sort: 'old' })),
      h('div', { class: 'b' }, d.burst_best ? 'Picked as the best frame' : ['Not the best frame', d.burst_best_id ? [' · ', h('button', { type: 'button', class: 'lnk', title: d.burst_best_name, onclick: () => { const i = LB.src.id.indexOf(d.burst_best_id); if (i >= 0) { LB.i = i; setPhotoParam(d.burst_best_id, false); showItem(); } else openLightbox({ id: [d.burst_best_id] }, 0, true); } }, 'open the best frame')] : null]))) : null,
    d.similar && !d.video ? h('div', { class: 'irow' }, icon('spark'), h('div', null, h('div', { class: 'a' }, lnk('More photos like this', { like: d.id })))) : null,
  ].flat().filter(Boolean) : [];
  fill(el,
    h('div', { class: 'ihead' }, h('button', { class: 'iconbtn', title: 'Close info', onclick: toggleInfo }, icon('close')), 'Info',
      h('button', { class: 'pillbtn iactions', style: 'margin-left:auto', title: 'Everything you can do with this photo', onclick: (e) => photoActions(e.currentTarget, d) }, icon('more'), 'Actions'),
      h('button', { class: 'iconbtn' + (LB.prefs ? ' on' : ''), title: 'Choose what this panel shows', onclick: () => { LB.prefs = !LB.prefs; renderInfo(); } }, icon('gear'))),
    LB.prefs ? infoPrefsEditor(renderInfo) : null,
    show('caption') && d.title ? h('div', { class: 'title' }, d.title) : null,
    show('caption') && d.caption ? h('div', { class: 'muted', style: 'margin:6px 0 10px;white-space:pre-wrap' }, d.caption) : null,
    show('date') ? [irow('calendar', lnk(dfLong.format(date(d.ts)), { from: d.day, to: d.day, sort: 'old' }), dfTime.format(date(d.ts))),
      S.EDITING ? h('div', { hidden: true, style: 'margin:0 0 8px 40px' }, dateEditor([d.id], d.taken)) : null] : null,
    show('file') ? [irow(d.video ? 'video' : 'photo', d.name, [d.video ? null : `${mp >= 1 ? mp.toFixed(1) : mp.toFixed(2)} MP`, `${d.w} × ${d.h}`, fmtBytes(d.size), d.fmt].filter(Boolean).join(' · ')),
      d.album ? irow('folder', lnk(d.album.path.replace(/^\//, '') || d.album.name, { album: d.album.id, sort: 'old' }), 'Folder') : null,
      d.versions?.length ? irow('dup', h('span', null, d.versions.map((x, k) => [k ? ' · ' : '', x.id === d.id ? h('b', null, x.role === 'original' ? 'Original' : 'This version')
        : h('button', { type: 'button', class: 'lnk', onclick: () => openLightbox({ id: d.versions.map((y) => y.id) }, k, true) }, x.role === 'original' ? 'Original' : `Version ${k}`)])), 'Versions') : null] : null,
    S.EDITING ? [h('div', { style: 'height:10px' }),  // the rating, title and caption controls follow; they need no heading
      editControls({ rating: Math.max(d.rating, 0) }, saveOne),
      h('input', { class: 'einput', placeholder: 'Title', value: d.title || '', onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') e.target.blur(); },
        onchange: (e) => saveOne({ title: e.target.value }) }),
      h('textarea', { class: 'einput', placeholder: 'Caption', rows: 2, onkeydown: (e) => e.stopPropagation(), onchange: (e) => saveOne({ caption: e.target.value }) }, d.caption || ''),
      d.edited ? h('div', { class: 'enote' }, S.WRITE_FILES ? 'Changed here and saved to the sidecar and the image file. ' : 'Changed here and saved to the sidecar. ',
        S.AUTO_DB ? 'It goes into digiKam’s database by itself within a minute (once digiKam is closed). ' : 'digiKam shows it after “Reread Metadata From File”, or apply it to digiKam’s database from Explore. ',
        h('button', { type: 'button', class: 'lnk', onclick: async () => { try { await post('/api/edit/revert', { ids: [d.id] }); } catch (e) { toastError(e); return; } details.delete(d.id); toast('Edits undone; sidecar restored.'); S.keepView = true; clearTimeout(S.pollTimer); S.pollTimer = setTimeout(async () => { await pollStatus(); if (LB.open && LB.src.id[LB.i] === d.id) showItem(); }, 1500); } }, 'Undo')) : null] : null,
    // one row per face (named, suggested, unknown); a person tag without a face region gets a row with the badge picture
    show('people') ? (() => {
      const faced = new Set(d.faces.map((f) => f.tag));
      const rows = [...faceRegions(d),
        ...peopleTags.filter((t) => !faced.has(t.id)).map((t) => ({ kind: 'tagonly', tag: t.id, name: t.name }))];
      const canDetect = S.CAN_MANAGE && S.FACE_DETECT && !d.video;
      if (!rows.length && !canDetect) return null;
      return [h('h4', null, 'People'),
        h('div', { class: 'facerows' }, rows.map((f) => h('div', { class: 'facerow', title: f.kind === 'confirmed' || f.kind === 'tagonly' ? (S.CAN_MANAGE ? 'Options for this face' : `Show all photos of ${f.name}`) : 'Show the face on the photo',
        onclick: (e) => {
          if (f.kind !== 'tagonly' && !LB.faces) toggleFaces();
          if (S.CAN_MANAGE && f.kind !== 'tagonly') faceMenu(e.currentTarget, f, d);
          else if (S.CAN_MANAGE && f.kind === 'tagonly') tagOnlyMenu(e.currentTarget, f, d);
          else if (f.kind === 'confirmed' || f.kind === 'tagonly') filterFromLightbox({ tag: f.tag });
        } },
        h('img', { src: f.kind === 'tagonly' ? faceUrl(f.tag) : `/facecrop/${d.id}/${f.x}/${f.y}/${f.w}/${f.h}.jpg`, alt: '' }),
        h('span', { class: 'nm' }, f.kind === 'confirmed' || f.kind === 'tagonly' ? f.name : f.kind === 'suggested' ? [h('i', null, 'Suggestion: '), f.name] : h('i', null, 'Unknown face')),
        S.CAN_MANAGE ? h('span', { class: 'muted smaller' }, f.kind === 'tagonly' ? 'no face box ▾' : 'options ▾') : null)))];
    })() : null,
    show('tags') && (tags.length || S.EDITING) ? [h('h4', null, 'Tags'), h('div', { class: 'ichips' }, tags.map((t) => { const c = chip(t);
      if (S.EDITING) c.append(h('button', { class: 'tx', title: 'Remove this tag', onclick: (e) => { e.stopPropagation(); saveOne({ remove_tags: [t.id] }); } }, icon('close'))); return c; })),
      S.EDITING ? h('div', { class: 'mt8',}, tagPicker((t) => saveOne({ add_tags: [t.id] }))) : null] : null,
    show('location') && S.EDITING && d.lat == null ? h('div', { class: 'irow' }, icon('place'), h('div', null, h('button', { type: 'button', class: 'lnk', onclick: () => locationDialog([d.id], null) }, 'Set location on the map'))) : null,
    show('location') && d.lat != null ? [h('h4', null, 'Location', S.EDITING ? h('button', { type: 'button', class: 'lnk', style: 'margin-left:10px;text-transform:none;letter-spacing:0;font-weight:400', onclick: () => locationDialog([d.id], { lat: d.lat, lon: d.lon, alt: d.alt, accuracy: ex.gpsAccuracy }) }, 'Change') : null),
      d.place ? h('div', { class: 'a', style: 'margin-bottom:6px' }, lnk(d.place, { place: d.place, ...(d.region ? { region: d.region } : {}) }),
        d.region ? [', ', lnk(d.region, { region: d.region })] : null, d.country ? [', ', lnk(d.country, { country: d.country })] : null) : null,
      ex.gpsAccuracy ? h('div', { class: 'approx', title: 'Only the town is known: the pin marks its center' }, icon('place'),
        `Approximate location · within about ${fmtDist(ex.gpsAccuracy)}`) : null,
      mapEl,
      h('div', { class: 'irow' }, icon('place'), h('div', null, h('div', { class: 'a' }, `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}`),
        h('div', { class: 'b' }, d.alt != null && d.alt !== 0 ? `${fmtHeight(d.alt)} altitude · ` : null, ex.orientationGps != null ? `facing ${Math.round(ex.orientationGps)}° · ` : null,
          h('a', { href: mapLink(d.lat, d.lon), target: '_blank', rel: 'noopener' }, S.MAPS.linkLabel), ' · ', h('button', { type: 'button', class: 'lnk', onclick: () => streetView(d.lat, d.lon, d.name) }, 'Street View'))))] : null,
    show('details') && kv.length ? [h('h4', null, 'Details'), h('div', { class: 'kv' }, kv)] : null,
    show('camera') && ckv.length ? [h('h4', null, 'Camera'), h('div', { class: 'kv' }, ckv)] : null,
    show('file') ? [h('h4', null, 'File'), h('div', { class: 'b muted', style: 'word-break:break-all;user-select:all;font-size:12.5px' }, d.path),
    ] : null,
    ai.length ? [h('h4', null, 'AI analysis'), ai] : null,
    show('history') && S.EDITING ? (() => {  // last, folded; editors only; nothing shows for a photo never changed here
      const box = h('details', { class: 'hfold' });
      const list = h('div', { class: 'hlist small' });
      box.hidden = true;
      api(`/api/history?photo=${d.id}&limit=6`).then((r) => {
        if (!r.entries.length || !box.isConnected) return;
        box.append(h('summary', null, `Edit log · ${plural(r.total, 'change')}`), list);
        if (r.total > 6) box.append(h('a', { class: 'lnk small', href: `#/history?photo=${d.id}` }, `See all ${nf.format(r.total)}`));
        list.append(...r.entries.map((e) => histEntry(e, () => { details.delete(d.id); showItem(); }, false)));
        box.hidden = false;
      }).catch(() => {});
      return box;
    })() : null);
  if (mapEl) {
    const token = LB.token;
    loadLeaflet(false).then(async () => {
      if (token !== LB.token || !mapEl.isConnected) return;
      const L = window.L;
      await loadMaps();
      if (token !== LB.token || !mapEl.isConnected) return;
      LB.mini = L.map(mapEl, { zoomControl: false, attributionControl: false, scrollWheelZoom: false }).setView([d.lat, d.lon], 13);
      tiles().addTo(LB.mini);
      const acc = (d.extra || {}).gpsAccuracy;
      if (acc) {  // an approximate position: the area it may be in, not a pin pretending to know the spot
        const c = L.circle([d.lat, d.lon], { radius: acc, color: '#c77d00', weight: 1, fillOpacity: 0.12 }).addTo(LB.mini);
        LB.mini.fitBounds(c.getBounds(), { padding: [6, 6] });
      } else {
        L.marker([d.lat, d.lon], { icon: L.divIcon({ className: '', html: '<div class="pm"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }) }).addTo(LB.mini);
      }
    }).catch(() => { mapEl.remove(); });
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {  // a menu or a dialog first; the sign-in dialog stays
    const menu = document.querySelector('.facemenu');
    if (menu) { menu.remove(); return; }
    const backs = [...document.querySelectorAll('.modal-back:not(.required)')];
    if (backs.length) { backs[backs.length - 1].remove(); return; }
  }
  // Enter or Space on a card, tile, row or link-styled control that is not a real button (role=button) acts like a click
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[role="button"]:not(button):not(a[href])') && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); e.target.click(); return; }
  if (document.querySelector('.modal-back')) return;  // a dialog is open: its keys are its own (Space on a button, Delete in a field)
  if (e.target.matches('input, select, textarea') || e.metaKey || e.ctrlKey || e.altKey) return;
  if (LB.open) {
    if (e.key === 'Escape') { if (LB.mode) endMode(); else closeLightbox(true); }
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'i' || e.key === 'I') toggleInfo();
    else if (e.key === 'f' || e.key === 'F') toggleFaces();
    else if (e.key === 'o' || e.key === 'O') toggleObjects();
    else if (e.key === 'd' || e.key === 'D') LB.el.dl.click();
    else if (e.key === 'l' || e.key === 'L') LB.el.favBtn.click();
    else if ((e.key === 't' || e.key === 'T') && S.EDITING) tagDialog([LB.src.id[LB.i]]);
    else if ((e.key === 'p' || e.key === 'P') && S.CAN_MANAGE) nameNextFace();
    else if (S.EDITING && /^[0-5]$/.test(e.key)) editPhotos([LB.src.id[LB.i]], { rating: +e.key });
    else if ((e.key === 'Delete' || e.key === 'Backspace') && S.CAN_MANAGE) deleteCurrent();
    else if ((e.key === ']' || e.key === '[') && S.CAN_MANAGE && S.LOSSLESS) transformPhotos([LB.src.id[LB.i]], e.key === ']' ? 'right' : 'left');
    else if (e.key === ' ') { const v = LB.el.frame.querySelector('video'); v ? (v.paused ? v.play() : v.pause()) : toggleSlideshow(); }
    else return;
    e.preventDefault();
  } else if (e.key === '/') { $('#q').focus(); e.preventDefault(); }
  else if (e.key === 'Escape' && G.sel.size) clearSelection();
  else if ((e.key === 'Delete' || e.key === 'Backspace') && G.sel.size && S.CAN_MANAGE && !document.querySelector('.modal-back')) { trashPhotos([...G.sel]); e.preventDefault(); }
});
