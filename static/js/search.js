/** The search box with its suggestions, the filter chips and the breadcrumbs. */
import { $, MONTHS, h, icon, nf, norm } from './core.js';
import { S, ST, applyFilter, faceUrl, filterParams, go, hashFor, loadConcepts, photosWith, savedSearchFor, ualbumById } from './state.js';
import { people } from './pages.js';
import { toggleFilters } from './filters.js';

export function chipList(p) {
  const out = [];
  const add = (key, value, label, opt = {}) => out.push({ key, value, label, ...opt });
  for (const [k, v] of p) {
    if (k === 'q') add(k, v, `“${v}”`, { ic: 'text' });
    else if (k === 'tag' || k === 'nottag') {
      const t = S.tagById.get(+v);
      add(k, v, (k === 'nottag' ? 'Not ' : '') + (t ? t.name : `Tag ${v}`), t?.person ? { img: faceUrl(+v) } : { ic: 'tag' });
    } else if (k === 'album') add(k, v, S.albumById.get(+v)?.name || `Folder ${v}`, { ic: 'folder' });
    else if (k === 'year') add(k, v, v, { ic: 'calendar' });
    else if (k === 'month') add(k, v, MONTHS[v - 1] || v, { ic: 'calendar' });
    else if (k === 'from') add(k, v, `From ${v}`, { ic: 'calendar' });
    else if (k === 'to') add(k, v, `Until ${v}`, { ic: 'calendar' });
    else if (k === 'md') add(k, v, `On ${MONTHS[+v.slice(0, 2) - 1]} ${+v.slice(3)}, any year`, { ic: 'calendar' });
    else if (k === 'camera' || k === 'lens') add(k, v, v === '~' ? `No ${k}` : v, { ic: 'camera' });
    else if (k === 'fmt') add(k, v, v);
    else if (k === 'creator') add(k, v, `By ${v}`, { ic: 'copyright' });
    else if (k === 'rating') add(k, v, `${v}★ and up`, { ic: 'star' });
    else if (k === 'type') add(k, v, v === 'video' ? 'Videos' : 'Photos only', { ic: v === 'video' ? 'video' : 'photo' });
    else if (k === 'geo') add(k, v, v === '1' ? 'Has location' : 'No location', { ic: 'place' });
    else if (k === 'untagged') add(k, v, 'Untagged', { ic: 'tag' });
    else if (k === 'bbox') add(k, v, 'Map area', { ic: 'place' });
    else if (k === 'trail') add(k, v, `Trail for ${v}`, { ic: 'map' });
    else if (k === 'faces') add(k, v, { unknown: 'Unnamed faces', suggested: 'Face suggestions', ignored: 'Ignored faces', confirmed: 'Named faces' }[v] || v, { ic: 'face' });
    else if (k === 'fav') add(k, v, 'Favorites', { ic: 'heart' });
    else if (k === 'ualbum') add(k, v, ualbumById(v)?.name || 'Album', { ic: 'album' });
    else if (k === 'place' || k === 'region' || k === 'country') add(k, v, v, { ic: 'place' });
    else if (k === 'sem') add(k, v, `Looks like “${v}”`, { ic: 'spark' });
    else if (k === 'like') add(k, v, 'Similar photos', { img: `/thumb/${v}` });
    else if (k === 'concept') add(k, v, `Detected: ${v}`, { ic: 'spark' });
    else if (k === 'blurry') add(k, v, 'Blurry', { ic: 'spark' });
    else if (k === 'sharpest') add(k, v, 'Sharpest', { ic: 'spark' });
    else if (k === 'exposure') add(k, v, v === 'dark' ? 'Very dark' : 'Very bright', { ic: 'spark' });
    else if (k === 'hastext') add(k, v, 'Contains text', { ic: 'text' });
    else if (k === 'bursts') add(k, v, 'In a burst', { ic: 'dup' });
    else if (k === 'bestonly') add(k, v, 'Best frame of each burst', { ic: 'dup' });
    else if (k === 'burst') add(k, v, 'One burst', { ic: 'dup' });
    else if (k === 'obj') add(k, v, `Shows: ${v}`, { ic: 'spark' });
    else if (k === 'npeople') add(k, v, v === '0' ? 'No people' : `${v} ${v === '1' ? 'person' : 'people'}`, { ic: 'person' });
    else if (k === 'dupes') add(k, v, 'Duplicates', { ic: 'dup' });
    else if (k === 'edited') add(k, v, 'Edited here, not yet in digiKam', { ic: 'edit' });
    else if (k === 'ids') add(k, v, `${nf.format(v.split(',').length)} chosen photos`, { ic: 'place' });
    else if (/^(iso|ap|fl|exp)_(min|max)$/.test(k)) {
      const [f, m] = k.split('_');
      add(k, v, `${{ iso: 'ISO', ap: 'ƒ/', fl: 'Focal mm', exp: 'Exposure s' }[f]} ${m === 'min' ? '≥' : '≤'} ${v}`, { ic: 'camera' });
    }
  }
  return out;
}
export function renderChips() {
  const p = filterParams();
  const chips = chipList(p);
  $('#chips').replaceChildren(...chips.map((c) => h('span', { class: 'chip', title: c.label, 'data-k': c.key },
    c.img ? h('img', { src: c.img, alt: '' }) : null,
    h('span', { class: 't' }, c.label),
    h('button', { title: 'Remove', onclick: () => applyFilter((q) => {
      const rest = q.getAll(c.key).filter((v) => v !== c.value);
      q.delete(c.key);
      rest.forEach((v) => q.append(c.key, v));
    }) }, icon('close')))));
  $('#clear').hidden = !chips.length && !$('#q').value;
  $('#q').placeholder = chips.length ? 'Add to search…' : 'Search people, tags, folders, cameras, captions…';
}


