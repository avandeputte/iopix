/** The timeline grid: layout, virtual rendering, selection by click and keyboard. */
import { $, MONTHS, api, date, dfDay, dfMonth, fill, fmtDur, fmtRange, h, icon, main, nf, plural, post, store } from './core.js';
import { S, ST, applyFilter, filterParams, loadState, onRoute, savedSearchFor, toast, toastError } from './state.js';
import { crumbs, photoCrumbs } from './search.js';
import { clearSelection, promptDialog, selectRange, toggleSelect, updateSelBar } from './actions.js';
import { downloadDialog } from './download.js';
import { LB, dropIndex, openLightbox, sizeFrame, syncLightbox } from './lightbox.js';

export const SIZES = { S: [120, 80], M: [190, 110], L: [280, 170] };
// grid state: sel = selected photo ids; items = laid-out rows/headers; rendered = row index -> element currently in the DOM
export const G = { sel: new Set(), lastSel: -1, data: null, items: [], ar: null, total: 0, top0: 0, restore: 0, rendered: new Map(), token: 0,
  size: store.get('tile', 'M') in SIZES ? store.get('tile', 'M') : 'M', group: store.get('group', 'day') === 'month' ? 'month' : 'day', lastTop: 0, lastT: 0, settle: 0, scrub: null };
/** The section a timestamp belongs to in the chronological grid: its day, or its month. */
export function groupKey(ts) {
  if (G.group === 'month') { const dt = date(ts); return dt.getUTCFullYear() * 12 + dt.getUTCMonth(); }
  return Math.floor(ts / 86400);
}
export const GAP = 4, BAR_H = 48, HEAD_H = 46;

export async function showPhotos(fp) {
  const token = ++G.token;
  G.data = null;
  G.rendered.clear();
  const grid = h('div', { id: 'grid' }, h('div', { class: 'empty' }, 'Loading…'));
  main.replaceChildren(grid);
  let data;
  try { data = await api('/api/photos?' + fp.toString()); } catch (e) { grid.replaceChildren(h('div', { class: 'empty' }, 'Could not load photos: ' + e.message)); return; }
  if (token !== G.token || S.route.view !== 'photos') return;
  G.data = data;
  G.geo = new Set(data.geo || []);
  G.faces = new Set(data.faces || []);
  G.sort = fp.get('sort') || (data.ranked ? 'rel' : 'new');
  const n = data.count;
  G.ar = new Float32Array(n);
  for (let i = 0; i < n; i++) G.ar[i] = Math.min(2.6, Math.max(0.5, data.w[i] / (data.h[i] || 1)));
  grid.replaceChildren();
  grid.onclick = (e) => {
    const hc = e.target.closest('.hcheck');
    if (hc) { selectRange(+hc.dataset.s, +hc.dataset.e - 1, 'toggle'); return; }
    const t = e.target.closest('.tile');
    if (!t) return;
    const i = +t.dataset.i;
    if (e.target.closest('.tcheck') || G.sel.size) {
      if (e.shiftKey && G.lastSel >= 0) selectRange(Math.min(G.lastSel, i), Math.max(G.lastSel, i), true);
      else toggleSelect(i);
    } else openLightbox(G.data, i, true);
  };
  if (!n) {
    grid.append(h('div', { class: 'empty' }, 'No photos match this search.'));
    syncLightbox();
    return;
  }
  layoutGrid();
  if (G.restore) { main.scrollTop = G.restore; G.restore = 0; }
  renderGrid(true);
  updateSelBar();
  syncLightbox();
}

