/** The map page (Leaflet, loaded on demand). */
import { $, api, date, dfDay, dfTime, errorBox, fill, h, icon, main, nf, plural, store } from './core.js';
import { S, applyFilter, filterParams, go, toast, toastError } from './state.js';
import { modal } from './actions.js';
import { editPhotos } from './editing.js';
import { details, getDetail, openLightbox, syncLightbox } from './lightbox.js';

export const M = { data: null, map: null, token: 0 };

export function loadLeaflet(cluster) {
  const css = (href) => document.head.append(h('link', { rel: 'stylesheet', href }));
  const js = (src) => new Promise((ok, fail) => document.head.append(h('script', { src, onload: ok, onerror: () => fail(new Error('offline')) })));
  S.leafletP ||= (css('https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'), js('https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'));
  if (!cluster) return S.leafletP;
  S.clusterP ||= S.leafletP.then(() => {
    css('https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css');
    css('https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css');
    return js('https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js');
  });
  return S.clusterP;
}
// tile provider from the server (OpenStreetMap, or Google Maps with the owner's key); fetched once, OSM until it arrives


export async function loadMaps() { if (S.mapsLoaded) return S.MAPS; try { S.MAPS = await api('/api/maps'); if (S.MAPS.error) toast(S.MAPS.error, null, null, 8000); } catch { /* keep OSM */ } S.mapsLoaded = true; return S.MAPS; }
export const tiles = () => window.L.tileLayer(S.MAPS.url, { maxZoom: S.MAPS.maxZoom, attribution: S.MAPS.attribution });
/** Map and Satellite base layers with a switcher; the choice is remembered. Returns the layer shown. */
export function baseLayers(map, position = 'topright') {
  const L = window.L, road = tiles();
  const sat = S.MAPS.satellite ? L.tileLayer(S.MAPS.satellite.url, { maxZoom: S.MAPS.satellite.maxZoom, attribution: S.MAPS.satellite.attribution }) : null;
  const first = sat && store.get('map-layer', 'map') === 'satellite' ? sat : road;
  first.addTo(map);
  if (sat) {
    L.control.layers({ Map: road, Satellite: sat }, null, { position, collapsed: false }).addTo(map);
    map.on('baselayerchange', (e) => store.set('map-layer', e.layer === sat ? 'satellite' : 'map'));
  }
  return first;
}
/** Street View at a spot: embedded when the server has a Google key with the Maps Embed API, else Google Maps in a new tab. */
export function streetView(lat, lon, title) {
  const sv = S.MAPS.streetview || {};
  const at = (u) => u.replaceAll('{lat}', lat).replaceAll('{lon}', lon);
  if (!sv.embed) { window.open(at(sv.link || 'https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lon}'), '_blank', 'noopener'); return; }
  const back = modal(title || 'Street View', h('iframe', { class: 'svframe', src: at(sv.embed), allowfullscreen: true, loading: 'lazy', referrerpolicy: 'no-referrer-when-downgrade' }),
    h('div', { class: 'muted', style: 'font-size:12px;margin-top:6px' }, 'Where Google has no Street View imagery nearby, the frame stays gray. ',
      h('a', { class: 'lnk', href: at(sv.link), target: '_blank', rel: 'noopener' }, 'Open in Google Maps')),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close')));
  back.querySelector('.modal').classList.add('svmodal');
}
export const mapLink = (lat, lon) => S.MAPS.link.replaceAll('{lat}', lat).replaceAll('{lon}', lon);

export async function showMap(fp) {
  const token = ++M.token;
  M.data = null;
  M.map?.remove(); M.map = null;
  const el = h('div', { id: 'map' });
  const bar = h('div', { class: 'mapbar' });
  main.replaceChildren(el, bar);
  let d;
  try { [d] = await Promise.all([api('/api/map?' + fp.toString()), loadLeaflet(true)]); } catch {
    if (token === M.token && S.route.view === 'map') main.replaceChildren(h('div', { class: 'empty' }, 'The map needs an internet connection for map tiles. Everything else works offline.'));
    return;
  }
  if (token !== M.token || S.route.view !== 'map') return;
  const L = window.L;
  M.data = d;
  await loadMaps();
  const map = M.map = L.map(el, { worldCopyJump: true }).setView([30, 0], 2);
  baseLayers(map, 'bottomleft');  // the top right belongs to the placing tray
  const dot = L.divIcon({ className: '', html: '<div class="pm"></div>', iconSize: [14, 14], iconAnchor: [7, 7] });
  const group = L.markerClusterGroup({ chunkedLoading: true, maxClusterRadius: 55, showCoverageOnHover: false, zoomToBoundsOnClick: false });
  const marker = (dd, i) => L.marker([dd.lat[i], dd.lon[i]], { icon: dot, idx: i, draggable: !!M.placing }).on('click', () => openLightbox(M.data, i, true))
    .on('dragend', (ev) => moveMarker(ev.target, dd, i));
  group.addLayers(d.id.map((id, i) => marker(d, i)));
  M.group = group;
  // the library changed (an edit here, digiKam, a background job): new markers, same view, tray and pin untouched
  M.update = async () => {
    if (token !== M.token || S.route.view !== 'map') return;
    let nd; try { nd = await api('/api/map?' + fp.toString()); } catch { return; }
    if (token !== M.token) return;
    M.data = nd; d = nd;
    group.clearLayers();
    group.addLayers(nd.id.map((id, i) => marker(nd, i)));
    const c = bar.querySelector('.mcount'); if (c) c.textContent = `${nf.format(nd.id.length)} with location`;
  };
  // clicking a bubble offers all of its photos at once instead of forcing a drill-down
  group.on('clusterclick', (ev) => {
    const idxs = ev.layer.getAllChildMarkers().map((m) => m.options.idx).sort((a, b) => a - b);
    const b = ev.layer.getBounds();
    const openAll = () => {
      map.closePopup();
      const q = filterParams();
      q.delete('bbox'); q.delete('ids');
      if (idxs.length <= 600) q.set('ids', idxs.map((i) => d.id[i]).join(','));
      else q.set('bbox', [b.getSouth() - 1e-4, b.getWest() - 1e-4, b.getNorth() + 1e-4, b.getEast() + 1e-4].map((x) => x.toFixed(5)).join(','));
      go('photos', q);
    };
    const el = h('div', { class: 'mpop' },
      h('div', { class: 'mthumbs' }, idxs.slice(0, 8).map((i) => h('img', { src: `/thumb/${d.id[i]}`, alt: '', onclick: () => { map.closePopup(); openLightbox(M.data, i, true); } }))),
      h('div', { class: 'mbtns' },
        h('button', { class: 'pillbtn on', onclick: openAll }, icon('photo'), `Open all ${nf.format(idxs.length)} photos`),
        h('button', { class: 'pillbtn', onclick: () => { map.closePopup(); ev.layer.zoomToBounds({ padding: [30, 30] }); } }, 'Zoom in')));
    L.popup({ maxWidth: 360, minWidth: 300, closeButton: false }).setLatLng(ev.layer.getLatLng()).setContent(el).openOn(map);
  });
  map.addLayer(group);
  if (d.id.length) map.fitBounds(L.latLngBounds(d.id.map((_, i) => [d.lat[i], d.lon[i]])).pad(0.1), { maxZoom: 15 });
  const trailDay = fp.get('trail') || (fp.get('from') && fp.get('from') === fp.get('to') ? fp.get('from') : null);
  if (trailDay) api(`/api/trail?day=${trailDay}`).then((t) => {
    if (token !== M.token || t.id.length < 2) return;
    const pts = t.id.map((_, i) => [t.lat[i], t.lon[i]]);
    L.polyline(pts, { color: '#ea4335', weight: 3, opacity: .8, dashArray: '6 6' }).addTo(map);
    L.circleMarker(pts[0], { radius: 7, color: '#34a853', fillOpacity: 1 }).bindTooltip('Start · ' + dfTime.format(date(t.ts[0]))).addTo(map);
    L.circleMarker(pts[pts.length - 1], { radius: 7, color: '#ea4335', fillOpacity: 1 }).bindTooltip('End · ' + dfTime.format(date(t.ts[t.ts.length - 1]))).addTo(map);
    const km = pts.reduce((a, p, i) => i ? a + Math.hypot(p[0] - pts[i - 1][0], (p[1] - pts[i - 1][1]) * Math.cos(p[0] * Math.PI / 180)) * 111.2 : 0, 0);
    bar.append(h('span', { class: 'pillbtn', style: 'cursor:default' }, `Trail: ${t.id.length} photos, ${km.toFixed(1)} km, ${dfTime.format(date(t.ts[0]))} to ${dfTime.format(date(t.ts[t.ts.length - 1]))}`));
    map.fitBounds(L.latLngBounds(pts).pad(0.15));
  }).catch(() => {});
  if (S.CAN_MANAGE) bar.append(h('button', { class: 'pillbtn' + (M.placing ? ' on' : ''), title: 'Put photos without a location on the map: select them in the tray, then click the map or drag them onto it. Markers can then be dragged to a better spot.',
    onclick: () => { M.placing = !M.placing; showMap(fp); } }, icon('place'), M.placing ? 'Done placing' : 'Place photos'));
  bar.append(h('span', { class: 'pillbtn mcount', style: 'cursor:default' }, `${nf.format(d.id.length)} with location`),
    h('button', { class: 'pillbtn', onclick: () => {
      const b = map.getBounds();
      applyFilter((p) => p.set('bbox', [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((x) => x.toFixed(4)).join(',')), 'photos');
    } }, icon('photo'), 'Show photos in this area'));
  if (!M.placing) bar.prepend(mapSearch(map));  // (placing has the same search in its tray)
  if (M.placing && S.CAN_MANAGE) placeTray(map, fp, el);
  syncLightbox();
}
/** Find a place on the map: a town, landmark or address (the server's place search, offline towns too); the map goes
 *  there, with a pin until the next search. */
function mapSearch(map) {
  const L = window.L;
  let timer = 0, pin = null;
  const results = h('div', { class: 'alist mapsr', hidden: true });
  const go = (x) => {
    results.hidden = true; input.value = x.name;
    if (pin) map.removeLayer(pin);
    pin = L.marker([x.lat, x.lon], { icon: L.divIcon({ className: '', html: '<div class="fpin"></div>', iconSize: [30, 42], iconAnchor: [15, 40] }) })
      .addTo(map).bindTooltip(x.name);
    map.setView([x.lat, x.lon], Math.min(Math.max(x.zoom || 12, 11), 17));
  };
  const look = async () => {
    const q = input.value.trim();
    if (q.length < 2) { results.hidden = true; return; }
    let r; try { r = await api('/api/geocode?q=' + encodeURIComponent(q)); } catch (e) { toastError(e); return; }
    if (input.value.trim() !== q) return;
    results.hidden = false;
    fill(results, r.results.length ? r.results.map((x) => h('div', { class: 'frow wrapbtn', onclick: () => go(x) },
      icon('place'), h('span', { class: 'nm wrapbtn' }, x.name, x.detail ? h('span', { class: 'muted' }, ' · ' + x.detail) : null)))
      : h('div', { class: 'muted', style: 'padding:6px 8px' }, 'Nothing found.'));
  };
  // Enter goes straight to the best match
  const input = h('input', { class: 'einput', type: 'search', placeholder: 'Find a place, e.g. Amsterdam', 'aria-label': 'Find a place on the map',
    onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); look().then(() => results.querySelector('.frow')?.click()); } if (e.key === 'Escape') results.hidden = true; },
    oninput: () => { clearTimeout(timer); timer = setTimeout(look, 450); } });
  return h('div', { class: 'mapfind' }, icon('search'), input, results);
}
/** A marker dragged to a new spot (placing mode): the photo's location follows, with Undo. */
async function moveMarker(mk, d, i) {
  // the position before, at full precision and with its altitude (the map's marker list is rounded and has none):
  // the move keeps the altitude, and Undo puts back exactly what was there
  let det = null;
  try { details.delete(d.id[i]); det = await getDetail(d.id[i]); } catch { /* the marker's own position below */ }
  const was = det && det.lat != null ? { lat: det.lat, lon: det.lon, alt: det.alt ?? null } : { lat: d.lat[i], lon: d.lon[i], alt: null };
  const to = mk.getLatLng();
  if (!(await editPhotos([d.id[i]], { gps: { lat: +to.lat.toFixed(7), lon: +to.lng.toFixed(7), alt: was.alt } }))) { mk.setLatLng([was.lat, was.lon]); return; }
  d.lat[i] = to.lat; d.lon[i] = to.lng;
  toast('Location moved.', 'Undo', async () => { if (await editPhotos([d.id[i]], { gps: was })) { mk.setLatLng([was.lat, was.lon]); d.lat[i] = was.lat; d.lon[i] = was.lon; } }, 8000);
}
/** The tray of photos without a location: pick some, then click the map or drag them onto it. */
async function placeTray(map, fp, el) {
  const L = window.L;
  const q = new URLSearchParams(fp); q.set('geo', '0'); q.delete('bbox');
  const tray = h('aside', { class: 'maptray' }, h('div', { class: 'muted' }, 'Loading the photos without a location…'));
  el.parentElement.append(tray);
  let d;
  try { d = await api('/api/photos?' + q.toString()); } catch (e) { fill(tray, errorBox(e)); return; }
  const sel = new Set();
  let last = -1, shown = 240;
  const count = h('b');
  const hint = h('div', { class: 'muted small' });
  const grid = h('div', { class: 'tgrid' });
  const ids = d.id.slice();
  const refreshHead = () => { fill(count, sel.size ? `${nf.format(sel.size)} selected` : `${nf.format(ids.length)} without a location`); fill(hint, sel.size ? 'Click the map where they were taken, or drag them onto it.' : 'Click photos to select them (Shift for a range), or drag one straight onto the map.'); };
  const tile = (id, k) => {
    const im = h('img', { src: `/thumb/${id}`, alt: '', loading: 'lazy', draggable: 'true', title: d.ts ? dfDay.format(date(d.ts[d.id.indexOf(id)])) : '',
      class: sel.has(id) ? 'on' : '',
      onclick: (e) => {
        if (e.shiftKey && last >= 0) { const [a, b] = [Math.min(last, k), Math.max(last, k)]; for (let j = a; j <= b; j++) sel.add(ids[j]); }
        else sel.has(id) ? sel.delete(id) : sel.add(id);
        last = k; draw();
      },
      ondragstart: (e) => { if (!sel.has(id)) { sel.clear(); sel.add(id); draw(); } e.dataTransfer.setData('text/plain', 'iopix-place'); e.dataTransfer.effectAllowed = 'move'; } });
    return im;
  };
  const draw = () => { refreshHead(); grid.replaceChildren(...ids.slice(0, shown).map(tile), ids.length > shown ? h('button', { class: 'pillbtn', onclick: () => { shown += 240; draw(); } }, `Show ${nf.format(Math.min(240, ids.length - shown))} more`) : ''); };
  const place = async (latlng) => {
    if (!sel.size) { toast('Select photos in the tray first.'); return; }
    const picked = [...sel], at = { lat: +latlng.lat.toFixed(7), lon: +latlng.lng.toFixed(7), alt: null };
    if (townOnly.checked) at.accuracy = +accSel.value;  // approximate: the town's center, how far off it may be
    if (!(await editPhotos(picked, { gps: at }))) return;
    for (const id of picked) { const k = ids.indexOf(id); if (k >= 0) ids.splice(k, 1); }
    sel.clear(); draw();
    const pin = (at.accuracy ? L.circle([at.lat, at.lon], { radius: at.accuracy, color: '#c77d00', weight: 1, fillOpacity: 0.12 })
      : L.circleMarker([at.lat, at.lon], { radius: 9, color: '#1a73e8', fillOpacity: .6 })).addTo(map).bindTooltip(`${picked.length} placed${at.accuracy ? ' (approximate)' : ''}`);
    toast(`${plural(picked.length, 'photo')} placed.`, 'Undo', async () => { if (await editPhotos(picked, { gps: null })) { map.removeLayer(pin); ids.unshift(...picked); draw(); } }, 10000);
  };
  map.on('click', (e) => place(e.latlng));
  map.on('contextmenu', (e) => found({ lat: +e.latlng.lat.toFixed(6), lon: +e.latlng.lng.toFixed(6), name: 'This spot', detail: `${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}`, zoom: map.getZoom() }));
  el.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; });
  el.addEventListener('drop', (e) => { e.preventDefault(); if (e.dataTransfer.getData('text/plain') === 'iopix-place') place(map.mouseEventToLatLng(e)); });
  el.style.cursor = 'crosshair';
  // only the town is known: the photos go to the town's center, saved as approximate (no altitude, an accuracy radius)
  const townOnly = h('input', { type: 'checkbox', onchange: () => { accSel.disabled = !townOnly.checked; search.placeholder = townOnly.checked ? 'Find the town' : 'Find a place, landmark or address'; } });
  const accSel = h('select', { class: 'sharepick', disabled: true, title: 'How far from the spot the photos may have been taken' },
    [[1000, '~1 km'], [2000, '~2 km'], [5000, '~5 km'], [10000, '~10 km'], [25000, '~25 km']].map(([v, l]) => h('option', { value: v, selected: v === 2000 }, l)));
  // find a place: a name, landmark or address (the same search as Set location), then the map zooms there
  let st = 0;
  const results = h('div', { class: 'alist', hidden: true, style: 'max-height:180px' });
  const search = h('input', { class: 'einput', placeholder: 'Find a place, landmark or address',
    onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); clearTimeout(st); look(); } if (e.key === 'Escape') results.hidden = true; },
    oninput: () => { clearTimeout(st); st = setTimeout(look, 500); } });
  const look = async () => {
    const q = search.value.trim();
    if (q.length < 2) { results.hidden = true; return; }
    let r; try { r = await api('/api/geocode?q=' + encodeURIComponent(q)); } catch (e) { toastError(e); return; }
    if (search.value.trim() !== q) return;
    results.hidden = false;
    fill(results, r.results.length ? r.results.map((x) => h('div', { class: 'frow wrapbtn', onclick: () => { results.hidden = true; search.value = x.name; found(x); } },
      icon('place'), h('span', { class: 'nm wrapbtn' }, x.name, x.detail ? h('span', { class: 'muted' }, ' · ' + x.detail) : null)))
      : h('div', { class: 'muted', style: 'padding:6px 8px' }, 'Nothing found.'));
  };
  // the place found: close in (street level for an address or landmark), with a pin that takes the photos
  let pin = null;
  const found = (x) => {
    if (pin) map.removeLayer(pin);
    const icon_ = L.divIcon({ className: '', html: '<div class="fpin"></div>', iconSize: [30, 42], iconAnchor: [15, 40] });
    pin = L.marker([x.lat, x.lon], { icon: icon_, zIndexOffset: 1000 }).addTo(map);
    const label = h('div', { class: 'fpinlabel' }, h('b', null, x.name), x.detail ? h('div', { class: 'muted' }, x.detail) : null,
      h('div', { class: 'toolbar tight mt6',},
        h('button', { class: 'pillbtn on', onclick: (e) => { e.stopPropagation(); place(L.latLng(x.lat, x.lon)); } }, icon('place'), 'Put the selected photos here'),
        h('button', { class: 'pillbtn', onclick: (e) => { e.stopPropagation(); streetView(x.lat, x.lon, x.name); } }, 'Street View')),
      h('div', { class: 'muted', style: 'font-size:11.5px;margin-top:4px' }, 'Right-click (or long-press) elsewhere to move the pin.'));
    pin.bindPopup(label, { closeButton: false, offset: [0, -34] }).openPopup();
    pin.on('click', (e) => { L.DomEvent.stopPropagation(e); if (sel.size) place(e.latlng); else pin.openPopup(); });
    map.setView([x.lat, x.lon], x.name === 'This spot' ? map.getZoom() : townOnly.checked ? 12 : Math.max(x.zoom || 0, 18));  // a town: all of it
  };
  // thumbnail size, remembered
  const px = () => +store.get('tray-size', '70');
  const sizer = h('input', { type: 'range', min: 60, max: 200, step: 10, value: px(), title: 'Thumbnail size', style: 'flex:1',
    oninput: () => { store.set('tray-size', sizer.value); grid.style.setProperty('--tsz', sizer.value + 'px'); } });
  grid.style.setProperty('--tsz', px() + 'px');
  fill(tray, h('div', { class: 'mapsearch' }, search, results),
    h('label', { class: 'mttown', title: 'Find the town, then put the photos on its pin: they are saved as approximate' }, townOnly, 'Only the town is known', accSel), h('div', { class: 'mthead' }, count, h('span', { class: 'sp' }), h('button', { class: 'pillbtn', onclick: () => { ids.slice(0, shown).forEach((x) => sel.add(x)); draw(); } }, 'Select shown'),
    h('button', { class: 'pillbtn', onclick: () => { sel.clear(); draw(); } }, 'Clear')), hint,
    h('label', { class: 'mtsize' }, icon('photo'), sizer, icon('photo')), grid);
  draw();
}