export function suggestions(text) {
  const words = norm(text).split(/\s+/).filter(Boolean);
  const out = [{ ic: 'search', label: `Search everything for “${text.trim()}”`, kind: 'Text', act: (p) => p.append('q', text.trim()) }];
  if (!words.length || !S.META) return [];
  if (S.SEM.enabled) { loadConcepts(); out.push({ ic: 'spark', label: `Photos that look like “${text.trim()}”`, kind: 'Content', act: (p) => { p.delete('like'); p.set('sem', text.trim()); } }); }
  const score = (n) => {
    if (!words.every((w) => n.includes(w))) return -1;
    return n.startsWith(words[0]) ? 3 : new RegExp(`(^|[^a-z0-9])${words[0].replace(/[^\w]/g, '.')}`).test(n) ? 2 : 1;
  };
  const top = (list, limit, key, count) => list.map((x) => [score(x[key]), x]).filter((s) => s[0] > 0)
    .sort((a, b) => b[0] - a[0] || count(b[1]) - count(a[1])).slice(0, limit).map((s) => s[1]);
  const m = /^((?:19|20)\d\d)$/.exec(text.trim());
  if (m) out.push({ ic: 'calendar', label: m[1], kind: 'Year', act: (p) => p.set('year', m[1]) });
  const mm = /^([a-z]{3,})\.?(?:\s+((?:19|20)\d\d))?$/i.exec(text.trim());
  if (mm) {
    const mi = MONTHS.findIndex((x) => x.toLowerCase().startsWith(mm[1].toLowerCase()));
    if (mi >= 0) out.push({ ic: 'calendar', label: MONTHS[mi] + (mm[2] ? ' ' + mm[2] : ''), kind: 'Month',
      act: (p) => { p.set('month', mi + 1); if (mm[2]) p.set('year', mm[2]); } });
  }
  for (const c of top(S.CONCEPTS || [], 3, '_n', (c) => c.n))
    out.push({ ic: 'spark', label: c.name, kind: `Detected · ${nf.format(c.n)}`, act: (p) => p.set('concept', c.name) });
  const used = new Set(filterParams().getAll('tag'));
  const tags = S.META.tags.filter((t) => t.nAll && !used.has(String(t.id)));
  for (const t of top(tags.filter((t) => t.person), 6, '_n', (t) => t.nAll))
    out.push({ img: faceUrl(t.id), label: t.name, sub: nf.format(t.nAll), kind: 'Person', act: (p) => p.append('tag', t.id) });
  for (const t of top(tags.filter((t) => !t.person), 6, '_n', (t) => t.nAll))
    out.push({ ic: 'tag', label: t.name, sub: t.full.includes('/') ? t.full.split('/').slice(0, -1).join(' › ') : '', kind: `Tag · ${nf.format(t.nAll)}`, act: (p) => p.append('tag', t.id) });
  for (const a of top(S.META.albums.filter((a) => a.pid != null), 5, '_n', (a) => a.nAll))
    out.push({ ic: 'folder', label: a.name, sub: a.path, kind: `Folder · ${nf.format(a.nAll)}`, act: (p) => { p.delete('album'); p.append('album', a.id); } });
  for (const a of top(ST.albums.map((x) => ({ ...x, _n: norm(x.name) })), 3, '_n', (x) => x.n))
    out.push({ ic: 'album', label: a.name, kind: `Album · ${nf.format(a.n)}`, act: (p) => { p.delete('ualbum'); p.set('ualbum', a.id); } });
  for (const [kind, key, list] of [['Camera', 'camera', S.META.cameras], ['Lens', 'lens', S.META.lenses], ['Creator', 'creator', S.META.creators]])
    for (const c of top(list.map(([name, n]) => ({ name, n, _n: norm(name) })), 3, '_n', (c) => c.n))
      out.push({ ic: kind === 'Creator' ? 'copyright' : 'camera', label: c.name, kind: `${kind} · ${nf.format(c.n)}`, act: (p) => p.append(key, c.name) });
  for (const c of top(S.placeRows, 5, '_n', (c) => c.n))
    out.push({ ic: 'place', label: c.name, sub: c.sub, kind: `Place · ${nf.format(c.n)}`, act: (p) => p.set(c.key, c.name) });
  if (words.length === 1) {
    if ('favorites'.startsWith(words[0])) out.push({ ic: 'heart', label: 'Favorites', kind: 'Filter', act: (p) => p.set('fav', '1') });
    if ('videos'.startsWith(words[0])) out.push({ ic: 'video', label: 'Videos', kind: 'Type', act: (p) => p.set('type', 'video') });
    if ('untagged'.startsWith(words[0])) out.push({ ic: 'tag', label: 'Untagged', kind: 'Filter', act: (p) => p.set('untagged', '1') });
  }
  return out;
}
export function renderSuggest() {
  const box = $('#suggest');
  box.hidden = !S.sgItems.length;
  box.replaceChildren(...S.sgItems.map((s, i) => h('div', { class: 'sg' + (i === S.sgSel ? ' sel' : ''), onmousedown: (e) => { e.preventDefault(); pickSuggestion(i); } },
    s.img ? h('img', { src: s.img, alt: '' }) : icon(s.ic), h('span', null, s.label, s.sub ? h('span', { class: 'sub' }, s.sub) : null),
    h('span', { class: 'kind' }, s.kind))));
  box.querySelector('.sel')?.scrollIntoView({ block: 'nearest' });
}
export function pickSuggestion(i) {
  const s = S.sgItems[i];
  if (!s) return;
  $('#q').value = '';
  S.sgItems = [];
  renderSuggest();
  applyFilter(s.act);
}
export function initSearch() {
  const q = $('#q');
  q.addEventListener('input', () => { S.sgItems = suggestions(q.value); S.sgSel = 0; renderSuggest(); $('#clear').hidden = false; });
  q.addEventListener('focus', () => { if (q.value) { S.sgItems = suggestions(q.value); renderSuggest(); } });
  q.addEventListener('blur', () => { $('#suggest').hidden = true; });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { S.sgSel = Math.min(S.sgSel + 1, S.sgItems.length - 1); renderSuggest(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { S.sgSel = Math.max(S.sgSel - 1, 0); renderSuggest(); e.preventDefault(); }
    else if (e.key === 'Enter') {
      if (!q.value.trim()) return;
      if (S.sgItems.length) pickSuggestion(S.sgSel);
      else { const text = q.value.trim(); q.value = ''; renderSuggest(); applyFilter((p) => p.append('q', text)); }  // no list yet: a text search
    }
    else if (e.key === 'Escape') { q.value = ''; S.sgItems = []; renderSuggest(); q.blur(); }
    else if (e.key === 'Backspace' && !q.value) {
      const chips = chipList(filterParams());
      const last = chips[chips.length - 1];
      if (last) applyFilter((p) => { const rest = p.getAll(last.key).filter((v) => v !== last.value); p.delete(last.key); rest.forEach((v) => p.append(last.key, v)); });
    }
  });
  $('#clear').onclick = () => { q.value = ''; S.sgItems = []; renderSuggest(); go(S.route.view === 'map' ? 'map' : 'photos', new URLSearchParams()); q.focus(); };
  $('#filterbtn').onclick = () => toggleFilters();
  document.querySelectorAll('[data-ic]').forEach((el) => el.replaceWith(icon(el.dataset.ic)));
}