/** Take photos out of the grid on screen now (moved to the trash): the others close up and the scroll position stays. */
export function dropPhotos(ids) {
  if (!G.data) return;
  const gone = new Set(ids), ks = [];
  G.data.id.forEach((id, i) => { if (gone.has(id)) ks.push(i); });
  if (!ks.length) return;
  let d = G.data;
  for (const k of ks.reverse()) d = dropIndex(d, k);
  d.count = d.id.length;
  if (!d.count) { S.viewKey = null; onRoute(); return; }
  G.data = d; G.geo = new Set(d.geo || []); G.faces = new Set(d.faces || []);
  G.ar = new Float32Array(d.count);
  for (let i = 0; i < d.count; i++) G.ar[i] = Math.min(2.6, Math.max(0.5, d.w[i] / (d.h[i] || 1)));
  const top = main.scrollTop;
  G.rendered.forEach((el) => el.remove()); G.rendered.clear();
  layoutGrid();
  main.scrollTop = top;
  renderGrid(true);
}
export function layoutGrid() {
  const grid = $('#grid'), d = G.data;
  if (!grid || !d) return;
  const W = grid.clientWidth, H = SIZES[G.size][innerWidth < 800 ? 1 : 0];
  const items = [], byDate = G.sort === 'new' || G.sort === 'old';
  const sections = [];
  if (byDate) {
    let start = 0, day = groupKey(d.ts[0]);
    for (let i = 1; i <= d.count; i++) {
      const dd = i < d.count ? groupKey(d.ts[i]) : null;
      if (dd !== day) { sections.push([start, i]); start = i; day = dd; }
    }
  } else sections.push([0, d.count]);
  const trail = photoCrumbs(filterParams());
  const top0 = 0;  // the trail is in the fixed strip above the page (crumbs())
  G.top0 = top0;
  let y = BAR_H + top0;
  for (const [s, e] of sections) {
    if (byDate) { items.push({ y, h: HEAD_H, s, e, head: true }); y += HEAD_H; }
    let i = s;
    while (i < e) {
      let sum = 0, j = i;
      while (j < e) { sum += G.ar[j++]; if (sum * H + (j - i - 1) * GAP >= W) break; }
      const full = sum * H + (j - i - 1) * GAP >= W;
      const rh = full ? (W - (j - i - 1) * GAP) / sum : H;
      items.push({ y, h: rh, s: i, e: j });
      y += rh + GAP;
      i = j;
    }
    y += 8;
  }
  G.items = items;
  G.total = y + 40;
  grid.style.height = G.total + 'px';
  for (const el of G.rendered.values()) el.remove();
  G.rendered.clear();
  $('.gbar', grid)?.remove();
  crumbs(trail);
  grid.append(h('div', { class: 'gbar', style: `top:${top0}px` },
    h('span', { class: 'count' }, plural(d.count, 'photo')),
    byDate ? h('span', null, fmtRange(Math.min(d.ts[0], d.ts[d.count - 1]), Math.max(d.ts[0], d.ts[d.count - 1]))) : null,
    h('span', { class: 'sp' }),
    h('button', { type: 'button', class: 'lnk selall', title: 'Select everything in this view', hidden: G.sel.size >= d.count, onclick: () => selectRange(0, d.count - 1, true) }, 'Select all'),
    h('button', { type: 'button', class: 'lnk selnone', title: 'Deselect everything', hidden: !G.sel.size, onclick: () => clearSelection() }, 'Deselect all'),
    [...filterParams().keys()].some((k) => k !== 'sort') ? h('button', { class: 'pillbtn dlbtn', title: 'Download these as zip files', onclick: () => downloadDialog(filterParams()) }, icon('download'), h('span', null, 'Download')) : null,
    // the default sort (newest, or best match for ranked results) is left out of the URL
    h('select', { title: 'Sort', onchange: (e) => applyFilter((p) => { ['new', 'rel'].includes(e.target.value) && !(d.ranked && e.target.value === 'new') ? p.delete('sort') : p.set('sort', e.target.value); }) },
      ...[...(d.ranked ? [['rel', 'Best match']] : []), ['new', 'Newest first'], ['old', 'Oldest first'], ['added', 'Recently modified'], ['rating', 'Top rated'], ['sharp', 'Sharpest first'], ['blur', 'Blurriest first'], ['name', 'File name'], ['size', 'Largest files']]
        .map(([v, l]) => h('option', { value: v, selected: v === G.sort }, l))),
    byDate ? h('input', { type: 'date', class: 'jumpdate', title: 'Jump to a date', onkeydown: (e) => e.stopPropagation(), onchange: (e) => {
      if (!e.target.value) return;
      const t0 = Date.parse(e.target.value + 'T00:00:00Z') / 1000, t1 = t0 + 86400;
      const ts = d.ts, n = d.count;  // ts is sorted (desc for newest first, asc for oldest first): binary search for the day
      let lo = 0, hi = n;
      const desc = G.sort === 'new';
      while (lo < hi) { const mid = (lo + hi) >> 1; (desc ? ts[mid] >= t1 : ts[mid] < t0) ? lo = mid + 1 : hi = mid; }
      scrollToIndex(Math.min(lo, n - 1), true); renderGrid(true);
      e.target.blur();
    } }) : null,
    [...filterParams().keys()].some((k) => k !== 'sort') && (S.USER || !S.USERS_EXIST) && !savedSearchFor(filterParams()) ? h('button', { class: 'pillbtn dlbtn', title: 'Save this search as a smart album (it updates itself)', onclick: async () => {
      const n = await promptDialog('Name for this smart album'); if (!n || !n.trim()) return;
      const q = filterParams(); q.delete('sort');
      try { await post('/api/searches', { name: n.trim(), query: q.toString() }); toast(`Saved “${n.trim()}” under Albums.`); await loadState(); layoutGrid(); renderGrid(true); } catch (e) { toastError(e); }
    } }, icon('album'), h('span', null, 'Save search')) : null,
    byDate ? h('select', { title: 'Group the photos by day or by month', class: 'groupsel', onchange: (e) => {
      const anchor = firstVisibleIndex();
      G.group = e.target.value; store.set('group', G.group); layoutGrid(); scrollToIndex(anchor, true); renderGrid(true);
    } }, h('option', { value: 'day', selected: G.group === 'day' }, 'By day'), h('option', { value: 'month', selected: G.group === 'month' }, 'By month')) : null,
    h('span', { class: 'seg', title: 'Thumbnail size' }, ...Object.keys(SIZES).map((k) => h('button', { class: k === G.size ? 'on' : '', onclick: () => {
      const anchor = firstVisibleIndex();
      G.size = k; store.set('tile', k); layoutGrid(); scrollToIndex(anchor, true); renderGrid(true);
    } }, k)))));
  buildScrubber(byDate);
}

