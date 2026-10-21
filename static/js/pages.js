/** The pages: explore, people, places, folders, albums, tags, import and the trash. */
import { $, MONTHS, SETTINGS, api, date, dfDay, dfTime, errorBox, fill, fmtBytes, h, icon, loadingBox, main, nf, norm, plural, post, store } from './core.js';
import { NAV, S, ST, applyBadgePref, applyTheme, avatarOf, faceUrl, filterParams, go, hashFor, loadState, onRoute, photosWith, pollSoon, toast, toastError } from './state.js';
import { crumbs, placeHash } from './search.js';
import { confirmDialog, folderPicker, modal, pickedShared, promptDialog, sharingPick } from './actions.js';
import { detectFaces } from './editing.js';
import { downloadDialog } from './download.js';
import { tiles } from './map.js';
import { openLightbox, personPicker, popMenu, syncLightbox } from './lightbox.js';
import { boot, pollStatus, start } from './main.js';

export function card({ img, fallback, name, sub, onclick }) {
  const im = img ? h('img', { src: img, alt: '', loading: 'lazy' }) : null;
  if (im && fallback) im.onerror = () => { im.onerror = null; im.src = fallback; };
  return h('div', { class: 'card', tabindex: 0, role: 'button', onclick }, h('span', { class: 'cv' }, im), h('div', { class: 'nm', title: name }, name), sub ? h('div', { class: 'sb' }, sub) : null);
}
export const thumbCard = (cover, name, sub, params) => card({ img: cover ? `/thumb/${cover}` : null, name, sub, onclick: () => go('photos', photosWith(params)) });
export const personCard = (t) => card({ img: faceUrl(t.id), fallback: t.cover ? `/thumb/${t.cover}` : null, name: t.name, sub: nf.format(t.nAll), onclick: () => go('photos', photosWith({ tag: t.id })) });
/** Leaf person tags with photos (a person tag with children is a family/group). */
export const people = () => S.META.tags.filter((t) => t.person && t.n > 0 && !t.hasKids).sort((a, b) => b.nAll - a.nAll);

