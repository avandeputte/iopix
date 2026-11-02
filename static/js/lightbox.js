/** The photo viewer: navigation, zoom, the film strip, sharing, lossless edits, slideshow (the info panel is info.js, the face tools faces.js). */
import { $, PREF, TOAST_LONG, api, date, dfDay, dfTime, fill, fmtBytes, fmtKnown, h, icon, plural, post, store } from './core.js';
import { S, ST, go, hashFor, onRoute, parseHash, photosWith, pollSoon, toast, toastError } from './state.js';
import { G, renderGrid, scrollToIndex } from './grid.js';
import { albumDialog, clearSelection, setFavorite, trashPhotos } from './actions.js';
import { editPhotos, tagDialog } from './editing.js';
import { M } from './map.js';
import { nameNextFace, reapplyRecent, renderFaces, startDrawFace, toggleFaces, toggleObjects } from './faces.js';
import { renderInfo } from './info.js';

// src = the list being browsed ({id, w, h, ts, video}); faces/objects = overlay toggles; pushed = we added a history entry for ?p=
export const LB = { open: false, src: null, i: 0, token: 0, info: store.get(PREF.info, '0') === '1', prefs: false, faces: false, objects: false, scale: 1, tx: 0, ty: 0, ar: 1.5, pushed: false, detail: null, play: 0, mini: null, hires: false };
export const details = new Map();  // photo id -> promise of /api/photo/:id, bounded LRU-ish cache
const NATIVE = new Set(['JPG', 'PNG', 'GIF', 'WEBP']);
export function getDetail(id) {
  if (!details.has(id)) {
    details.set(id, api(`/api/photo/${id}`).then((d) => reapplyRecent(id, d)).catch((e) => { details.delete(id); throw e; }));  // not cached: asked again next time
    if (details.size > 400) details.delete(details.keys().next().value);
  }
  return details.get(id);
}
function currentSource() { return S.route.view === 'map' ? M.data : S.route.view === 'photos' ? G.data : { id: [] }; }
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