export function itemAt(y) { // last item starting at or above y
  let lo = 0, hi = G.items.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (G.items[mid].y <= y) lo = mid; else hi = mid - 1; }
  return lo;
}
export function firstVisibleIndex() {
  if (!G.items.length) return 0;
  return G.items[itemAt(main.scrollTop + 1)].s;
}
export function scrollToIndex(i, top = false) {
  const it = G.items.find((x) => !x.head && i >= x.s && i < x.e);
  if (!it) return;
  if (top) main.scrollTop = it.y - (it.s === 0 ? BAR_H + HEAD_H + (G.top0 || 0) : 4);
  else if (it.y < main.scrollTop || it.y + it.h > main.scrollTop + main.clientHeight) main.scrollTop = it.y - main.clientHeight / 2 + it.h / 2;
}

export function renderGrid(force) {
  const grid = $('#grid'), d = G.data;
  if (!grid || !d || !G.items.length) return;
  const top = main.scrollTop, vh = main.clientHeight, now = performance.now();
  const speed = Math.abs(top - G.lastTop) / Math.max(1, now - G.lastT); // px per ms
  G.lastTop = top; G.lastT = now;
  const fast = !force && speed > 12;
  const a = itemAt(top - vh * 0.8), b = itemAt(top + vh * 1.8);
  for (const [k, el] of G.rendered) if (k < a || k > b) { el.remove(); G.rendered.delete(k); }
  for (let k = a; k <= b; k++) {
    if (G.rendered.has(k)) continue;
    const it = G.items[k];
    let el;
    if (it.head) {
      el = h('div', { class: 'ghead', style: `top:${it.y}px;height:${it.h}px` },
        h('span', { class: 'hcheck', title: G.group === 'month' ? 'Select this month' : 'Select this day', 'data-s': it.s, 'data-e': it.e }, icon('check')), (G.group === 'month' ? dfMonth : dfDay).format(date(d.ts[it.s])),
        it.e - it.s > 12 ? h('span', { class: 'n' }, nf.format(it.e - it.s)) : null);
    } else {
      el = h('div', { class: 'grow', style: `top:${it.y}px;height:${it.h}px` });
      for (let i = it.s; i < it.e; i++) {
        const img = document.createElement('img');
        img.alt = ''; img.decoding = 'async'; img.draggable = false;
        if (fast) img.dataset.src = `/thumb/${d.id[i]}`; else img.src = `/thumb/${d.id[i]}`;
        const tile = h('div', { class: 'tile' + (G.sel.has(d.id[i]) ? ' sel' : ''), 'data-i': i, tabindex: 0, role: 'button', style: `width:${(G.ar[i] * it.h).toFixed(2)}px` }, img,
          h('span', { class: 'tcheck', title: 'Select' }, icon('check')), ST.fav.has(d.id[i]) ? h('span', { class: 'fbadge' }, icon('heart')) : null);
        if (i in d.video) tile.append(h('span', { class: 'vbadge' }, d.video[i] ? fmtDur(d.video[i]) : null, icon('play')));
        const marks = [G.faces.has(i) ? h('span', { class: 'cbadge', title: 'Has face tags' }, icon('face')) : null, G.geo.has(i) ? h('span', { class: 'cbadge', title: 'Has GPS coordinates' }, icon('place')) : null].filter(Boolean);
        if (marks.length) tile.append(h('span', { class: 'cbadges' }, marks));
        el.append(tile);
      }
    }
    grid.append(el);
    G.rendered.set(k, el);
  }
  // While flinging through years, hold off on image requests until scrolling settles.
  clearTimeout(G.settle);
  G.settle = setTimeout(() => {
    grid.querySelectorAll('img[data-src]').forEach((img) => { img.src = img.dataset.src; delete img.dataset.src; });
  }, fast ? 90 : 0);
  updateScrubPos(top, vh);
}