/** parts: [[label, hash or null]]; the last part is the current location. */
/** The breadcrumb trail lives in one fixed strip right under the top bar (#crumbbar), so it is in the same
 *  place on every page and stays put while the page scrolls. Views call crumbs(parts) while they render
 *  (it returns null, so it can sit where the trail used to be built); the router clears it on every view change. */
export function crumbs(parts) {
  const bar = $('#crumbbar');
  if (!parts || !parts.length) { bar.hidden = true; bar.replaceChildren(); document.body.classList.remove('crumbed'); return null; }
  bar.replaceChildren(...parts.map(([label, href], i) => [
    i ? h('span', { class: 'sep' }, '›') : null,
    href && i < parts.length - 1 ? h('a', { href }, label) : h('span', { class: i === parts.length - 1 ? 'cur' : '' }, label)]).flat().filter(Boolean));
  bar.hidden = false;
  document.body.classList.add('crumbed');
  return null;
}
export const placeHash = (o) => hashFor('places', photosWith(o));
/** Where the current photo list sits in a hierarchy (album, place, tag, ...), if it does. */
export function photoCrumbs(p) {
  const albums = p.getAll('album'), tags = p.getAll('tag');
  const saved = savedSearchFor(p);
  if (saved) return [['Albums', '#/albums'], [saved.name, null]];
  if (albums.length === 1 && S.albumById.get(+albums[0])) {
    const chain = [];
    for (let a = S.albumById.get(+albums[0]); a; a = a.pid != null ? S.albumById.get(a.pid) : null) chain.unshift(a);  // the folder itself too, even at the top level
    return [['Folders', '#/folders'], ...chain.map((a) => [a.name, hashFor('photos', photosWith({ album: a.id, sort: 'old' }))])];
  }
  if (p.get('ualbum') && ualbumById(p.get('ualbum'))) return [['Albums', '#/albums'], [ualbumById(p.get('ualbum')).name, null]];
  if (p.get('country') || p.get('region') || p.get('place')) {
    let [country, region, place] = [p.get('country'), p.get('region'), p.get('place')];
    const row = S.META.places.find((x) => (!place || x[0] === place) && (!region || x[1] === region) && (!country || x[2] === country));
    if (row) { country ||= row[2]; if (place || region) region ||= row[1]; }
    const out = [['Places', '#/places']];
    if (country) out.push([country, placeHash({ country })]);
    if (region) out.push([region, placeHash({ country, region })]);
    if (place) out.push([place, null]);
    return out;
  }
  if (tags.length === 1 && S.tagById.get(+tags[0])) {
    const chain = [];
    for (let t = S.tagById.get(+tags[0]); t; t = S.tagById.get(t.pid)) chain.unshift(t);
    return [[chain[0].name === 'People' ? 'People' : 'Tags', chain[0].name === 'People' ? '#/people' : '#/tags'],
      ...chain.filter((t, i) => !(i === 0 && t.name === 'People')).map((t) => [t.name, hashFor('photos', photosWith({ tag: t.id }))])];
  }
  if (p.get('concept')) return [['Explore', '#/explore'], ['Detected content', '#/detected'], [p.get('concept'), null]];
  if (p.get('dupes') || p.get('edited')) return [['Explore', '#/explore'], [p.get('dupes') ? 'Duplicates' : 'Edited here', null]];
  return null;
}