export async function showExplore() {
  const now = new Date();
  const md = `${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  const strip = h('div', { class: 'jrows' });
  const otd = h('div', { hidden: true }, h('h2', { style: '--hue:var(--c-rose)' }, h('span', { class: 'hic' }, icon('calendar')), `On this day · ${MONTHS[now.getMonth()]} ${now.getDate()}`, h('a', { href: hashFor('photos', photosWith({ md })) }, 'See all')), strip);
  const detected = h('div', { hidden: true }), trips = h('div', { hidden: true });
  const rootTags = S.META.tags.filter((t) => t.pid == null && t.nAll && !(t.person && !S.META.tags.some((c) => c.pid === t.id))).sort((a, b) => b.nAll - a.nAll);
  const pill = (label, n, params) => h('a', { class: 'pill', href: hashFor('photos', photosWith(params)) }, label, n != null ? h('b', null, nf.format(n)) : null);
  const count = (list, v) => (list.find((x) => x[0] === v) || [0, 0])[1];
  const ppl = people();
  // the summary: one card per number, each with an icon and its own colour; most open the matching view
  const stat = (hue, ic, value, label, href) => h(href ? 'a' : 'div', { class: 'stat', style: `--hue:var(--c-${hue})`, href },
    h('span', { class: 'sic' }, icon(ic)), h('div', null, h('b', null, value), h('span', null, label)));
  // section headings with a coloured icon; the pill group below a heading shares its hue
  const sec = (hue, ic, title, link, linkLabel) => h('h2', { style: `--hue:var(--c-${hue})` }, h('span', { class: 'hic' }, icon(ic)), title, link ? h('a', { href: link }, linkLabel) : null);
  // Rediscover: three dozen photos picked at random from the whole library, new ones on every visit or with Shuffle
  const rediscover = h('div', { hidden: true });
  const shuffle = async () => {
    let r; try { r = await api('/api/random?n=36'); } catch { return; }
    if (!r.id.length || !rediscover.isConnected) return;
    const tiles = r.id.map((id, i) => h('div', { class: 'tile', style: `--ar:${Math.max(0.5, Math.min(2.2, r.w[i] / (r.h[i] || 1))).toFixed(3)}`, title: dfDay.format(date(r.ts[i])),
      onclick: () => openLightbox({ id: r.id }, i, true) }, h('img', { src: `/thumb/${id}`, alt: '', loading: 'lazy' })));
    fill(rediscover, h('h2', { style: '--hue:var(--c-indigo)' }, h('span', { class: 'hic' }, icon('spark')), 'Rediscover', h('button', { type: 'button', class: 'lnk', onclick: shuffle }, 'Shuffle')), h('div', { class: 'jrows' }, tiles));
    rediscover.hidden = false;
  };
  setTimeout(shuffle, 0);
  // Then and now: a person's earliest and latest named face, years apart; "Others" picks new people
  const thenNow = h('div', { hidden: true });
  const years = (ts) => new Date(ts * 1000).getFullYear();
  const drawThenNow = async () => {
    let r; try { r = await api('/api/thenandnow?n=3'); } catch { return; }
    if (!r.pairs.length || !thenNow.isConnected) return;
    const face = (e, label) => h('button', { type: 'button', class: 'tnface', onclick: () => openLightbox({ id: [e.photo] }, 0, true) },
      h('img', { src: `/facecrop/${e.photo}/${e.rect.join('/')}.jpg`, alt: '', loading: 'lazy' }), h('span', null, label));
    fill(thenNow, h('h2', { style: '--hue:var(--c-amber)' }, h('span', { class: 'hic' }, icon('person')), 'Then and now', h('button', { type: 'button', class: 'lnk', onclick: drawThenNow }, 'Others')),
      h('div', { class: 'tnrow' }, r.pairs.map((p) => h('div', { class: 'tncard' },
        h('div', { class: 'tnfaces' }, face(p.then, String(years(p.then.ts))), h('span', { class: 'tnarrow' }, '→'), face(p.now, String(years(p.now.ts)))),
        h('div', { class: 'tnname' }, h('a', { href: hashFor('photos', photosWith({ tag: p.tag, sort: 'old' })) }, p.name), h('span', { class: 'muted' }, ` · ${p.years} years apart`))))));
    thenNow.hidden = false;
  };
  setTimeout(drawThenNow, 0);
  const group = (hue, ...kids) => h('div', { class: 'pills', style: `--hue:var(--c-${hue})` }, ...kids);
  // the size of the photos in On this day and Rediscover, remembered
  const sizes = [['s', 'S'], ['m', 'M'], ['l', 'L']];
  let jsz = store.get('explore-size', 'm');
  const page = h('div', { class: `page jsz-${jsz}` });
  const seg = h('div', { class: 'eseg', title: 'Photo size', style: 'margin-left:auto' }, sizes.map(([k, l]) => h('button', { class: k === jsz ? 'on' : '', onclick: (e) => {
    jsz = k; store.set('explore-size', k); page.className = `page jsz-${k}`; seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.currentTarget)); } }, l)));
  main.replaceChildren(page);
  fill(page,
    pageTitle('explore', 'purple', 'Explore', seg),
    h('div', { class: 'stats' },
      stat('blue', 'photo', nf.format(S.META.total - S.META.videos), 'photos', '#/photos'),
      stat('purple', 'video', nf.format(S.META.videos), 'videos', hashFor('photos', photosWith({ type: 'video' }))),
      stat('amber', 'person', nf.format(ppl.length), 'people', '#/people'),
      stat('teal', 'folder', nf.format(S.META.albums.length), 'folders', '#/folders'),
      stat('green', 'place', nf.format(S.META.geotagged), 'with location', '#/map'),
      stat('slate', 'disk', fmtBytes(S.META.bytes), 'on disk')),
    otd, rediscover, thenNow,
    S.META.places.length ? [sec('green', 'place', 'Places', '#/places', 'See all'),
      h('div', { class: 'cards' }, S.META.places.slice(0, 12).map(placeCard))] : null,
    sec('amber', 'person', 'People', '#/people', 'See all'),
    h('div', { class: 'cards people' }, ppl.slice(0, 16).map(personCard)),
    detected, trips,
    sec('orange', 'tag', 'Things', '#/tags', 'All tags'),
    h('div', { class: 'cards' }, rootTags.map((t) => thumbCard(t.cover, t.name, nf.format(t.nAll), { tag: t.id }))),
    sec('cyan', 'tune', 'Quick filters'),
    h('div', { class: 'pills' },
      pill('Videos', S.META.videos, { type: 'video' }), pill('★★★★★', count(S.META.ratings, 5), { rating: 5 }),
      pill('★★★ and up', S.META.ratings.filter((r) => r[0] >= 3).reduce((a, r) => a + r[1], 0), { rating: 3 }),
      S.USER?.person && S.tagById.get(S.USER.person) ? pill('Photos of me', S.tagById.get(S.USER.person).nAll, { tag: S.USER.person }) : null,
      pill('Untagged', null, { untagged: 1 }),
      pill('No location', S.META.total - S.META.geotagged, { geo: 0 }), pill('Favorites', ST.fav.size, { fav: 1 }),
      h('a', { class: 'pill', href: '#/duplicates' }, 'Duplicate files'), h('a', { class: 'pill', href: '#/bursts' }, 'Bursts & best shots'),
      h('a', { class: 'pill', href: '#/events' }, 'Events & trips'), pill('Contains text', null, { hastext: 1 }), pill('Blurry', null, { blurry: 1 }),
      S.EDITING ? pill('Edited here, not yet in digiKam', S.PENDING, { edited: 1 }) : null,
      h('a', { class: 'pill', href: '#/map' }, 'Map', h('b', null, nf.format(S.META.geotagged)))),
    sec('blue', 'calendar', 'Years'),
    group('blue', S.META.years.map(([y, n]) => pill(y, n, { year: y }))),
    sec('purple', 'camera', 'Cameras'),
    group('purple', S.META.cameras.map(([c, n]) => pill(c, n, { camera: c }))),
    S.META.creators.length ? [sec('rose', 'copyright', 'Photographers'), group('rose', S.META.creators.slice(0, 30).map(([c, n]) => pill(c, n, { creator: c })))] : null,
    sec('slate', 'photo', 'File types'),
    group('slate', S.META.formats.map(([c, n]) => pill(c, n, { fmt: c }))));
  api('/api/events').then((ev) => {
    const list = ev.events.filter((e) => e.kind === 'trip').slice(0, 8);
    if (S.route.view !== 'explore' || !list.length) return;
    fill(trips, h('h2', { style: '--hue:var(--c-cyan)' }, h('span', { class: 'hic' }, icon('map')), 'Trips', h('a', { href: '#/events' }, 'All events & trips')), h('div', { class: 'cards' }, list.map(eventCard)));
    trips.hidden = false;
  }).catch(() => {});
  if (S.SEM.enabled) api('/api/concepts').then((c) => {
    if (S.route.view !== 'explore' || !c.concepts.length) return;
    fill(detected, h('h2', { style: '--hue:var(--c-purple)' }, h('span', { class: 'hic' }, icon('spark')), 'Detected content', h('a', { href: '#/detected' }, 'See all')),
      h('div', { class: 'cards' }, c.concepts.sort((a, b) => b.n - a.n).slice(0, 12).map(conceptCard)));
    detected.hidden = false;
  }).catch(() => {});
  try {
    const d = await api(`/api/photos?md=${md}`);
    if (S.route.view !== 'explore' || !d.count) return;
    for (let i = 0; i < Math.min(d.count, 8); i++) {  // one row or so; "See all" has the rest
      strip.append(h('div', { class: 'tile', style: `--ar:${Math.max(0.5, Math.min(2.2, d.w[i] / (d.h[i] || 1))).toFixed(3)}`, title: dfDay.format(date(d.ts[i])),
        onclick: () => go('photos', photosWith({ md, p: d.id[i] })) }, h('img', { src: `/thumb/${d.id[i]}`, loading: 'lazy', alt: '' })));
    }
    otd.hidden = false;
  } catch { /* optional section */ }
}

/** People: everyone named in the library, in the groups chosen here (Family, Friends, Classmates... in the viewer's
 *  database, not digiKam's), then everyone else by number of photos. Editors arrange the groups: drag people into a
 *  group and within it, or use a card's menu (touch screens). */
export async function showPeople() {
  const all = people(), byId = new Map(all.map((t) => [t.id, t]));
  let groups = [], canEdit = false, editing = false, filter = '';  // editing survives a redraw (S.arrangePeople)
  if (!main.querySelector('.cards')) main.replaceChildren(h('div', { class: 'page' }, loadingBox('Loading the people…')));
  try { const r = await api('/api/people/groups'); groups = r.groups; canEdit = r.canEdit && S.CAN_MANAGE; } catch { /* the groups are optional */ }
  if (S.route.view !== 'people') return;
  editing = canEdit && !!S.arrangePeople;
  const body = h('div');
  const todo = h('div', { class: 'pills', style: 'margin:6px 0 4px' });
  api('/api/faces/summary').then((s) => fill(todo,
    s.suggested[0] ? h('a', { class: 'pill', href: '#/faces?tab=suggested' }, 'Suggestions to confirm', h('b', null, `${nf.format(s.suggested[1])} in ${nf.format(s.suggested[0])} photos`)) : null,
    s.unknown[0] ? h('a', { class: 'pill', href: '#/faces?tab=unknown' }, 'Unnamed faces', h('b', null, `${nf.format(s.unknown[1])} in ${nf.format(s.unknown[0])} photos`)) : null,
    s.ignored[0] ? h('a', { class: 'pill', href: '#/faces?tab=ignored' }, 'Ignored faces', h('b', null, nf.format(s.ignored[1]))) : null)).catch(() => {});
  const save = async (g, change) => {
    try { await post(`/api/people/groups/${g.id}`, change); } catch (e) { toastError(e); }
  };
  const saveOrder = async () => { try { await post('/api/people/groups/order', { ids: groups.map((g) => g.id) }); } catch (e) { toastError(e); } };
  // move person `tid` into group `to` (null: out of `from`) before person `before` (null: at the end)
  const place = (tid, from, to, before) => {
    if (from && from !== to) { from.members = from.members.filter((x) => x !== tid); save(from, { members: from.members }); }
    if (to) {
      const m = to.members.filter((x) => x !== tid), k = before != null ? m.indexOf(before) : -1;
      m.splice(k < 0 ? m.length : k, 0, tid); to.members = m; save(to, { members: m });
    }
    draw();
  };
  let drag = null;  // {tid, from}
  const cardFor = (t, g) => {
    const c = personCard(t);
    if (!editing) return c;
    c.onclick = null; c.classList.add('arrange'); c.draggable = true;
    c.ondragstart = (e) => { drag = { tid: t.id, from: g }; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(t.id)); c.classList.add('dragging'); };
    c.ondragend = () => { drag = null; c.classList.remove('dragging'); body.querySelectorAll('.over').forEach((x) => x.classList.remove('over')); };
    c.ondragover = (e) => { if (drag && g) { e.preventDefault(); e.stopPropagation(); c.classList.add('over'); } };
    c.ondragleave = () => c.classList.remove('over');
    c.ondrop = (e) => { if (!drag || !g) return; e.preventDefault(); e.stopPropagation(); const d = drag; drag = null; if (d.tid !== t.id) place(d.tid, d.from, g, t.id); };
    // the same without dragging: which groups, and the place within this one
    c.append(h('button', { class: 'iconbtn pmenu', title: 'Groups and order', onclick: (e) => {
      e.stopPropagation();
      const k = g ? g.members.indexOf(t.id) : -1;
      popMenu(e.currentTarget, [
        groups.length ? 'Groups' : null,
        ...groups.map((x) => [`${x.members.includes(t.id) ? '✓ ' : ''}${x.name}`, () => {
          x.members = x.members.includes(t.id) ? x.members.filter((y) => y !== t.id) : [...x.members, t.id];
          save(x, { members: x.members }); draw(); }]),
        g && k > 0 ? 'Order' : g && k < g.members.length - 1 ? 'Order' : null,
        g && k > 0 ? ['Move earlier', () => { g.members.splice(k - 1, 0, g.members.splice(k, 1)[0]); save(g, { members: g.members }); draw(); }] : null,
        g && k >= 0 && k < g.members.length - 1 ? ['Move later', () => { g.members.splice(k + 1, 0, g.members.splice(k, 1)[0]); save(g, { members: g.members }); draw(); }] : null,
        ['New group with this person…', async () => { const n = await promptDialog('Name of the new group (Family, Friends, Classmates…)'); if (n && n.trim()) await newGroup(n.trim(), [t.id]); }],
      ]);
    } }, icon('more')));
    return c;
  };
  const newGroup = async (name, members = []) => {
    let r; try { r = await post('/api/people/groups', { name }); } catch (e) { toastError(e); return; }
    const g = { id: r.id, name, members };
    groups.push(g); if (members.length) await save(g, { members });
    draw();
  };
  const section = (g, list, title) => {
    const cards = h('div', { class: 'cards people' }, list.map((t) => cardFor(t, g)));
    const k = g ? groups.indexOf(g) : -1;
    const tools = editing && g ? h('span', { class: 'gtools' },
      h('button', { class: 'iconbtn', title: 'Rename this group', onclick: async () => { const n = await promptDialog('Group name', g.name); if (n && n.trim() && n.trim() !== g.name) { g.name = n.trim(); save(g, { name: g.name }); draw(); } } }, icon('edit')),
      k > 0 ? h('button', { class: 'iconbtn turn', title: 'Move this group up', onclick: () => { groups.splice(k - 1, 0, groups.splice(k, 1)[0]); saveOrder(); draw(); } }, icon('left')) : null,
      k < groups.length - 1 ? h('button', { class: 'iconbtn turn', title: 'Move this group down', onclick: () => { groups.splice(k + 1, 0, groups.splice(k, 1)[0]); saveOrder(); draw(); } }, icon('right')) : null,
      h('button', { class: 'iconbtn', title: 'Delete this group (the people stay)', onclick: async () => {
        if (!await confirmDialog(`Delete the group “${g.name}”? The people in it stay; only the group goes.`, { ok: 'Delete', danger: true })) return;
        try { await fetch(`/api/people/groups/${g.id}`, { method: 'DELETE' }).then((r) => { if (!r.ok) throw new Error(`${r.status}`); }); } catch (e) { toastError(e); return; }
        groups = groups.filter((x) => x !== g); draw(); } }, icon('trash'))) : null;
    const sec = h('section', { class: 'pgroup' + (editing ? ' editing' : '') },
      title ? h('h2', { class: 'sec pghead' }, title, h('span', { class: 'muted' }, nf.format(list.length)), tools) : null,
      editing && g && !list.length ? h('div', { class: 'muted pgempty' }, 'Drag people here, or use a person’s ⋯ menu.') : null, cards);
    if (editing) {  // drop on the section itself: at the end of this group (or out of the group, for Everyone else)
      sec.ondragover = (e) => { if (drag) { e.preventDefault(); sec.classList.add('over'); } };
      sec.ondragleave = (e) => { if (!sec.contains(e.relatedTarget)) sec.classList.remove('over'); };
      sec.ondrop = (e) => { if (!drag) return; e.preventDefault(); sec.classList.remove('over'); const d = drag; drag = null; if (g) place(d.tid, d.from, g, null); else if (d.from) place(d.tid, d.from, null, null); };
    }
    return sec;
  };
  let sort = store.get('people-sort', 'group'), size = store.get('people-size', 'm'), within = store.get('people-within', 'arranged');
  const byName = (a, b) => a.name.localeCompare(b.name), byCount = (a, b) => b.nAll - a.nAll;
  // the order inside a section: as arranged (groups only), by name, or the most photos first
  const order = (list, arranged) => within === 'name' ? [...list].sort(byName) : within === 'count' || !arranged ? [...list].sort(within === 'count' ? byCount : byName) : list;
  // a person's family: the tag above them under People (People/Yu/Carissa Yu), as digiKam's people are often filed
  const familyOf = (t) => { const parts = t.full.split('/'); return parts[0] === 'People' && parts.length > 2 ? parts[parts.length - 2] : null; };
  const draw = () => {
    const ok = (t) => !filter || t._n.includes(filter);
    if (arrange) arrange.hidden = sort !== 'group';
    withinSel.hidden = !(sort === 'group' || sort === 'family');
    withinSel.options[0].hidden = sort === 'family';
    if (sort === 'family') {
      const fams = new Map();
      for (const t of all.filter(ok)) { const f = familyOf(t) || ''; if (!fams.has(f)) fams.set(f, []); fams.get(f).push(t); }
      const names = [...fams.keys()].filter(Boolean).sort((a, b) => a.localeCompare(b));
      const secs = names.map((f) => section(null, order(fams.get(f), false), f));
      if (fams.has('')) secs.push(section(null, order(fams.get(''), false), names.length ? 'No family name' : null));
      body.replaceChildren(...secs);
      return;
    }
    if (sort !== 'group' || (!groups.length && !editing)) {  // one list: by name, or the most photos first
      const list = all.filter(ok).sort(sort === 'name' ? byName : byCount);
      body.replaceChildren(section(null, list, null));
      return;
    }
    const grouped = new Set(groups.flatMap((g) => g.members));
    const secs = groups.map((g) => {
      const list = g.members.map((id) => byId.get(id)).filter((t) => t && ok(t));
      return list.length || editing ? section(g, editing ? list : order(list, true), g.name) : null;
    });
    const rest = all.filter((t) => !grouped.has(t.id) && ok(t));
    secs.push(rest.length || editing ? section(null, within === 'name' ? order(rest, false) : rest, groups.length ? 'Everyone else' : null) : null);
    body.replaceChildren(...secs.filter(Boolean));
  };
  const withinSel = h('select', { class: 'sharepick', title: 'Order within each section', onchange: (e) => { within = e.target.value; store.set('people-within', within); draw(); } },
    [['arranged', 'As arranged'], ['name', 'Name'], ['count', 'Most photos']].map(([v, l]) => h('option', { value: v, selected: v === within }, `Within: ${l.toLowerCase()}`)));
  const arrange = canEdit ? h('button', { class: 'pillbtn' + (editing ? ' on' : ''), onclick: () => { editing = S.arrangePeople = !editing; arrange.classList.toggle('on', editing); fill(arrange, editing ? 'Done' : 'Arrange groups'); newBtn.hidden = !editing; draw(); } }, editing ? 'Done' : 'Arrange groups') : null;
  const newBtn = h('button', { class: 'pillbtn', hidden: !editing, onclick: async () => { const n = await promptDialog('Name of the new group (Family, Friends, Classmates…)'); if (n && n.trim()) newGroup(n.trim()); } }, icon('add'), 'New group');
  // sort and badge size, remembered
  const sortSel = h('select', { class: 'sharepick', title: 'Sort', onchange: (e) => { sort = e.target.value; store.set('people-sort', sort);
    if (sort !== 'group' && editing) { editing = S.arrangePeople = false; arrange.classList.remove('on'); fill(arrange, 'Arrange groups'); newBtn.hidden = true; }
    draw(); } }, [['group', 'By group'], ['family', 'By family name'], ['name', 'By name'], ['count', 'Most photos first']].map(([v, l]) => h('option', { value: v, selected: v === sort }, l)));
  const pageEl = h('div', { class: `page psz-${size}` });
  const seg = h('div', { class: 'eseg', title: 'Badge size' }, [['s', 'S'], ['m', 'M'], ['l', 'L']].map(([k, l]) => h('button', { class: k === size ? 'on' : '', onclick: (e) => {
    size = k; store.set('people-size', k); pageEl.className = `page psz-${k}`; seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.currentTarget)); } }, l)));
  main.replaceChildren(pageEl);
  fill(pageEl, pageTitle('person', 'amber', 'People ', h('span', { class: 'muted' }, nf.format(all.length))), todo,
    h('div', { class: 'toolbar',},
      h('input', { class: 'pagefilter', placeholder: 'Find a person…', style: 'margin:0', oninput: (e) => { filter = norm(e.target.value); draw(); } }), sortSel, withinSel, seg, arrange, newBtn),
    h('div', { style: 'height:14px' }), body);
  draw();
}

export const UPLOAD_ACCEPT = '.jpg,.jpeg,.png,.gif,.tif,.tiff,.webp,.heic,.heif,.avif,.bmp,.dng,.cr2,.cr3,.nef,.arw,.raf,.orf,.rw2,.mov,.mp4,.m4v,.avi,.mkv,.webm,.3gp,.mts';
/** Folders: digiKam's own (filesystem) folders, empty ones included, with new-folder and upload
 *  actions for editors when the database can be written. Albums (the viewer's own groupings) are
 *  a separate page. */
/** digiKam's Album Properties: caption, date, category and the cover photo, stored in its Albums row. */
export async function folderProperties(f, all, after) {
  const caption = h('textarea', { class: 'einput', rows: 3, placeholder: 'What this folder holds', value: f.caption || '' });
  const date = h('input', { class: 'einput', type: 'date', value: f.date || '', style: 'max-width:180px' });
  const cats = [...new Set((all || []).map((x) => x.category).filter(Boolean))].sort();
  const list = h('datalist', { id: 'fcats' }, cats.map((c) => h('option', { value: c })));
  const category = h('input', { class: 'einput', list: 'fcats', placeholder: 'e.g. Holidays, Family, Work', value: f.category || '' });
  let icon_ = f.icon ?? null;
  const covers = h('div', { class: 'fcovers' }, h('div', { class: 'muted' }, 'Loading photos…'));
  const pick = (id) => { icon_ = id; covers.querySelectorAll('.fcov').forEach((el) => el.classList.toggle('on', String(el.dataset.id) === String(id ?? 'auto'))); };
  api('/api/photos?' + photosWith({ album: f.id, sort: 'old' }).toString()).then((d) => {
    const ids = (d.id || []).slice(0, 80);
    fill(covers, h('div', { class: 'fcov auto', 'data-id': 'auto', title: 'Chosen automatically: the best-rated photo', onclick: () => pick(null) }, 'Auto'),
      ids.map((id) => h('img', { class: 'fcov', 'data-id': id, src: `/thumb/${id}`, alt: '', onclick: () => pick(id) })));
    pick(icon_);
  }).catch(() => fill(covers, h('div', { class: 'muted' }, 'This folder has no photos yet.')));
  const save = async () => {
    try {
      await post(`/api/folders/${f.id}/properties`, { caption: caption.value, date: date.value, category: category.value, icon: icon_ });
    } catch (e) { toastError(e); return; }
    back.remove(); toast('Folder properties saved.'); after?.();
  };
  const back = modal(`Properties of “${f.name}”`,
    h('div', { class: 'fpbody' },
      h('label', null, h('div', { class: 'muted' }, 'Caption'), caption),
      h('div', { style: 'display:flex;gap:12px;flex-wrap:wrap' }, h('label', null, h('div', { class: 'muted' }, 'Date'), date),
        h('label', { style: 'flex:1;min-width:200px' }, h('div', { class: 'muted' }, 'Category'), category, list)),
      h('div', null, h('div', { class: 'muted' }, 'Cover'), covers),
      h('div', { style: 'display:flex;gap:8px;justify-content:flex-end' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'),
        h('button', { class: 'pillbtn on', onclick: save }, 'Save'))));
  back.querySelector('.modal').classList.add('fpmodal');
}
export async function showFolders() {
  let d;
  if (!main.querySelector('.tree')) main.replaceChildren(h('div', { class: 'page' }, loadingBox('Loading the folders…')));
  try { d = await api('/api/folders'); } catch (e) { if (S.route.view === 'folders') main.replaceChildren(h('div', { class: 'page' }, errorBox(e))); return; }
  if (S.route.view !== 'folders') return;
  const all = d.folders.map((f) => ({ ...f, _n: norm(f.path) }));
  const tops = all.filter((f) => f.pid == null).sort((a, b) => b.path.localeCompare(a.path, undefined, { numeric: true }));
  const can = d.canManage && S.CAN_MANAGE;
  const refresh = () => { S.viewKey = null; pollSoon(600); setTimeout(() => { if (S.route.view === 'folders') showFolders(); }, 900); };
  const newFolder = async (parent) => {
    const n = await promptDialog(`Name for the new folder in ${parent.path}`); if (!n || !n.trim()) return;
    try { const r = await post('/api/folders', { parent: parent.id, name: n.trim() }); toast(`Created ${r.path}. digiKam sees it on its next start.`); } catch (e) { toastError(e); return; }
    refresh();
  };
  const uploadTo = (folder) => {
    const inp = h('input', { type: 'file', multiple: true, accept: UPLOAD_ACCEPT, hidden: true });
    inp.onchange = async () => {
      const files = [...inp.files]; inp.remove();
      if (!files.length) return;
      const send = async (retried) => {
        const fd = new FormData(); files.forEach((f) => fd.append('files', f));
        toast(`Uploading ${plural(files.length, 'file')} to ${folder.path}…`, null, null, 120000);
        const res = await fetch(`/api/folders/${folder.id}/upload`, { method: 'POST', body: fd });
        let r = {}; try { r = await res.json(); } catch { /* not json */ }
        if (!res.ok) throw new Error(r.detail || `${res.status}`);
        return r;
      };
      try {
        const r = await send(false);
        toast(`${plural(r.added.length, 'file')} added to ${folder.path}` + (r.skipped.length ? `; ${plural(r.skipped.length, 'file')} skipped (${r.skipped[0].reason})` : '') + '. digiKam reads the full metadata on its next run.', null, null, 9000);
      } catch (e) { toastError(e); }
      refresh();
    };
    document.body.append(inp); inp.click();
  };
  // the tree: one row per folder, sub-folders under an expander, like the tag tree
  const kidsOf = new Map();
  all.forEach((f) => { if (f.pid != null) { if (!kidsOf.has(f.pid)) kidsOf.set(f.pid, []); kidsOf.get(f.pid).push(f); } });
  kidsOf.forEach((l) => l.sort((a, b) => b.name.localeCompare(a.name, undefined, { numeric: true })));
  const act = (ic, title, fn) => h('button', { class: 'iconbtn tact', title, 'aria-label': title, onclick: (e) => { e.stopPropagation(); fn(); } }, icon(ic));
  const row = (f, flat) => {
    const kids = flat ? [] : (kidsOf.get(f.id) || []);
    const node = h('div', { class: 'tnode' }), box = h('div', { class: 'tkids', hidden: true });
    const expand = (on) => { if (on && !box.children.length) box.append(...kids.map((k) => row(k))); node.classList.toggle('open', on); box.hidden = !on; on ? openFolders.add(f.id) : openFolders.delete(f.id); };
    const tw = h('span', { class: 'tw', style: kids.length ? '' : 'visibility:hidden', onclick: (e) => { e.stopPropagation(); expand(!node.classList.contains('open')); } }, icon('right'));
    const a = S.albumById.get(f.id);
    const cover = a?.cover ? h('img', { src: `/thumb/${a.cover}`, loading: 'lazy', alt: '' }) : h('span', { class: 'ph' }, icon('folder'));
    const trow = h('div', { class: 'trow', title: f.path, tabindex: 0, role: 'button', onclick: () => { if (f.nAll) go('photos', photosWith({ album: f.id, sort: 'old' })); else if (can) uploadTo(f); else expand(!node.classList.contains('open')); } },
      tw, cover, h('span', { class: 'nm' }, flat ? f.path.replace(/^\//, '').replaceAll('/', ' › ') : f.name,
        f.caption || f.category ? h('span', { class: 'muted fcap' }, [f.category, f.caption].filter(Boolean).join(' · ')) : null),
      can ? h('span', { class: 'tacts' }, act('add', 'Upload photos into this folder', () => uploadTo(f)), act('folder', 'New folder inside this one', () => newFolder(f)),
        f.pid != null ? act('edit', 'Rename this folder', async () => { const n = await promptDialog('Folder name', f.name); if (!n || n.trim() === f.name) return; try { await post(`/api/folders/${f.id}/rename`, { name: n.trim() }); toast(`Renamed to ${n.trim()}.`); } catch (e) { toastError(e); return; } refresh(); }) : null,
        f.pid != null ? act('open', 'Move this folder under another one', () => folderPicker(`Move “${f.name}” under…`, async (p) => { try { const r = await post(`/api/folders/${f.id}/move`, { parent: p.id }); toast(`Moved to ${r.path}.`); } catch (e) { toastError(e); return; } refresh(); }, (x) => x.id === f.id || x.path.startsWith(f.path + '/'))) : null,
        act('info', 'Folder properties: caption, date, category, cover', () => folderProperties(f, all, refresh)),
        f.nAll ? act('download', 'Download this folder as a zip', () => downloadDialog(photosWith({ album: f.id }))) : null,
        f.nAll && S.CAN_MANAGE && S.FACE_DETECT ? act('face', 'Detect faces in this folder and its subfolders', () => detectFaces(null, f)) : null) : null,
      h('span', { class: 'ct' }, f.nAll ? nf.format(f.nAll) : h('i', null, 'empty')));
    if (!flat && kids.length && openFolders.has(f.id)) expand(true);
    node.append(trow, box);
    return node;
  };
  const tree = h('div', { class: 'tree' });
  const draw = (q) => {
    tree.replaceChildren();
    if (q) tree.append(...all.filter((f) => f._n.includes(q)).map((f) => row(f, true)));
    else tree.append(...tops.map((t) => row(t)));
    if (!tree.children.length) tree.append(h('div', { class: 'empty' }, 'No folders match.'));
  };
  if (!openFolders.size) tops.forEach((t) => openFolders.add(t.id));  // first visit: the top level open, deeper levels folded
  main.replaceChildren(h('div', { class: 'page' }, pageTitle('folder', 'teal', 'Folders ', h('span', { class: 'muted' }, nf.format(all.length)), can ? [h('a', { class: 'lnk', href: '#/import', style: 'margin-left:14px;font-size:14px;font-weight:400' }, 'Import…'), h('a', { class: 'lnk', href: '#/trash', style: 'margin-left:12px;font-size:14px;font-weight:400' }, 'Trash')] : null),
    h('div', { class: 'muted intro' }, 'digiKam’s folders, as they are on disk. Click a folder to see its photos (sub-folders included); the arrow opens its sub-folders.', can ? ' New folders and uploads go straight into the library; digiKam picks them up and reads their full metadata on its next start.' : ''),
    h('input', { class: 'pagefilter', placeholder: 'Find a folder…', oninput: (e) => draw(norm(e.target.value)) }), tree));
  draw('');
}
export const openFolders = new Set();  // which folder nodes are expanded, kept across visits
/** Albums: groupings that exist only in this viewer (favorites-like lists stored in its own database). */
export function showAlbums() {
  const body = h('div');
  // viewer albums: a change is posted, then the list is redrawn from the reloaded state
  const change = async (path, payload) => { try { await post(path, payload); } catch (e) { toast(e.message || 'Could not update the album.'); return; } await loadState(); showAlbums(); };
  const mine = (a) => {
    const c = thumbCard(a.cover, a.name, plural(a.n, 'photo'), { ualbum: a.id });
    const act = (ic, title, fn) => h('button', { class: 'carddl', title, 'aria-label': title, onclick: async (e) => { e.stopPropagation(); await fn(); } }, icon(ic));
    if (!a.shared) c.querySelector('.cv').append(h('span', { class: 'privbadge', title: 'Private: only you see this album' }, icon('lock')));
    c.querySelector('.cv').append(h('span', { class: 'cardacts' },
      act('link', 'Copy a link to this album', async () => { await navigator.clipboard?.writeText(location.origin + '/' + hashFor('photos', photosWith({ ualbum: a.id }))); toast(a.shared ? `Link copied. It works for anyone who can reach ${S.APP_NAME}.` : 'Link copied. The album is private: it opens only for you.'); }),
      a.canEdit ? act('edit', 'Rename', async () => { const n = await promptDialog('Album name', a.name); if (n && n.trim()) return change(`/api/ualbums/${a.id}`, { name: n.trim() }); }) : null,
      a.canEdit && ST.canShare && ST.canPrivate ? act(a.shared ? 'lock' : 'group', a.shared ? 'Make private (only you will see it)' : 'Share with everyone',
        async () => { if (await confirmDialog(a.shared ? `Make “${a.name}” private? Only you will see it; other users lose it from their Albums.` : `Share “${a.name}” with everyone who can use ${S.APP_NAME}? Editors and admins can then change it.`)) return change(`/api/ualbums/${a.id}`, { shared: !a.shared }); }) : null,
      act('download', 'Download as a zip', () => downloadDialog(photosWith({ ualbum: a.id }))),
      a.canEdit ? act('trash', 'Delete this album (photos are not touched)', async () => { if (await confirmDialog(`Delete the album “${a.name}”? The photos themselves are not affected.`, { ok: 'Delete', danger: true })) return change(`/api/ualbums/${a.id}`, { delete: true }); }) : null));
    return c;
  };
  const newAlbum = () => {
    const name = h('input', { class: 'einput', placeholder: 'Album name' }), share = sharingPick();
    const create = () => { if (!name.value.trim()) { name.focus(); return; } back.remove(); change('/api/ualbums', { name: name.value.trim(), shared: pickedShared(share) }); };
    name.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') create(); };
    const back = modal('New album', h('div', { style: 'display:grid;gap:10px' }, name, share),
      h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'), h('button', { class: 'pillbtn on', onclick: create }, 'Create')));
    name.focus();
  };
  const newCard = ST.canShare || ST.canPrivate ? h('div', { class: 'card', tabindex: 0, role: 'button', onclick: newAlbum },
    h('span', { class: 'cv newalbum' }, icon('add')), h('div', { class: 'nm' }, 'New album'), h('div', { class: 'sb' }, ST.canShare ? 'Shared or private' : 'Private: only you see it')) : null;
  // smart albums: a smart album, evaluated live (count and cover fetched when the card is drawn)
  const smart = (s) => {
    const q = new URLSearchParams(s.query);
    const c = thumbCard(null, s.name, '…', Object.fromEntries(q.entries()));
    api('/api/photos?' + q.toString()).then((d) => { if (!c.isConnected) return; c.querySelector('.sb').textContent = plural(d.count, 'photo'); if (d.count) { const im = h('img', { src: `/thumb/${d.id[0]}`, alt: '', loading: 'lazy' }); c.querySelector('.cv').prepend(im); } }).catch(() => { c.querySelector('.sb').textContent = 'could not run the search'; });
    const act = (ic, title, fn) => h('button', { class: 'carddl', title, 'aria-label': title, onclick: async (e) => { e.stopPropagation(); await fn(); } }, icon(ic));
    c.querySelector('.cv').append(h('span', { class: 'cardacts' },
      act('link', 'Copy a link to this smart album', async () => { await navigator.clipboard?.writeText(location.origin + '/' + hashFor('photos', q)); toast('Link copied.'); }),
      act('edit', 'Rename', async () => { const n = await promptDialog('Smart album name', s.name); if (n && n.trim()) return change(`/api/searches/${s.id}`, { name: n.trim() }); }),
      act('download', 'Download as a zip', () => downloadDialog(q)),
      act('trash', 'Delete this smart album (the photos are not touched)', async () => { if (await confirmDialog(`Delete the smart album “${s.name}”?`, { ok: 'Delete', danger: true })) return change(`/api/searches/${s.id}`, { delete: true }); })));
    c.classList.add('smart');
    return c;
  };
  const draw = (f) => {
    body.replaceChildren();
    const my = ST.albums.filter((a) => !f || norm(a.name).includes(f));
    const shared = my.filter((a) => a.shared), priv = my.filter((a) => !a.shared);
    body.append(h('div', { class: 'cards' }, shared.map(mine), f ? null : newCard));
    if (priv.length) body.append(h('h2', { style: '--hue:var(--c-indigo)' }, h('span', { class: 'hic' }, icon('lock')), 'Private albums', h('span', { class: 'muted' }, 'only you see these')),
      h('div', { class: 'cards' }, priv.map(mine)));
    const sm = ST.searches.filter((s) => !f || norm(s.name).includes(f));
    if (sm.length || !f) body.append(h('h2', { style: '--hue:var(--c-indigo)' }, h('span', { class: 'hic' }, icon('search')), 'Smart albums', h('span', { class: 'muted' }, 'smart albumes that update themselves')),
      sm.length ? h('div', { class: 'cards' }, sm.map(smart)) : h('div', { class: 'muted note' }, 'None yet. Filter or search the photos, then use “Save search” in the bar above the grid.'));
    if (!my.length && !sm.length && f) body.append(h('div', { class: 'empty' }, 'No albums match.'));
  };
  main.replaceChildren(h('div', { class: 'page' }, pageTitle('album', 'indigo', 'Albums ', h('span', { class: 'muted' }, nf.format(ST.albums.length))),
    h('div', { class: 'muted intro' }, `Albums are groupings that exist only in ${S.APP_NAME}: any photos, from any folder, under a name of your own. digiKam and the files on disk are not touched. A shared album is seen by everyone and changed by editors and admins; a private album is seen and changed only by the person who made it. The folders digiKam manages are under `, h('a', { href: '#/folders', class: 'lnk' }, 'Folders'), '.'),
    h('input', { class: 'pagefilter', placeholder: 'Find an album…', oninput: (e) => draw(norm(e.target.value)) }), body));
  draw('');
}

export const placeCard = ([city, region, country, n, cover]) => card({ img: `/thumb/${cover}`, name: city, sub: `${[region, country].filter(Boolean).join(', ')} · ${nf.format(n)}`,
  onclick: () => go('photos', photosWith({ place: city, ...(region ? { region } : {}), ...(country ? { country } : {}) })) });
export function showPlaces(vp) {
  if (!S.META.places.length) {
    main.replaceChildren(h('div', { class: 'empty' }, 'Place names are not set up yet. They need the offline GeoNames data in the app folder (see the README).'));
    return;
  }
  const country = vp?.get('country') || null, region = vp?.get('region') || null;
  const rows = S.META.places.filter((x) => (!country || x[2] === country) && (!region || (x[1] || 'Other') === region));
  const level = !country ? 2 : !region ? 1 : 0;  // which column of a place row this page lists
  const groups = new Map();
  for (const x of rows) {
    const key = level === 0 ? x[0] : (x[level] || 'Other');
    if (!groups.has(key)) groups.set(key, { name: key, n: 0, cover: x[4], top: 0, kids: new Set(), row: x });
    const g = groups.get(key);
    g.n += x[3];
    g.kids.add(level === 2 ? (x[1] || 'Other') : x[0]);
    if (x[3] > g.top) { g.top = x[3]; g.cover = x[4]; }
  }
  const list = [...groups.values()].sort((a, b) => b.n - a.n);
  const mk = (g) => level === 2
    ? card({ img: `/thumb/${g.cover}`, name: g.name, sub: `${nf.format(g.n)} · ${plural(g.kids.size, 'region')}`, onclick: () => go('places', photosWith({ country: g.name })) })
    : level === 1
      ? card({ img: `/thumb/${g.cover}`, name: g.name, sub: `${nf.format(g.n)} · ${plural(g.kids.size, 'town')}`, onclick: () => go('places', photosWith({ country, region: g.name })) })
      : thumbCard(g.cover, g.name, nf.format(g.n), { place: g.name, ...(g.row[1] ? { region: g.row[1] } : {}), country });
  const here = level === 2 ? {} : level === 1 ? { country } : { country, ...(region !== 'Other' ? { region } : {}) };
  const total = rows.reduce((a, x) => a + x[3], 0);
  const cards = h('div', { class: 'cards' });
  const draw = (f) => { const l = list.filter((g) => !f || norm(g.name).includes(f)); fill(cards, l.length ? l.map(mk) : h('div', { class: 'empty' }, 'No places match.')); };
  main.replaceChildren(h('div', { class: 'page' },
    country ? crumbs([['Places', '#/places'], [country, region ? placeHash({ country }) : null], ...(region ? [[region, null]] : [])]) : null,  // no trail on the top level: it would only repeat the title
    pageTitle('place', 'green', region || country || 'Places', ' ', h('span', { class: 'muted' }, nf.format(total))),
    h('div', { class: 'muted' }, level === 2 ? 'Countries where your geotagged photos were taken. Open one to see its regions, then towns. Names come from each photo’s GPS position, worked out offline.'
      : level === 1 ? `Regions of ${country}.` : `Towns in ${region}, ${country}.`),
    h('div', { class: 'toolbar wide',},
      h('input', { class: 'pagefilter', placeholder: level === 2 ? 'Find a country…' : level === 1 ? 'Find a region…' : 'Find a town…', oninput: (e) => draw(norm(e.target.value)) }),
      level < 2 ? h('a', { class: 'pillbtn mt6', href: hashFor('photos', photosWith(here)) }, icon('photo'), `All ${nf.format(total)} photos here`) : null,
      level < 2 ? h('a', { class: 'pillbtn mt6', href: hashFor('map', photosWith(here)) }, icon('map'), 'Map') : null),
    h('div', { style: 'height:14px' }), cards));
  draw('');
}

/** A main page's title with its navigation icon in the page's colour (as in the left rail). */
export const pageTitle = (ic, hue, ...kids) => h('h1', { class: 'ptitle', style: `--hue:var(--c-${hue})` }, h('span', { class: 'pic' }, icon(ic)), ...kids);
export const conceptCard = (c) => thumbCard(c.cover, c.name[0].toUpperCase() + c.name.slice(1), nf.format(c.n), { concept: c.name });
export async function showDetected() {
  main.replaceChildren(h('div', { class: 'empty' }, 'Looking at what the model sees…'));
  let d;
  try { d = await api('/api/concepts'); } catch { if (S.route.view === 'detected') main.replaceChildren(h('div', { class: 'empty' }, 'Could not load detected content.')); return; }
  if (S.route.view !== 'detected') return;
  if (!d.enabled) { main.replaceChildren(h('div', { class: 'empty' }, 'Content search is not enabled (see the README).')); return; }
  const body = h('div');
  const draw = (f) => {
    body.replaceChildren();
    const groups = new Map();
    for (const c of d.concepts) { if (f && !norm(c.name).includes(f)) continue; if (!groups.has(c.group)) groups.set(c.group, []); groups.get(c.group).push(c); }
    for (const [g, list] of groups) body.append(h('h2', { class: 'sec' }, g, h('span', { class: 'muted' }, `${list.length} subjects`)),
      h('div', { class: 'cards' }, list.sort((a, b) => b.n - a.n).map(conceptCard)));
    if (!body.children.length) body.append(h('div', { class: 'empty' }, 'Nothing matches. Try the search box: type any description and pick “Photos that look like…”.'));
  };
  main.replaceChildren(h('div', { class: 'page' }, crumbs([['Explore', '#/explore'], ['Detected content', null]]), pageTitle('spark', 'purple', 'Detected content ', h('span', { class: 'muted' }, nf.format(d.concepts.length))),
    h('div', { class: 'muted intro' }, 'What the content-search model sees in your photos. It does not store labels: each photo is compared with a fixed list of several hundred everyday subjects and filed under the few it matches best. These are guesses and can be wrong; they are not written to digiKam. For anything not listed here, type a description in the search box.',
      d.indexing ? ' Indexing is still running, so counts will grow.' : ''),
    h('input', { class: 'pagefilter', placeholder: 'Find a subject…', oninput: (e) => draw(norm(e.target.value)) }), body));
  draw('');
}

export const eventCard = (e) => thumbCard(e.cover, e.title, `${e.when} · ${nf.format(e.n)}${e.kind === 'trip' && e.days > 1 ? ` · ${e.days} days` : ''}`, { from: e.from, to: e.to, sort: 'old' });
export async function showEvents() {
  main.replaceChildren(h('div', { class: 'empty' }, 'Working out events and trips…'));
  let d;
  try { d = await api('/api/events'); } catch { if (S.route.view === 'events') main.replaceChildren(h('div', { class: 'empty' }, 'Could not load events.')); return; }
  if (S.route.view !== 'events') return;
  let kind = '';
  const body = h('div');
  const draw = (f) => {
    body.replaceChildren();
    const years = new Map();
    for (const e of d.events) {
      if ((kind && e.kind !== kind) || (f && !norm(e.title + ' ' + (e.places || []).join(' ')).includes(f))) continue;
      const y = e.from.slice(0, 4);
      if (!years.has(y)) years.set(y, []);
      years.get(y).push(e);
    }
    for (const [y, list] of years) body.append(h('h2', { class: 'sec' }, y, h('span', { class: 'muted' }, `${list.length}`),
      h('a', { href: hashFor('photos', photosWith({ year: y })) }, 'All of ' + y)), h('div', { class: 'cards' }, list.map(eventCard)));
    if (!body.children.length) body.append(h('div', { class: 'empty' }, 'Nothing matches.'));
  };
  const filter = h('input', { class: 'pagefilter', placeholder: 'Find an event or trip…', oninput: (e) => draw(norm(e.target.value)) });
  const seg = h('span', { class: 'eseg mt6' }, [['', 'All'], ['trip', 'Trips'], ['event', 'Events']].map(([k, l]) => h('button', { class: k === kind ? 'on' : '', onclick: (e) => {
    kind = k; seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.target)); draw(norm(filter.value)); } }, l)));
  main.replaceChildren(h('div', { class: 'page' }, crumbs([['Explore', '#/explore'], ['Events & trips', null]]),
    pageTitle('map', 'cyan', 'Events & trips ', h('span', { class: 'muted' }, nf.format(d.events.length))),
    h('div', { class: 'muted intro' }, 'Trips are runs of days photographed far from where most of your photos are taken. Events are other days with 20 or more photos, named after the title most of them share, or the town. Worked out from dates, GPS and titles; nothing is stored in digiKam.'),
    h('div', { class: 'toolbar wide',}, filter, seg), body));
  draw('');
}

export async function showBursts(vp) {
  const qs = new URLSearchParams(vp || '');
  main.replaceChildren(h('div', { class: 'empty' }, 'Finding bursts…'));
  const list = h('div');
  let offset = 0, total = 0, src = { id: [] };
  const more = h('button', { class: 'pillbtn', style: 'margin:20px auto;display:flex', onclick: () => load() }, 'Show more bursts');
  const pct = (v) => (v == null ? '' : `${Math.round(v * 100)}`);
  const group = (g) => {
    const best = g.items.find((x) => x.best) || g.items[0];
    const others = (keep) => g.items.filter((x) => x.id !== keep.id).map((x) => x.id);
    const start = src.id.length;
    g.items.forEach((x) => src.id.push(x.id));
    const row = h('div', { class: 'duprow' }, g.items.map((x, k) => h('div', { class: 'dupitem burstitem' + (x.best ? ' best' : '') + '' },
      h('div', { class: 'tile', style: `width:${Math.round(150 * Math.min(2, x.w / (x.h || 1)))}px;height:150px`, onclick: () => openLightbox(src, start + k, true) },
        h('img', { src: `/thumb/${x.id}`, loading: 'lazy', alt: '' }), x.best ? h('span', { class: 'bestbadge' }, icon('star'), 'Best') : null),
      h('div', { class: 'sb' }, [x.sharpn != null ? `sharpness ${pct(x.sharpn)}` : null, x.faceq != null ? `faces ${pct(x.faceq)}` : null].filter(Boolean).join(' · ') || ' '),)));
    return h('div', { class: 'dupgroup' }, h('div', { style: 'display:flex;gap:14px;align-items:baseline;flex-wrap:wrap' },
      h('span', null, dfDay.format(date(g.items[0].ts)), ' ', h('span', { class: 'muted' }, `${dfTime.format(date(g.items[0].ts))} · ${g.items.length} frames`)),
      h('a', { class: 'lnk', href: hashFor('photos', photosWith({ burst: g.burst, sort: 'old' })) }, 'Open in grid')), row);
  };
  const load = async () => {
    qs.set('offset', offset);
    let d;
    try { d = await api('/api/bursts?' + qs.toString()); } catch { list.append(h('div', { class: 'empty' }, 'Could not load bursts.')); return; }
    if (S.route.view !== 'bursts') return;
    total = d.bursts;
    if (!offset) head.textContent = d.bursts ? `${nf.format(d.bursts)} bursts covering ${nf.format(d.photos)} photos. The best frame is picked by sharpness and how good the faces look; check it before rejecting the rest.` : 'No bursts found here.';
    list.append(...d.groups.map(group));
    offset += d.groups.length;
    more.hidden = offset >= total;
    syncLightbox();
  };
  const refresh = () => { offset = 0; src = { id: [] }; list.replaceChildren(); setTimeout(load, 1800); };
  const head = h('div', { class: 'muted intro' });
  const scoped = [...qs.keys()].length > 0;  // bursts within a search, rather than the whole library
  main.replaceChildren(h('div', { class: 'page' }, crumbs([['Explore', '#/explore'], ['Bursts & best shots', null]]),
    pageTitle('dup', 'indigo', 'Bursts & best shots'), head,
    scoped ? h('div', { class: 'enote', style: 'max-width:760px' }, 'Showing bursts within your current search only. ', h('a', { class: 'lnk', href: '#/bursts' }, 'Show all bursts')) : null,
    list, more));
  more.hidden = true;
  await load();
}

// ---- users: sign in / out, own password, first-admin setup, admin user management
export async function loadUser() {
  try { const me = await api('/api/auth/me'); S.USER = me.user; SETTINGS.user = me.user ? me.settings : null; S.IS_ADMIN = me.isAdmin; S.USERS_EXIST = me.usersExist; S.REQUIRE_LOGIN = !!me.requireLogin; S.VIA_PROXY = !!me.viaProxy; } catch { S.USER = null; SETTINGS.user = null; }
  renderUserMenu();
}
// which bar item a view belongs to: Explore's sub-pages light Explore, the Faces page People, Maintenance's pages Maintenance
export const NAV_OF = { duplicates: 'explore', detected: 'explore', bursts: 'explore', events: 'explore', faces: 'people', analysis: 'maintenance', history: 'maintenance', consistency: 'maintenance', quality: 'maintenance', trash: 'folders', import: 'folders' };
export const MORE_VIEWS = ['favorites', 'folders', 'albums', 'tags', 'map', 'settings', 'maintenance'];
export function currentNav() {
  const fp = filterParams();
  if (S.route.view === 'photos' && fp.get('fav') === '1' && [...fp.keys()].length === 1) return 'favorites';
  return NAV_OF[S.route.view] || S.route.view;
}
/** Phones: the bottom bar shows four destinations; the rest, with Settings and Maintenance, open from "More". */
export function moreSheet() {
  const rest = NAV.filter(([v]) => ['favorites', 'folders', 'albums', 'tags', 'map'].includes(v)).map(([v, label, ic, href]) => [href || `#/${v}`, label, ic, v]);  // the ones the phone bar hides
  if (S.CAN_MANAGE) rest.push(['#/import', 'Import', 'add', 'import']);
  rest.push(['#/settings', 'Settings', 'gear', 'settings']);
  if (S.IS_ADMIN) rest.push(['#/maintenance', 'Maintenance', 'wrench', 'maintenance']);
  const back = modal('More', h('div', { class: 'sheetgrid' }, rest.map(([href, label, ic, v]) => h('a', { href, class: currentNav() === v ? 'on' : '', style: `--hue:var(--c-${{ favorites: 'rose', folders: 'teal', albums: 'indigo', tags: 'orange', map: 'cyan' }[v] || 'slate'})`, onclick: () => back.remove() }, h('span', { class: 'sic' }, icon(ic)), label))),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close')));
}
export function renderUserMenu() {
  const el = $('#usermenu'); if (!el) return;
  el.replaceChildren(S.USER
    ? h('button', { class: 'iconbtn tool', title: `${S.USER.name} (${S.USER.role}) · account`, onclick: accountDialog }, avatarOf(S.USER))
    : h('button', { class: 'iconbtn tool', title: S.USERS_EXIST ? 'Sign in' : 'Create the admin account', onclick: () => (S.USERS_EXIST ? loginDialog() : setupDialog()) }, icon('person')));
}
export async function afterLogin(me) {
  S.USER = me.user; SETTINGS.user = me.settings || {}; renderUserMenu();
  if (!S.BOOTED) { applyBadgePref(); applyTheme(); await boot(); return; }  // sign-in was required: load the library now
  await pollStatus(); applyBadgePref(); applyTheme(); S.viewKey = null; onRoute();
}
/** required: the viewer shows nothing else until someone signs in (no Cancel, no closing). */
export function loginDialog(required) {
  // the account picker: a tile per account; one click for a passwordless viewer, a password for the others
  const name = h('input', { class: 'einput', placeholder: 'User name', autocomplete: 'username' });
  const pw = h('input', { class: 'einput', type: 'password', placeholder: 'Password', autocomplete: 'current-password' });
  const form = h('div', { style: 'display:grid;gap:8px;margin-top:10px' }, name, pw);
  const tiles = h('div', { class: 'accounts' });
  const submit = async () => { try { const me = await post('/api/auth/login', { name: name.value.trim(), password: pw.value }); back.remove(); await afterLogin(me); toast(`Signed in as ${me.user.name}.`); } catch (e) { toastError(e); } };
  [name, pw].forEach((i) => { i.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') submit(); }; });
  const back = modal('Sign in', tiles, form,
    h('p', { class: 'muted small' }, required ? `Sign in to use ${S.APP_NAME}.` : 'Without signing in you can browse everything; signing in keeps your display settings, favorites and albums on the server, and editors can change the library.'),
    h('div', { class: 'modal-btns' }, required ? null : h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'), h('button', { class: 'pillbtn on', onclick: submit }, 'Sign in')));
  if (required) { back.onclick = null; back.classList.add('required'); }
  api('/api/auth/accounts').then((d) => {
    if (!d.accounts.length) { name.focus(); return; }
    form.hidden = true;
    fill(tiles, d.accounts.map((a) => h('button', { class: 'account', title: a.passwordless ? `Sign in as ${a.name}` : `${a.name} (password)`, onclick: async () => {
      if (a.passwordless) { try { const me = await post('/api/auth/login', { name: a.name }); back.remove(); await afterLogin(me); toast(`Signed in as ${a.name}.`); } catch (e) { toastError(e); } return; }
      name.value = a.name; form.hidden = false; tiles.querySelectorAll('.account').forEach((t) => t.classList.toggle('on', t === tiles.querySelector(`[data-n="${CSS.escape(a.name)}"]`))); pw.focus();
    }, 'data-n': a.name }, avatarOf(a, true), h('span', { class: 'nm' }, a.name), a.passwordless ? null : h('span', { class: 'muted', style: 'font-size:11px' }, 'password'))),
      h('button', { class: 'account', title: 'Type a user name', onclick: () => { form.hidden = false; name.value = ''; name.focus(); } }, h('span', { class: 'avatar big', style: 'background:var(--bg3);color:var(--fg2)' }, '…'), h('span', { class: 'nm' }, 'Other')));
  }).catch(() => name.focus());
}
export function setupDialog() {
  const name = h('input', { class: 'einput', placeholder: 'Admin user name', autocomplete: 'username' });
  const pw = h('input', { class: 'einput', type: 'password', placeholder: 'Password (8 characters or more)', autocomplete: 'new-password' });
  const submit = async () => { try { const me = await post('/api/auth/setup', { name: name.value.trim(), password: pw.value }); back.remove(); await afterLogin({ user: me.user, settings: {} }); toast('Admin account created; you are signed in.'); } catch (e) { toastError(e); } };
  [name, pw].forEach((i) => { i.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') submit(); }; });
  const back = modal('Create the admin account', h('p', { class: 'muted' }, `Right now ${S.APP_NAME} is open to anyone who can reach it. Once an admin exists, visitors browse read-only, signed-in users keep their settings on the server, and only editors and admins can change the library or the settings.`),
    h('div', { class: 'stack',}, name, pw),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'), h('button', { class: 'pillbtn on', onclick: submit }, 'Create')));
  name.focus();
}
export function accountDialog() {
  const cur = h('input', { class: 'einput', type: 'password', placeholder: 'Current password', autocomplete: 'current-password' });
  const pw = h('input', { class: 'einput', type: 'password', placeholder: 'New password (8 characters or more)', autocomplete: 'new-password' });
  [cur, pw].forEach((i) => { i.onkeydown = (e) => e.stopPropagation(); });
  // how this browser is signed in, and so what Sign out does
  const how = h('p', { class: 'muted small' });
  const outBtn = h('button', { class: 'pillbtn toolbar small stack mt8', onclick: async () => {
    back.remove(); const r = await post('/api/auth/logout', {});
    if (r && r.cloudflareLogout) { location.href = r.cloudflareLogout; return; }  // Cloudflare's own sign-out page ends its session too
    if (S.REQUIRE_LOGIN) { location.reload(); return; } S.USER = null; SETTINGS.user = null; renderUserMenu(); await pollStatus(); applyBadgePref(); applyTheme(); S.viewKey = null; onRoute(); toast('Signed out.'); } }, 'Sign out');
  api('/api/auth/me').then((me) => {
    const by = me.signedInBy;
    fill(how, h('b', null, 'Signed in: '), by === 'cloudflare' ? (me.throughCloudflare ? 'through Cloudflare Access, from outside your network.' : 'through Cloudflare Access; this browser stays signed in on your home network too.')
      : by === 'proxy' ? 'at the sign-in page of your proxy; sign out there.' : by === 'oneclick' ? 'here, with one click (no password).' : 'here, with a password.');
    outBtn.textContent = by === 'cloudflare' && me.throughCloudflare ? 'Sign out (of Cloudflare too)' : 'Sign out';
    outBtn.title = by === 'cloudflare' ? 'Ends the session here and Cloudflare Access’s, so Cloudflare asks for sign-in again next time' : '';
    if (by === 'proxy') outBtn.remove();
  }).catch(() => {});
  const back = modal(`${S.USER.name} · ${S.USER.role}`,
    h('p', { class: 'muted' }, S.USER.role === 'admin' ? 'You can change the library, the settings and the users.' : S.USER.role === 'editor' ? 'You can change the library (edits, tags, faces, places).' : 'You can browse, and keep favorites, albums and display settings.'),
    h('h4', null, 'Who you are in People'),
    h('div', {}, avatarOf(S.USER, true), h('span', null, S.USER.person && S.tagById.get(S.USER.person) ? S.tagById.get(S.USER.person).name : 'not linked'),
      S.IS_ADMIN ? h('button', { class: 'pillbtn', onclick: () => personPicker(async (t) => { try { S.USER = await post('/api/auth/person', { person: t.id }); renderUserMenu(); back.remove(); accountDialog(); } catch (e) { toastError(e); } }) }, S.USER.person ? 'Change' : 'Link to a person')
        : h('span', { class: 'muted' }, S.USER.person ? '' : '(an admin can link your account in Settings › Users)'),
      S.USER.person ? h('a', { class: 'lnk', href: hashFor('photos', photosWith({ tag: S.USER.person })), onclick: () => back.remove() }, 'Photos of me') : null),
    how,
    S.VIA_PROXY ? null : [h('h4', null, 'Change password'), h('div', {}, cur, pw)],
    S.VIA_PROXY ? null : h('div', {}, h('button', { class: 'pillbtn', onclick: async () => { try { await post('/api/auth/password', { current: cur.value, password: pw.value }); toast('Password changed.'); cur.value = pw.value = ''; } catch (e) { toastError(e); } } }, 'Change password')),
    h('div', { class: 'modal-btns' }, outBtn,
      h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Done')));
}
export function usersSection() {
  const body = h('div');
  const wrap = h('div', null, h('h2', null, 'Users'),
    h('div', { class: 'muted', style: 'max-width:820px' }, 'Visitors who are not signed in browse read-only with display settings in their browser. Viewers keep settings, favorites and albums on the server; editors can also change the library; admins can also change settings, run maintenance and manage users.'),
    h('div', { class: 'srow' }, h('div', null, h('b', null, 'Require sign-in'), h('div', { class: 'muted', style: 'font-size:13px' }, 'Visitors see only the sign-in screen; nothing from the library (not even thumbnails) is served without an account. MCP clients then need the admin token as a bearer header (see the README).')),
      h('label', { style: 'display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap' }, h('input', { type: 'checkbox', id: 'reqlogin', checked: S.REQUIRE_LOGIN, disabled: !S.USERS_EXIST, onchange: async (e) => {
        try { await post('/api/settings', { require_login: e.target.checked }); S.REQUIRE_LOGIN = e.target.checked; toast(S.REQUIRE_LOGIN ? 'Visitors now have to sign in.' : 'Visitors can browse again.'); } catch (err) { e.target.checked = !e.target.checked; toastError(err); } } }), 'Require sign-in')), body);
  const draw = async () => {
    let d; try { d = await api('/api/users'); } catch (e) { fill(body, errorBox(e)); return; }
    const name = h('input', { class: 'einput', placeholder: 'User name', style: 'width:160px' }), pw = h('input', { class: 'einput', type: 'password', placeholder: 'Password', style: 'width:160px' });
    const email = h('input', { class: 'einput', type: 'email', placeholder: 'Email (optional)', autocomplete: 'off', style: 'width:200px' });
    const role = h('select', null, d.roles.map((r) => h('option', { value: r }, r)));
    const nopw = h('label', { style: 'display:flex;gap:6px;align-items:center;font-size:13px' }, h('input', { type: 'checkbox', onchange: (e) => { pw.disabled = e.target.checked; if (e.target.checked) { pw.value = ''; role.value = 'viewer'; } } }), 'no password (one-click sign-in, viewer only)');
    [name, pw, email].forEach((i) => { i.onkeydown = (e) => e.stopPropagation(); });
    const act = async (fn, after = draw) => { try { await fn(); } catch (err) { toastError(err); } after(); };
    const setPerson = (u, t) => act(async () => { await post(`/api/users/${u.id}`, { person: t ? t.id : null }); if (S.USER && S.USER.id === u.id) { S.USER.person = t ? t.id : null; renderUserMenu(); } });
    fill(body, h('table', { class: 'utable' },
      h('thead', null, h('tr', null, ['Account', 'Email', 'Person in People', 'Role', 'Password', ''].map((c) => h('th', null, c)))),
      h('tbody', null, d.users.map((u) => h('tr', { 'data-user': u.name },
        h('td', null, h('span', { class: 'ucell' }, avatarOf(u), h('span', null, u.name, u.id === S.USER?.id ? h('span', { class: 'muted' }, ' (you)') : null))),
        h('td', null, u.email || h('span', { class: 'muted' }, 'none'), ' ',
          h('button', { type: 'button', class: 'lnk', onclick: async () => { const v = await promptDialog(`Email address for ${u.name} (empty to clear)`, u.email || ''); if (v !== null) act(async () => { await post(`/api/users/${u.id}`, { email: v.trim() }); }); } }, u.email ? 'Change' : 'Set')),
        h('td', null, u.person && S.tagById.get(u.person) ? S.tagById.get(u.person).name : h('span', { class: 'muted' }, 'not linked'), ' ',
          h('button', { type: 'button', class: 'lnk', onclick: () => personPicker((t) => setPerson(u, t)) }, u.person ? 'Change' : 'Link'), u.person ? [' · ', h('button', { type: 'button', class: 'lnk', onclick: () => setPerson(u, null) }, 'Unlink')] : null),
        h('td', null, h('select', { onchange: (e) => act(async () => { await post(`/api/users/${u.id}`, { role: e.target.value }); toast(`${u.name} is now ${e.target.value}.`); }) }, d.roles.map((r) => h('option', { value: r, selected: r === u.role }, r)))),
        h('td', null, u.passwordless ? h('span', { class: 'muted' }, 'none (one-click)') : 'set', ' ',
          h('button', { type: 'button', class: 'lnk', onclick: async () => { const p = await promptDialog(`New password for ${u.name}`, { type: 'password', placeholder: '8 characters or more', ok: 'Set password' }); if (p) act(async () => { await post(`/api/users/${u.id}`, { password: p }); toast('Password set.'); }, () => {}); } }, 'Set'),
          u.role === 'viewer' && !u.passwordless ? [' · ', h('button', { type: 'button', class: 'lnk', title: 'One-click sign-in from the account picker', onclick: async () => { if (await confirmDialog(`Remove ${u.name}’s password? Anyone who can reach ${S.APP_NAME} can then sign in as ${u.name} with one click.`)) act(() => post(`/api/users/${u.id}`, { password: '' })); } }, 'Remove')] : null),
        h('td', { style: 'text-align:right' }, u.id !== S.USER?.id ? h('button', { class: 'pillbtn', onclick: async () => { if (await confirmDialog(`Delete the user ${u.name}?`, { ok: 'Delete', danger: true })) act(() => post(`/api/users/${u.id}`, { delete: true })); } }, 'Delete') : null))))),
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px' }, name, email, pw, role, nopw, h('button', { class: 'pillbtn on', onclick: async () => {
        try { await post('/api/users', { name: name.value.trim(), email: email.value.trim(), password: nopw.querySelector('input').checked ? '' : pw.value, role: nopw.querySelector('input').checked ? 'viewer' : role.value }); toast(`Added ${name.value.trim()}.`); } catch (err) { toastError(err); } draw();
      } }, icon('add'), 'Add user')));
  };
  draw();
  wrap.append(proxySection());
  return wrap;
}
// sign-in at a reverse proxy (Cloudflare Access, Authelia behind nginx...): who signed in there is signed in here
function proxySection() {
  const box = h('div');
  const draw = async () => {
    let d; try { d = await api('/api/settings/proxy'); } catch (e) { fill(box, errorBox(e)); return; }
    const v = d.values, env = new Set(d.fromEnv || []);
    const lock = (k, el) => { if (env.has(k)) { el.disabled = true; el.title = 'Set in iopix.env'; } el.onkeydown = (e) => e.stopPropagation(); return el; };
    const inp = (k, ph, w = 320) => lock(k, h('input', { class: 'einput', value: v[k] || '', placeholder: ph, style: `margin:0;width:${w}px;max-width:100%` }));
    const modeSel = lock('AUTH_PROXY', h('select', null, [['off', 'Off'], ['cloudflare', 'Cloudflare Access'], ['haproxy', 'HAProxy'], ['nginx', 'nginx'], ['caddy', 'Caddy'], ['traefik', 'Traefik'], ['header', 'Another proxy']]
      .map(([k, l]) => h('option', { value: k, selected: (v.AUTH_PROXY || 'off').toLowerCase() === k }, l))));
    const team = inp('CF_ACCESS_TEAM', 'myteam.cloudflareaccess.com'), aud = inp('CF_ACCESS_AUD', 'Application Audience (AUD) tag', 520);
    const header = inp('AUTH_PROXY_HEADER', 'default: ' + (d.mode === 'cloudflare' ? 'Cf-Access-Authenticated-User-Email' : 'Remote-User, X-Forwarded-User, X-Auth-Request-Email…'));
    const trusted = inp('AUTH_PROXY_TRUSTED', 'default: 127.0.0.1, ::1');
    const create = lock('AUTH_PROXY_CREATE', h('select', null, [['', 'Gets the sign-in screen (default)'], ['deny', 'Is denied access'], ['viewer', 'Gets a viewer account'], ['editor', 'Gets an editor account']]
      .map(([k, l]) => h('option', { value: k, selected: (v.AUTH_PROXY_CREATE || '') === k }, l))));
    const row = (label, detail, el) => h('div', { class: 'srow' }, h('div', null, h('b', null, label), h('div', { class: 'muted', style: 'font-size:13px' }, detail)), el);
    const cf = h('div', null,
      row('Cloudflare team domain', 'Zero Trust › Settings › Team name and domain (the address the Access sign-in page is on).', team),
      row('Application Audience (AUD) tag', 'Zero Trust › Access › Applications › this app › Overview. With both set, the token Cloudflare signs is checked and the email comes from it; nothing else is believed.', aud));
    const other = h('div', null,
      row('User header', 'The request header the proxy puts the signed-in user (name or email) in. Comma-separate several.', header),
      row('Proxy addresses', 'The header is believed only from these addresses or networks (where the proxy connects from).', trusted));
    const seen = d.seen && d.seen.iss ? h('div', { class: 'muted', style: 'font-size:13px;margin:6px 0' }, `Last request through your tunnel (its claims as sent, unverified until saved): team ${String(d.seen.iss).replace(/^https:\/\//, '')}, AUD ${[].concat(d.seen.aud || []).join(', ')}${d.seen.email ? ', ' + d.seen.email : ''}${d.seen.account !== undefined ? (d.seen.account ? ' (account ' + d.seen.account + ')' : ' (no account)') : ''}. `,
      env.has('CF_ACCESS_TEAM') || env.has('CF_ACCESS_AUD') ? null : h('button', { type: 'button', class: 'lnk', onclick: () => { team.value = String(d.seen.iss).replace(/^https:\/\//, ''); aud.value = [].concat(d.seen.aud || [])[0] || ''; } }, 'Use these')) : null;
    const show = () => { cf.style.display = modeSel.value === 'cloudflare' ? '' : 'none'; other.style.display = modeSel.value === 'off' ? 'none' : ''; rest.style.display = modeSel.value === 'off' ? 'none' : ''; };
    const rest = h('div', null, row('Someone with no account here', 'When the proxy lets in a person whose email or name matches no account. Denied: a “No access” page, with no way to sign in here.', create));
    modeSel.onchange = show;
    const state = d.mode === 'off' ? 'Off: only the sign-in here.' : d.mode === 'cloudflare' ? (d.cloudflareVerified ? 'On: Cloudflare Access tokens are verified.' : 'On, but without the team domain and AUD tag: the header is believed from the proxy addresses only.') : `On (${d.mode}): the user header is believed from ${d.trusted.join(', ')}.`;
    fill(box, h('h2', null, 'Sign-in at a proxy'),
      h('div', { class: 'muted', style: 'max-width:820px' }, 'Someone signed in at your reverse proxy (Cloudflare Access on a tunnel, or Authelia, Authentik or oauth2-proxy behind HAProxy, nginx, Caddy or Traefik) is signed in here too, matched to an account by email, then name. Everyone else still uses the sign-in here; requests from outside through Cloudflare can only use accounts with a password.'),
      h('div', { style: 'margin:8px 0' }, h('b', null, state)),
      env.size ? h('div', { class: 'muted', style: 'font-size:13px' }, `Set in iopix.env (change them there): ${[...env].join(', ')}`) : null,
      row('Proxy', 'Which reverse proxy signs people in.', modeSel), cf, seen, other, rest,
      h('div', { style: 'margin-top:8px' }, h('button', { class: 'pillbtn on', onclick: async () => {
        const body = {};
        [['AUTH_PROXY', modeSel], ['CF_ACCESS_TEAM', team], ['CF_ACCESS_AUD', aud], ['AUTH_PROXY_HEADER', header], ['AUTH_PROXY_TRUSTED', trusted], ['AUTH_PROXY_CREATE', create]]
          .forEach(([k, el]) => { if (!env.has(k)) body[k] = el.value.trim(); });
        try { await post('/api/settings/proxy', body); toast('Proxy sign-in saved.'); draw(); } catch (e) { toastError(e); }
      } }, 'Save')));
    show();
  };
  draw();
  return box;
}
export function showAnalysis() { go('maintenance', null, true); }  // the old #/analysis page now lives in Maintenance; keep bookmarks working