main.addEventListener('scroll', () => {
  if (S.route.view !== 'photos' || S.scrollRaf) return;
  S.scrollRaf = requestAnimationFrame(() => { S.scrollRaf = 0; renderGrid(); });
}, { passive: true });

export function removeScrubber() { $('#scrub')?.remove(); G.scrub = null; }
export function buildScrubber(byDate) {
  removeScrubber();
  if (!byDate || G.total < main.clientHeight * 3) return;
  const d = G.data;
  const pos = h('div', { class: 'pos' });
  const sc = h('div', { id: 'scrub' }, pos, h('div', { class: 'bubble', hidden: true }));
  document.body.append(sc);
  const track = sc.clientHeight;
  G.scrub = { pos, track, label: null, lastIt: -1 };  // read once here; updateScrubPos runs on every scroll frame and must not force a layout
  // the density ribbon: one band per month, as tall as the month is on the timeline and as wide as it has photos
  const months = [];
  for (const it of G.items) {
    if (!it.head) continue;
    const dt = date(d.ts[it.s]), key = dt.getUTCFullYear() * 12 + dt.getUTCMonth();
    const last = months[months.length - 1];
    if (last && last.key === key) { last.n += it.e - it.s; continue; }
    if (last) last.y1 = it.y;
    months.push({ key, y0: it.y, n: it.e - it.s });
  }
  if (months.length) months[months.length - 1].y1 = G.total;
  const most = Math.max(1, ...months.map((m) => m.n));
  const ribbon = h('div', { class: 'ribbon' }, months.map((m) => {
    const top = (m.y0 / G.total) * track, hgt = Math.max(1, ((m.y1 - m.y0) / G.total) * track - 1);
    const w = 3 + Math.round(11 * Math.sqrt(m.n / most));  // square root: a quiet month still shows, a huge one does not swamp the rail
    return h('i', { style: `top:${top.toFixed(1)}px;height:${hgt.toFixed(1)}px;width:${w}px;opacity:${(0.35 + 0.65 * m.n / most).toFixed(2)}` });
  }));
  sc.prepend(ribbon);
  let lastYear = null, lastPx = -99;
  for (const it of G.items) {
    if (!it.head) continue;
    const yr = date(d.ts[it.s]).getUTCFullYear();
    if (yr === lastYear) continue;
    lastYear = yr;
    const px = (it.y / G.total) * track;
    if (px - lastPx < 16) continue;
    lastPx = px;
    sc.append(h('span', { class: 'yr', style: `top:${px}px` }, yr));
  }
  // months as well when the whole list spans at most three years (a trip, a season), with room between them
  const span = Math.abs(d.ts[0] - d.ts[d.count - 1]) / 86400 / 365;
  if (span <= 3) {
    const yearPx = [...sc.querySelectorAll('.yr')].map((e) => parseFloat(e.style.top));
    let lastKey = null, lastMpx = -99;
    for (const it of G.items) {
      if (!it.head) continue;
      const dt = date(d.ts[it.s]), key = dt.getUTCFullYear() * 12 + dt.getUTCMonth();
      if (key === lastKey) continue;
      lastKey = key;
      const px = (it.y / G.total) * track;
      if (px - lastMpx < 18 || yearPx.some((y) => Math.abs(y - px) < 16)) continue;  // never on top of a year
      lastMpx = px;
      sc.append(h('span', { class: 'yr mo', style: `top:${px}px` }, MONTHS[dt.getUTCMonth()].slice(0, 3)));
    }
  }
  const bubble = $('.bubble', sc);
  const at = (e) => Math.min(1, Math.max(0, (e.clientY - sc.getBoundingClientRect().top) / track));
  // the preview: month, how many photos it has, and a few thumbnails from where a click would land
  let shown = -1;
  const monthCount = (i) => { const dt = date(d.ts[i]), key = dt.getUTCFullYear() * 12 + dt.getUTCMonth(); return months.find((m) => m.key === key)?.n || 0; };
  const label = (f) => {
    const k = itemAt(f * G.total), it = G.items[k];
    bubble.style.top = Math.min(track - 60, Math.max(60, f * track)) + 'px';
    bubble.hidden = false;
    if (k === shown) return;
    shown = k;
    const ids = [];
    for (let j = it.s; j < Math.min(d.count, it.s + 4); j++) ids.push(d.id[j]);
    fill(bubble, h('div', { class: 'bt' }, h('b', null, dfMonth.format(date(d.ts[it.s]))), h('span', { class: 'muted' }, plural(monthCount(it.s), 'photo'))),
      h('div', { class: 'bth' }, ids.map((id) => h('img', { src: `/thumb/${id}`, alt: '', loading: 'eager', decoding: 'async' }))));
  };
  let drag = false;
  sc.onpointerdown = (e) => { drag = true; sc.classList.add('drag'); sc.setPointerCapture(e.pointerId); main.scrollTop = at(e) * (G.total - main.clientHeight); label(at(e)); };
  sc.onpointermove = (e) => { label(at(e)); if (drag) main.scrollTop = at(e) * (G.total - main.clientHeight); };
  sc.onpointerup = sc.onpointercancel = () => { drag = false; sc.classList.remove('drag'); };
  sc.onpointerleave = () => { if (!drag) { bubble.hidden = true; shown = -1; } };
  updateScrubPos();
}
export function updateScrubPos(top = main.scrollTop, vh = main.clientHeight) {
  const s = G.scrub;
  if (!s) return;
  s.pos.style.top = (top / Math.max(1, G.total - vh)) * (s.track - 2) + 'px';
  const k = itemAt(top + Math.min(vh * 0.3, 240));  // what the eye is on, a little below the top; a binary search, and the label only changes with the month
  if (k === s.lastIt) return;
  s.lastIt = k;
  const it = G.items[k];
  if (!it || !G.data) return;
  const dt = date(G.data.ts[it.s]), txt = `${MONTHS[dt.getUTCMonth()].slice(0, 3)} ${String(dt.getUTCFullYear()).slice(2)}`;
  if (s.label !== txt) { s.label = txt; s.pos.textContent = txt; }
}
new ResizeObserver(() => {
  if (S.route.view === 'photos' && G.data?.count) {
    const anchor = firstVisibleIndex(), atTop = main.scrollTop < 10;
    layoutGrid();
    if (!atTop) scrollToIndex(anchor, true);
    renderGrid(true);
  }
  if (LB.open) sizeFrame();
}).observe(main);
