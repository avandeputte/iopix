/** The filter drawer: browse every metadata facet. */
import { $, api, fill, h, icon, isoDay, nf, store } from './core.js';
import { FILTER_VIEWS, S, applyFilter, faceUrl, filterParams, go, setOne, toggle } from './state.js';

export const expanded = new Set();
export function toggleFilters(force, remember = true) {
  const el = $('#filters');
  const show = force != null ? !!force : el.hidden;
  if (show && !FILTER_VIEWS.includes(S.route.view)) { location.hash = '#/photos'; }  // the filters act on photos: go there with the pane open
  el.hidden = !show;
  document.body.classList.toggle('filters-open', show);
  $('#filterbtn').classList.toggle('on', show);
  if (remember) store.set('filters', show ? '1' : '0');
  if (show) renderFilters();
}

export async function renderFilters() {
  const el = $('#filters');
  const p = filterParams();
  const token = ++S.facetToken;
  let f;
  try { f = await api('/api/facets?' + p.toString()); } catch { return; }
  if (token !== S.facetToken) return;
  drawFilters(el, f, p);
}
/** The pane from facets already fetched; "All N" and "Show less" only draw it again. */
function drawFilters(el, f, p) {
  const has = (k, v) => p.getAll(k).includes(String(v));
  const frow = (label, count, on, onclick, extra) => h('div', { class: 'frow' + (on ? ' on' : ''), onclick, title: label }, extra, h('span', { class: 'nm' }, label),
    h('span', { class: 'ct' }, count != null ? nf.format(count) : ''));
  const section = (title, rows, limit = 8) => {
    if (!rows.length) return null;
    const open = expanded.has(title);
    return [h('h3', null, title, rows.length > limit ? h('span', { class: 'more', onclick: () => { open ? expanded.delete(title) : expanded.add(title); drawFilters(el, f, p); } },
      open ? 'Show less' : `All ${rows.length}`) : null), ...(open ? rows : rows.slice(0, limit))];
  };
  const multi = (key, list, labelOf = (v) => v, extraOf) => list.filter(([v]) => v != null).map(([v, n]) =>
    frow(labelOf(v), n, has(key, v), () => applyFilter((q) => toggle(q, key, v)), extraOf?.(v)));
  const tagRows = (person) => f.tags.filter(([id]) => !!S.tagById.get(id)?.person === person).map(([id, n]) => {
    const t = S.tagById.get(id);
    return t && frow(person ? t.name : t.full.split('/').slice(-2).join(' › '), n, has('tag', id), () => applyFilter((q) => toggle(q, 'tag', id)),
      person ? h('img', { src: faceUrl(id), loading: 'lazy', alt: '' }) : null);
  }).filter(Boolean);
  const range = (label, key, lohi, step) => h('div', { class: 'fpair' }, h('label', null, label), ...['min', 'max'].map((m, i) =>
    h('input', { type: 'number', step, placeholder: lohi[i] != null ? `${m} ${+(+lohi[i]).toFixed(1)}` : m, value: p.get(`${key}_${m}`) || '',
      onchange: (e) => applyFilter((q) => { e.target.value ? q.set(`${key}_${m}`, e.target.value) : q.delete(`${key}_${m}`); }) })));
  const dateIn = (label, key, ph) => h('div', { class: 'fpair' }, h('label', null, label),
    h('input', { type: 'date', value: p.get(key) || '', title: ph, onchange: (e) => applyFilter((q) => { e.target.value ? q.set(key, e.target.value) : q.delete(key); }) }));
  const rating = +(p.get('rating') || 0);
  const scroll = el.scrollTop;
  fill(el,
    h('div', { class: 'fhead' }, h('span', null, 'Filters'), h('span', { class: 'muted', style: 'margin-left:10px;font-size:13px' }, `${nf.format(f.count)} items`), h('span', { class: 'sp' }),
      [...p.keys()].length ? h('button', { type: 'button', class: 'lnk', style: 'font-size:13px;margin-right:6px', onclick: () => go(S.route.view === 'map' ? 'map' : 'photos', new URLSearchParams()) }, 'Clear all') : null,
      h('button', { class: 'iconbtn', title: 'Close', onclick: () => toggleFilters(false) }, icon('close'))),
    h('h3', null, 'Type'),
    frow('Photos', f.count - f.videos, p.get('type') === 'photo', () => applyFilter((q) => setOne(q, 'type', 'photo'))),
    frow('Videos', f.videos, p.get('type') === 'video', () => applyFilter((q) => setOne(q, 'type', 'video'))),
    h('h3', null, 'Rating at least'),
    h('div', { class: 'stars' }, [1, 2, 3, 4, 5].map((n) => h('button', { class: n <= rating ? 'on' : '', title: `${n} star${n > 1 ? 's' : ''} and up`,
      onclick: () => applyFilter((q) => setOne(q, 'rating', n)) }, icon('star')))),
    h('h3', null, 'Date'),
    dateIn('From', 'from', f.tsMin != null ? isoDay(f.tsMin) : ''), dateIn('Until', 'to', f.tsMax != null ? isoDay(f.tsMax) : ''),
    section('Year', multi('year', f.years), 6),
    section('People', tagRows(true)),
    section('Tags', [...tagRows(false), frow('Untagged', null, p.get('untagged') === '1', () => applyFilter((q) => setOne(q, 'untagged', '1')))]),
    section('Folder', f.albums.map(([id, n]) => S.albumById.get(id) && frow(S.albumById.get(id).path.replace(/^\//, ''), n, has('album', id),
      () => applyFilter((q) => toggle(q, 'album', id)))).filter(Boolean), 6),
    h('h3', null, 'Location'),
    frow('Favorites', null, p.get('fav') === '1', () => applyFilter((q) => setOne(q, 'fav', '1')), icon('heart')),
    frow('Has location', f.geotagged, p.get('geo') === '1', () => applyFilter((q) => setOne(q, 'geo', '1'))),
    frow('No location', f.count - f.geotagged, p.get('geo') === '0', () => applyFilter((q) => setOne(q, 'geo', '0'))),
    f.geotagged ? frow('Show these on the map', null, false, () => go('map', p), icon('place')) : null,
    section('Country', multi('country', f.countries), 5),
    section('Place', f.places.map(([city, region, country, n]) => frow(`${city}${region ? ', ' + region : ''}`, n, p.get('place') === city,
      () => applyFilter((q) => { if (q.get('place') === city) { q.delete('place'); q.delete('region'); } else { q.set('place', city); region ? q.set('region', region) : q.delete('region'); } }))), 8),
    section('Camera', f.cameras.map(([v, n]) => frow(v ?? 'Unknown camera', n, has('camera', v ?? '~'), () => applyFilter((q) => toggle(q, 'camera', v ?? '~')))), 6),
    section('Lens', multi('lens', f.lenses), 6),
    section('Quality', [
      f.extras.sharpest ? frow('Sharpest fifth', f.extras.sharpest, p.get('sharpest') === '1', () => applyFilter((q) => setOne(q, 'sharpest', '1'))) : null,
      f.extras.blurry ? frow('Blurry (least sharp tenth)', f.extras.blurry, p.get('blurry') === '1', () => applyFilter((q) => setOne(q, 'blurry', '1'))) : null,
      f.extras.dark ? frow('Very dark', f.extras.dark, p.get('exposure') === 'dark', () => applyFilter((q) => setOne(q, 'exposure', 'dark'))) : null,
      f.extras.bright ? frow('Very bright', f.extras.bright, p.get('exposure') === 'bright', () => applyFilter((q) => setOne(q, 'exposure', 'bright'))) : null,
      f.extras.hastext ? frow('Contains text', f.extras.hastext, p.get('hastext') === '1', () => applyFilter((q) => setOne(q, 'hastext', '1'))) : null,
    ].filter(Boolean), 9),
    section('Bursts', f.extras.bursts ? [
      frow('In a burst', f.extras.bursts, p.get('bursts') === '1', () => applyFilter((q) => setOne(q, 'bursts', '1'))),
      frow('Show only the best frame of each burst', null, p.get('bestonly') === '1', () => applyFilter((q) => setOne(q, 'bestonly', '1'))),
      frow('Review these bursts', null, false, () => go('bursts', p), icon('dup'))] : [], 9),
    section('People in the picture', f.npeople.sort((a, b) => a[0] - b[0]).reduce((acc, [n, c]) => { const k = n >= 5 ? '5+' : String(n); const e = acc.find((x) => x[0] === k); e ? e[1] += c : acc.push([k, c]); return acc; }, [])
      .map(([k, c]) => frow(k === '0' ? 'No people' : k === '1' ? '1 person' : `${k} people`, c, p.get('npeople') === k, () => applyFilter((q) => setOne(q, 'npeople', k)))), 8),
    section('Objects', multi('obj', f.objects), 8),
    h('h3', null, 'Exposure'),
    range('ISO', 'iso', f.iso, 1), range('Aperture ƒ/', 'ap', f.aperture, 0.1), range('Focal mm', 'fl', f.focal, 1),
    section('Photographer', multi('creator', f.creators), 5),
    section('File type', multi('fmt', f.formats), 10),
  );
  el.scrollTop = scroll;
}