function buildLightbox() {
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
async function shareNow() {
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
  try { const r = await fetch(`/preview/${id}`); if (!r.ok) return null; const blob = await r.blob(); return new File([blob], d.name.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' }); } catch { return null; }
}

/** The fallback menu: a link to the photo here, the original or a smaller JPEG as a download,
 *  the original in a tab, and an email with the link. Anchored under the share button. */
function shareMenu(anchor) {
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
function peek(id) {
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
function unpeek() {
  clearTimeout(LB.peekT);
  LB.peekEl?.remove(); LB.peekEl = null;
}

/** Rebuild the strip around the current photo when it drifts out of the rendered window, and
 *  keep the current one marked and centered. */
function renderStrip() {
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
      kids.push(h('img', { src: `/thumb/${src.id[k]}?h=120`, alt: '', loading: 'lazy', draggable: 'false', 'data-i': k, style: `width:${Math.round(56 * ar)}px`,
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
    LB.info = store.get(PREF.info, '0') === '1'; LB.prefs = false;
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
  const item = (label, fn, cls = '') => h('div', { class: 'fmi ' + cls, tabindex: 0, role: 'button', onclick: (e) => { e.stopPropagation(); menu.remove(); fn(); } }, label);
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
    : h('div', { class: 'fmi ' + (it[2] || ''), tabindex: 0, role: 'button', onclick: (e) => { e.stopPropagation(); menu.remove(); it[1](e); } }, it[0])));
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
async function refreshImages(id) {
  await Promise.all([fetch(`/thumb/${id}`, { cache: 'reload' }), fetch(`/preview/${id}`, { cache: 'reload' })]).catch(() => {});
  document.querySelectorAll(`img[src$="/thumb/${id}"], img[src$="/preview/${id}"]`).forEach((img) => { const u = img.src; img.src = ''; img.src = u; });
}
async function afterFileChange(id, d) {
  details.delete(id);
  await refreshImages(id);
  if (LB.open) {
    const i = LB.src.id.indexOf(id);
    if (i >= 0 && LB.src.w && d?.upright) { LB.src.w[i] = d.upright[0]; LB.src.h[i] = d.upright[1]; }
    if (i === LB.i) { LB.detail = null; showItem(); }
  }
  S.keepView = true; pollSoon(600);
}
async function undoLast(id) {
  try {
    const e = (await api(`/api/history?photo=${id}&limit=1`)).entries[0];
    if (!e || e.action !== 'lossless') throw new Error('Nothing to undo');
    await post(`/api/history/${e.id}/restore`, { kind: 'undo' });
    await afterFileChange(id, null); toast('Put back as it was.');
  } catch (err) { toastError(err, TOAST_LONG); }
}
export async function transformPhotos(ids, op) {
  let r;
  try { r = await post('/api/photos/transform', { ids, op }); } catch (e) { toastError(e); return false; }
  for (const d of r.done) await afterFileChange(d.id, d);
  const label = TRANSFORMS.find((t) => t[0] === op)[1].toLowerCase();
  if (r.failed.length) toast(`${r.done.length ? `${plural(r.done.length, 'photo')} ${label.replace('rotate', 'rotated').replace('flip', 'flipped').replace('turn', 'turned')}; ` : ''}${plural(r.failed.length, 'photo')} not: ${r.failed[0].error}`, null, null, 10000);
  else toast(ids.length > 1 ? `${plural(r.done.length, 'photo')}: ${label}.` : `${label[0].toUpperCase()}${label.slice(1)}.`, r.done.length === 1 ? 'Undo' : null, () => undoLast(r.done[0].id));
  return r.done.length > 0;
}
/** Adjust: exposure, contrast, saturation, warmth, straighten and auto levels, previewed live on screen and saved
 *  as a new version next to the original (digiKam's "save as new version"); the original stays as it is. */
/** The one editing mode in progress on the photo shown (adjust, crop, draw a face, move a face). It captures the
 *  photo it started on: stepping to another photo or closing the viewer ends it, so it can never act on the wrong photo. */
export function setMode(stop) { endMode(); LB.mode = { stop }; }
function endMode() { const m = LB.mode; LB.mode = null; if (m) m.stop(); }
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
function startCrop() {
  const { stage, frame } = LB.el, d = LB.detail;
  if (!d || d.video || !S.LOSSLESS) return;
  const W = d.w, H = d.h;
  const ext = (d.name || '').split('.').pop().toLowerCase();
  if (S.LOSSLESS.crop && !S.LOSSLESS.crop.includes('.' + ext)) { toast(`Cropping here works for ${S.LOSSLESS.crop.map((x) => x.slice(1).toUpperCase()).join(', ')} files.`, null, null, TOAST_LONG); return; }
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
function dropIndex(src, k) {
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
/** Like dropIndex for several positions at once: one pass over every column instead of one copy per photo. */
export function dropIndexes(src, ks) {
  const gone = new Set(ks);
  if (!gone.size) return src;
  const n = src.id.length, shift = new Int32Array(n + 1);
  for (let i = 0, g = 0; i < n; i++) { if (gone.has(i)) g++; shift[i] = g; }  // how many removed at or before i
  const map = (i) => (gone.has(i) ? -1 : i - shift[i]);
  const out = {};
  for (const [key, v] of Object.entries(src)) {
    if (Array.isArray(v) || ArrayBuffer.isView(v)) {
      if (!INDEX_LISTS.has(key)) out[key] = Array.from(v).filter((_, i) => !gone.has(i));
      else out[key] = Array.from(v).map(map).filter((i) => i >= 0);
    } else if (v instanceof Set) out[key] = new Set([...v].map(map).filter((i) => i >= 0));
    else if (v && typeof v === 'object' && Object.keys(v).every((x) => /^\d+$/.test(x))) {
      out[key] = {};
      for (const [i, x] of Object.entries(v)) { const j = map(+i); if (j >= 0) out[key][j] = x; }
    } else out[key] = v;
  }
  return out;
}
function deleteCurrent() {
  if (!S.CAN_MANAGE || !LB.open) return;
  const id = LB.src.id[LB.i];
  const wasGrid = LB.src === G.data;  // the grid replaces its data while trashing: the viewer must follow it, not a copy
  trashPhotos([id], () => {  // drop it from the sequence and show the next one (or close when it was the last)
    const k = LB.src.id.indexOf(id);
    if (wasGrid && G.data) LB.src = G.data;
    else if (k >= 0) LB.src = dropIndex(LB.src, k);
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
function step(d) {
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
  el.ttl.textContent = src.ts ? fmtKnown(src.ts[i], src.dp?.[i]) : '';
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
    LB.video = v;
  };
  /** Chapter thumbnails under the player (videos.py): click to jump there. */
  const mountChapters = (d) => {
    const ch = d.videoInfo?.chapters || [];
    el.chapters?.remove();
    if (!ch.length || !LB.video) return;
    const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
    el.chapters = h('div', { class: 'lb-chapters', onclick: (e) => e.stopPropagation() }, ch.map((t) => h('button', { type: 'button', title: `Jump to ${fmt(t)}`, onclick: () => { if (LB.video) { LB.video.currentTime = t; LB.video.play(); } } },
      h('img', { src: `/video/${id}/frame?t=${t}&h=120`, alt: '', loading: 'lazy' }), h('span', null, fmt(t)))));
    el.stage.append(el.chapters);
  };
  if (isVideo === true) mountVideo(); else if (isVideo === false) loadPreview();
  getDetail(id).then((d) => {
    if (token !== LB.token) return;
    LB.detail = d;
    LB.ar = d.w / (d.h || 1);
    sizeFrame();
    if (isVideo === undefined) { isVideo = !!d.video; d.video ? mountVideo() : loadPreview(); }
    if (d.video) mountChapters(d);
    renderTitle();
    el.facesBtn.style.display = d.faces.length || d.faces_other?.length ? '' : 'none';
    el.addFaceBtn.style.display = S.CAN_MANAGE && !d.video ? '' : 'none';
    el.delBtn.style.display = S.CAN_MANAGE ? '' : 'none';
    el.editBtn.style.display = S.CAN_MANAGE && S.LOSSLESS && !d.video ? '' : 'none';
    el.objectsBtn.style.display = d.boxes?.length ? '' : 'none';
    renderFaces();
    if (LB.info) { try { renderInfo(); } catch (e) { console.error('info panel', e); } }
  }, (e) => {
    if (token !== LB.token) return;
    el.frame.replaceChildren(h('div', { class: 'empty error', style: 'color:#fff' }, /404/.test(e.message) ? 'This photo is no longer in the library.' : e.message));
    [el.facesBtn, el.addFaceBtn, el.delBtn, el.editBtn, el.objectsBtn].forEach((b) => { b.style.display = 'none'; });
  }).catch((e) => console.error('viewer', e));
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
function applyZoom() {
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
function zoomAt(factor, cx, cy) {
  const { stage } = LB.el, r = stage.getBoundingClientRect();
  const s2 = Math.min(10, Math.max(1, LB.scale * factor));
  const dx = cx - r.left - r.width / 2 - LB.tx, dy = cy - r.top - r.height / 2 - LB.ty;
  LB.tx += dx * (1 - s2 / LB.scale); LB.ty += dy * (1 - s2 / LB.scale);
  LB.scale = s2;
  applyZoom();
}
function initGestures(stage) {
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
  store.set(PREF.info, LB.info ? '1' : '0');
  LB.el.info.hidden = !LB.info;
  LB.el.infoBtn.classList.toggle('on', LB.info);
  if (LB.info) renderInfo();
  sizeFrame();
}
function toggleSlideshow() { LB.play ? stopSlideshow() : (LB.play = setInterval(() => { if (!LB.el.frame.querySelector('video')) step(1); }, 4000), LB.el.playBtn.replaceChildren(icon('pause'))); }
function stopSlideshow() { clearInterval(LB.play); LB.play = 0; LB.el?.playBtn.replaceChildren(icon('play')); }
export function filterFromLightbox(obj) {
  closeLightbox(false);
  LB.pushed = false;
  go('photos', photosWith(obj));
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
