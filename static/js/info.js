/** The viewer's info panel: every section of it (date, file, people, tags, location, details, AI, edit log), the
 *  Actions menu, and the suggestion helpers the suggestion pages share (tagOnly, dismissSuggestion). */
import { TOAST_LONG, api, date, dfDay, dfLong, dfTime, fill, fmtBytes, fmtDist, fmtDur, fmtHeight, fmtKnown, h, icon, nf, plural, post, precOf } from './core.js';
import { S, faceUrl, go, infoHidden, infoPrefsEditor, photosWith, pollSoon, toast, toastError } from './state.js';
import { setBadges } from './grid.js';
import { albumDialog, histEntry, movePhotos, renamePhoto } from './actions.js';
import { dateEditor, detectFaces, editControls, editPhotos, locationDialog, metadataEditor, tagPicker, templatePicker } from './editing.js';
import { loadLeaflet, loadMaps, mapLink, streetView, tiles } from './map.js';
import { pollStatus } from './main.js';
import { LB, TRANSFORMS, closeLightbox, details, editMenu, filterFromLightbox, openLightbox, popMenu, setPhotoParam, showItem, toggleInfo, transformPhotos } from './lightbox.js';
import { faceMenu, faceOp, faceRegions, personPicker, removeUnnamedFaces, setTagIcon, startDrawFace, tagOnlyMenu, toggleFaces } from './faces.js';

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
/** A year for a photo whose date is unknown, from the library's clues about it (dateclues.py: ages, the people's dated
 * years, look-alikes, the camera): shown with its clues and the button that uses it. */
function suggestRow(d) {
  const g = d.dateSuggestion;
  const why = (g.clues || []).map((c) => c.text).join(' · ');
  return h('div', { class: 'irow' }, icon('spark'), h('div', null,
    h('div', { class: 'a' }, `Looks like about ${g.year}`, h('span', { class: 'muted' }, ` (${g.lo}–${g.hi})`)),
    h('div', { class: 'b' }, `${why}. `,
      S.EDITING ? h('button', { type: 'button', class: 'lnk', title: `Save the date as the year ${g.year} only (undo from the edit log)`, onclick: () => editPhotos([d.id], { date_precision: 'year', taken: String(g.year) }) }, `Use ${g.year}`) : null,
      ' · ', h('a', { class: 'lnk', href: '#/datesuggest' }, 'All suggestions'))));
}

/** Where a photo without a position was probably taken (placeguess.py): the place, how far off it may be, the clue, and Use. */
function placeSuggestRow(d) {
  const g = d.placeSuggestion;
  const where = [g.place, g.region, g.country].filter(Boolean).join(', ') || `${g.lat.toFixed(4)}, ${g.lon.toFixed(4)}`;
  const within = g.km < 1 ? `within ${Math.round(g.km * 1000)} m` : `within ${g.km.toFixed(1)} km`;
  return h('div', { class: 'irow' }, icon('spark'), h('div', null,
    h('div', { class: 'a' }, `Probably ${where}`, h('span', { class: 'muted' }, ` (${within})`)),
    h('div', { class: 'b' }, `${g.text.charAt(0).toUpperCase()}${g.text.slice(1)}. `,
      S.EDITING ? h('button', { type: 'button', class: 'lnk', title: 'Save this position, marked as approximate (undo from the edit log)', onclick: () => editPhotos([d.id], { gps: { lat: g.lat, lon: g.lon, accuracy: Math.round(g.km * 1000) } }) }, 'Use') : null,
      ' · ', h('a', { class: 'lnk', href: '#/placesuggest' }, 'All suggestions'))));
}

/** The photo quality row of the info panel: focus and look as ranks in the library, closed eyes, and why it looks like a reject. */
function qualityRow(d) {
  const q = d.quality, pct = (v) => Math.round(v * 100);
  const lines = [
    q.focusRank != null ? `${d.focusn != null ? 'In focus more than' : 'Sharper than'} ${pct(q.focusRank)}% of your photos` : null,
    q.aestheticRank != null ? `Looks better than ${pct(q.aestheticRank)}% of them (aesthetic score ${q.aesthetic.toFixed(1)} / 10)` : null,
  ].filter(Boolean);
  const small = [
    q.eyesChecked ? (q.eyesClosed ? `Eyes closed on ${q.eyesClosed} of ${q.eyesChecked} ${q.eyesChecked === 1 ? 'face' : 'faces'} checked` : `Eyes open on ${q.eyesChecked === 1 ? 'the face' : `all ${q.eyesChecked} faces`} checked`) : null,
    d.faceq != null ? `Face sharpness ${pct(d.faceq)} / 100` : null,
    d.bright != null && (d.bright < 0.2 || d.clip_lo > 0.5) ? 'Very dark' : null,
    d.bright != null && (d.bright > 0.8 || d.clip_hi > 0.3) ? 'Very bright' : null,
  ].filter(Boolean);
  const reject = q.reject.length ? h('div', { class: 'b' }, h('b', { style: 'font-weight:500' }, 'Likely reject: '), q.rejectLabels.join(', ').toLowerCase()) : null;  // a suggestion only: nothing is ever marked
  return h('div', { class: 'irow' }, icon('spark'), h('div', null, lines.map((t) => h('div', { class: 'a' }, t)), small.length ? h('div', { class: 'b' }, small.join(' · ')) : null, reject));
}

/** What every section of the info pane is built from: the photo's details `d`, which sections are shown, and the
 * small builders (a link that filters the grid, a one-field save, a row with an icon, a tag chip). */
function infoContext(d, show) {
  const lnk = (text, obj) => h('button', { type: 'button', class: 'lnk', onclick: () => filterFromLightbox(obj) }, text);
  const saveOne = (ch) => editPhotos([d.id], ch);
  const irow = (ic, a, b) => h('div', { class: 'irow' }, icon(ic), h('div', null, h('div', { class: 'a' }, a), b ? h('div', { class: 'b' }, b) : null));
  const sub = (t) => h('div', { class: 'sub' }, t);
  const peopleTags = d.tags.filter((t) => t.person), tags = d.tags.filter((t) => !t.person);
  const chip = (t) => h('span', { class: 'ichip', title: t.full.replaceAll('/', ' › '), onclick: () => filterFromLightbox({ tag: t.id }) },
    t.person ? h('img', { src: faceUrl(t.id), alt: '' }) : null, t.name,
    S.CAN_MANAGE && d.id !== S.tagById.get(t.id)?.icon ? h('button', { class: 'tx', title: t.person ? `Use this photo as ${t.name}’s badge picture (digiKam must be closed)` : `Use this photo as the thumbnail for “${t.name}” (digiKam must be closed)`,
      onclick: (e) => { e.stopPropagation(); setTagIcon(t, d.id); } }, icon('photo')) : null);
  const ex = d.extra || {}, cr = ex.copyright || {}, vid = ex.video || {};
  return { d, show, lnk, saveOne, irow, sub, chip, ex, cr, vid, tags, peopleTags };
}
/** When the photo was taken, with the suggestion and the date editor when there is one. */
function infoDate(c) {
  const { d, show, lnk, irow } = c;
  // an approximate date shows only what is known ("1992", "May 1992", "Date unknown"), never the placeholder day
  const dateRow = (x) => {
    const n = precOf(x.datePrecision), y = (x.day || '').slice(0, 4), ym = (x.day || '').slice(0, 7);
    if (!n) return irow('calendar', lnk(dfLong.format(date(x.ts)), { from: x.day, to: x.day, sort: 'old' }), dfTime.format(date(x.ts)));
    const span = n === 2 ? { from: `${y}-01-01`, to: `${y}-12-31`, sort: 'old' } : n === 1 ? { from: `${ym}-01`, to: `${ym}-${String(new Date(Date.UTC(+y, +ym.slice(5), 0)).getUTCDate()).padStart(2, '0')}`, sort: 'old' } : null;
    const text = fmtKnown(x.ts, n);
    return irow('calendar', span ? lnk(text, span) : text,
      h('span', { class: 'approx', style: 'margin:4px 0 0', title: 'Saved with a placeholder date where a full date is required; the tag under “Date approximate” says how much is known' },
        ['', 'Approximate date · the day is not known', 'Approximate date · only the year is known', `Sorted under ${dfDay.format(date(x.ts))}, a placeholder`][n]));
  };
  return show('date') ? [dateRow(d), d.dateSuggestion ? suggestRow(d) : null,
      S.EDITING ? h('div', { hidden: true, style: 'margin:0 0 8px 40px' }, dateEditor([d.id], d.taken, null, d.datePrecision)) : null] : null;
}

/** The file: name, size and format, its folder, and its other versions. */
function infoFile(c) {
  const { d, show, lnk, irow } = c;
  const mp = d.w * d.h / 1e6;
  return show('file') ? [irow(d.video ? 'video' : 'photo', d.name, [d.video ? null : `${mp >= 1 ? mp.toFixed(1) : mp.toFixed(2)} MP`, `${d.w} × ${d.h}`, fmtBytes(d.size), d.fmt].filter(Boolean).join(' · ')),
      d.album ? irow('folder', lnk(d.album.path.replace(/^\//, '') || d.album.name, { album: d.album.id, sort: 'old' }), 'Folder') : null,
      d.versions?.length ? irow('dup', h('span', null, d.versions.map((x, k) => [k ? ' · ' : '', x.id === d.id ? h('b', null, x.role === 'original' ? 'Original' : 'This version')
        : h('button', { type: 'button', class: 'lnk', onclick: () => openLightbox({ id: d.versions.map((y) => y.id) }, k, true) }, x.role === 'original' ? 'Original' : `Version ${k}`)])), 'Versions') : null] : null;
}

/** The rating, title and caption controls, and the note on where an edit went (with Undo). */
function infoEditor(c) {
  const { d, saveOne } = c;
  if (!S.EDITING) return null;
  return [h('div', { style: 'height:10px' }),  // the rating, title and caption controls follow; they need no heading
      editControls({ rating: Math.max(d.rating, 0) }, saveOne),
      h('input', { class: 'einput', placeholder: 'Title', value: d.title || '', onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') e.target.blur(); },
        onchange: (e) => saveOne({ title: e.target.value }) }),
      h('textarea', { class: 'einput', placeholder: 'Caption', rows: 2, onkeydown: (e) => e.stopPropagation(), onchange: (e) => saveOne({ caption: e.target.value }) }, d.caption || ''),
      d.edited ? h('div', { class: 'enote' }, S.WRITE_FILES ? 'Changed here and saved to the sidecar and the image file. ' : 'Changed here and saved to the sidecar. ',
        S.NO_DK ? '' : S.AUTO_DB ? 'It goes into digiKam’s database by itself within a minute (once digiKam is closed). ' : 'digiKam shows it after “Reread Metadata From File”, or apply it to digiKam’s database from Explore. ',
        h('button', { type: 'button', class: 'lnk', onclick: async () => { try { await post('/api/edit/revert', { ids: [d.id] }); } catch (e) { toastError(e); return; } details.delete(d.id); toast('Edits undone; sidecar restored.'); S.keepView = true; clearTimeout(S.pollTimer); S.pollTimer = setTimeout(async () => { await pollStatus(); if (LB.open && LB.src.id[LB.i] === d.id) showItem(); }, 1500); } }, 'Undo')) : null];
}

/** One row per face (named, suggested, unknown); a person tag without a face region gets a row with the badge picture. */
function infoPeople(c) {
  const { d, show, peopleTags } = c;
  if (!show('people')) return null;
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
        h('span', { class: 'nm' }, f.kind === 'confirmed' || f.kind === 'tagonly' ? [f.name, d.ages?.[f.tag] ? h('span', { class: 'muted', title: 'Age in this photo, from the birthday in ioContacts' }, ` · ${d.ages[f.tag]}`) : null] : f.kind === 'suggested' ? [h('i', null, 'Suggestion: '), f.name] : h('i', null, 'Unknown face')),
        S.CAN_MANAGE ? h('span', { class: 'muted smaller' }, f.kind === 'tagonly' ? 'no face box ▾' : 'options ▾') : null)))];
}

/** The keyword tags as chips, with the picker to add one. */
function infoTags(c) {
  const { show, saveOne, chip, tags } = c;
  return show('tags') && (tags.length || S.EDITING) ? [h('h4', null, 'Tags'), h('div', { class: 'ichips' }, tags.map((t) => { const c = chip(t);
      if (S.EDITING) c.append(h('button', { class: 'tx', title: 'Remove this tag', onclick: (e) => { e.stopPropagation(); saveOne({ remove_tags: [t.id] }); } }, icon('close'))); return c; })),
      S.EDITING ? h('div', { class: 'mt8',}, tagPicker((t) => saveOne({ add_tags: [t.id] }))) : null] : null;
}

/** Where the photo was taken: the place names, the small map, the coordinates and the links out. */
function infoLocation(c, mapEl) {
  const { d, show, lnk, ex } = c;
  return [
    show('location') && S.EDITING && d.lat == null ? h('div', { class: 'irow' }, icon('place'), h('div', null, h('button', { type: 'button', class: 'lnk', onclick: () => locationDialog([d.id], null) }, 'Set location on the map'))) : null,
    show('location') && d.lat == null && d.placeSuggestion ? placeSuggestRow(d) : null,
    show('location') && d.lat != null ? [h('h4', null, 'Location', S.EDITING ? h('button', { type: 'button', class: 'lnk', style: 'margin-left:10px;text-transform:none;letter-spacing:0;font-weight:400', onclick: () => locationDialog([d.id], { lat: d.lat, lon: d.lon, alt: d.alt, accuracy: ex.gpsAccuracy }) }, 'Change') : null),
      d.place ? h('div', { class: 'a', style: 'margin-bottom:6px' }, lnk(d.place, { place: d.place, ...(d.region ? { region: d.region } : {}) }),
        d.region ? [', ', lnk(d.region, { region: d.region })] : null, d.country ? [', ', lnk(d.country, { country: d.country })] : null) : null,
      ex.gpsAccuracy ? h('div', { class: 'approx', title: 'Only the town is known: the pin marks its center' }, icon('place'),
        `Approximate location · within about ${fmtDist(ex.gpsAccuracy)}`) : null,
      mapEl,
      h('div', { class: 'irow' }, icon('place'), h('div', null, h('div', { class: 'a' }, `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}`),
        h('div', { class: 'b' }, d.alt != null && d.alt !== 0 ? `${fmtHeight(d.alt)} altitude · ` : null, ex.orientationGps != null ? `facing ${Math.round(ex.orientationGps)}° · ` : null,
          h('a', { href: mapLink(d.lat, d.lon), target: '_blank', rel: 'noopener' }, S.MAPS.linkLabel), ' · ', h('button', { type: 'button', class: 'lnk', onclick: () => streetView(d.lat, d.lon, d.name) }, 'Street View'))))] : null,
  ];
}

/** The Details and Camera lists: rating, creator and copyright, the file's other properties, video facts; the camera, lens and exposure. */
function infoDetails(c) {
  const { d, show, lnk, ex, cr, vid } = c;
  const exposure = [d.aperture ? `ƒ/${+d.aperture.toFixed(1)}` : null,
    d.exposure ? (d.exposure < 1 ? `1/${Math.round(1 / d.exposure)} s` : `${+d.exposure.toFixed(1)} s`) : null,
    d.focal ? `${+d.focal.toFixed(1)} mm${d.focal35 && Math.round(d.focal35) !== Math.round(d.focal) ? ` (${Math.round(d.focal35)} mm eq.)` : ''}` : null,
    d.iso ? `ISO ${d.iso}` : null, d.flash != null && (d.flash & 1) ? 'Flash' : null].filter(Boolean).join(' · ');
  const stars = d.rating > 0 ? '★'.repeat(d.rating) + '☆'.repeat(5 - d.rating) : null;
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
  return [
    show('details') && kv.length ? [h('h4', null, 'Details'), h('div', { class: 'kv' }, kv)] : null,
    show('camera') && ckv.length ? [h('h4', null, 'Camera'), h('div', { class: 'kv' }, ckv)] : null,
  ];
}

/** Everything a model produced, together at the end: description, detected content, objects, text, sharpness, bursts, look-alikes. */
/** Tags each suggested person on its photo without a face box (digiKam's own person tag, sidecar included); false when any failed. */
export async function tagOnly(list) {
  let r;
  try { r = await post('/api/faces/batch', { ops: list.map((x) => ({ op: 'tagonly', image: x.id, tag: x.tag })) }); } catch (e) { toastError(e); return false; }
  if (r.failed?.length) { toast(`${plural(r.failed.length, 'photo')} not tagged: ${r.failed[0].error}`, null, null, TOAST_LONG); return false; }
  setBadges(list.map((x) => x.id), { faces: true });
  list.forEach((x) => details.delete(x.id));  // a photo open in the viewer shows the person at its next draw
  toast(list.length === 1 ? `Added ${list[0].name}.` : `${plural(list.length, 'photo')} tagged.`);
  return true;
}
/** Never suggest `tag` for this photo again (jersey, clothing), or say it is the right way up (rotation); throws on failure. */
export function dismissSuggestion(kind, id, tag) {
  return kind === 'rotation' ? post('/api/rotation/dismiss', { photos: [id], on: true }) : post('/api/suggestions/dismiss', { kind, pairs: [[id, tag]] });
}
/** One suggestion line of the info panel: its text, the button that applies it, "not this one" (disabled while it posts) and the link to the page. */
function suggestionRow(attrs, text, act, dismiss, all) {
  const noBtn = dismiss ? h('button', { type: 'button', class: 'lnk', onclick: async (e) => {
    const b = e.currentTarget; b.disabled = true;
    try { await dismissSuggestion(dismiss.kind, dismiss.id, dismiss.tag); toast(dismiss.said); } catch (err) { toastError(err); b.disabled = false; }
  } }, dismiss.label) : null;
  return h('div', attrs, text, act ? h('button', { type: 'button', class: 'lnk', title: act.title, onclick: act.onclick }, act.label) : null,
    noBtn ? [' · ', noBtn] : null, ' · ', h('a', { class: 'lnk', href: all.href }, all.label));
}
/** The info panel's Tag button for a suggested person: applied at once in the viewer, with undo in the toast. */
const tagHere = (d, x) => ({ title: `Tag ${x.name} on this photo (undo from the edit log)`, label: `Tag ${x.name}`, onclick: () => faceOp({ op: 'tagonly', image: d.id, tag: x.tag }, () => faceOp({ op: 'untag', image: d.id, tag: x.tag }), `Added ${x.name}.`) });
const notThem = (kind, d, x) => ({ kind, id: d.id, tag: x.tag, label: 'Not them', said: `Not ${x.name}: noted.` });

function infoAi(c) {
  const { d, show, sub, lnk } = c;
  // everything a model produced, together at the end: description, detected content, objects, text, sharpness, bursts, look-alikes
  const ai = show('ai') ? [
    d.caption_ai ? h('div', { class: 'muted', style: 'margin:2px 0 6px;font-style:italic' }, d.caption_ai.charAt(0).toUpperCase() + d.caption_ai.slice(1)) : null,
    d.concepts?.length ? [sub('Detected'), h('div', { class: 'ichips' }, d.concepts.map((c) => h('span', { class: 'ichip', title: `The model’s guess (${Math.round(c.p * 100)}% of its vote)`, onclick: () => filterFromLightbox({ concept: c.name }) }, icon('spark'), c.name)))] : null,
    d.boxes?.length ? [sub('Objects'), h('div', { class: 'ichips' }, [...new Set(d.boxes.map((b) => b.cls))].map((c) => h('span', { class: 'ichip', onclick: () => filterFromLightbox({ obj: c }) },
      c, h('b', { style: 'font-weight:400;color:var(--fg2)' }, d.boxes.filter((b) => b.cls === c).length > 1 ? ` ×${d.boxes.filter((b) => b.cls === c).length}` : ''))))] : null,
    d.ocr ? h('div', { class: 'irow' }, icon('text'), h('div', null, h('div', { class: 'b' }, 'Text in the picture'), h('div', { class: 'a small' }, d.ocr.slice(0, 600).split(/\n+/).map((t) => t.trim()).filter(Boolean).join(' · ')))) : null,
    d.videoInfo?.transcript ? h('div', { class: 'irow' }, icon('text'), h('div', null, h('div', { class: 'b' }, `What is said${d.videoInfo.lang ? ` (${d.videoInfo.lang})` : ''}`),
      h('div', { class: 'a small' }, d.videoInfo.segments?.length ? d.videoInfo.segments.map((s, k) => [k ? ' ' : '', h('button', { type: 'button', class: 'lnk', title: `Jump to ${Math.floor(s.t / 60)}:${String(Math.floor(s.t % 60)).padStart(2, '0')}`, onclick: () => { if (LB.video) { LB.video.currentTime = s.t; LB.video.play(); } } }, s.text)]) : d.videoInfo.transcript.slice(0, 2000)))) : null,
    d.jerseys?.length ? h('div', { class: 'irow' }, icon('text'), h('div', null, h('div', { class: 'b' }, 'Jersey numbers'),
      h('div', { class: 'a small' }, [...new Set(d.jerseys.map((j) => j.number))].map((n, k) => [k ? ' · ' : '', lnk(`number ${n}`, { jersey: n })])),
      (d.jerseySuggestions || []).map((x) => suggestionRow({ class: 'b', style: 'margin-top:4px' }, `Number ${x.number} is probably ${x.name} (${x.n} named ${x.basis === 'folder' ? 'in this folder' : 'this year'}). `,
        S.EDITING ? tagHere(d, x) : null, S.EDITING ? notThem('jersey', d, x) : null, { href: '#/jerseys', label: 'All suggestions' })))) : null,
    d.rotationSuggestion ? h('div', { class: 'irow' }, icon('rotate'), h('div', null, h('div', { class: 'a' }, d.rotationSuggestion.text, h('span', { class: 'muted' }, ` (${Math.round(d.rotationSuggestion.score * 100)}% sure)`)),
      suggestionRow({ class: 'b' }, null, S.CAN_MANAGE && S.LOSSLESS ? { title: 'Lossless rotate (undo from the edit log)', onclick: () => transformPhotos([d.id], d.rotationSuggestion.op), label: TRANSFORMS.find((t) => t[0] === d.rotationSuggestion.op)?.[1] || 'Rotate' } : null,
        S.CAN_MANAGE ? { kind: 'rotation', id: d.id, label: 'It’s fine', said: 'Marked as the right way up.' } : null, { href: '#/rotation', label: 'All findings' }))) : null,
    d.clothingSuggestions?.length ? h('div', { class: 'irow' }, icon('people'), h('div', null, h('div', { class: 'b' }, 'Probably here, by the clothes'),
      d.clothingSuggestions.map((x) => suggestionRow({ class: 'a small' }, `${x.name} (${Math.round(x.score * 100)}% like ${x.n} named ${x.n === 1 ? 'photo' : 'photos'} of this event). `,
        S.EDITING ? tagHere(d, x) : null, S.EDITING ? notThem('clothing', d, x) : null, { href: '#/clothing', label: 'All matches' })))) : null,
    d.quality ? qualityRow(d) : null,
    d.burst ? h('div', { class: 'irow' }, icon('dup'), h('div', null, h('div', { class: 'a' }, lnk(`Part of a burst of ${d.burst_size} frames`, { burst: d.burst, sort: 'old' })),
      h('div', { class: 'b' }, d.burst_best ? 'Picked as the best frame' : ['Not the best frame', d.burst_best_id ? [' · ', h('button', { type: 'button', class: 'lnk', title: d.burst_best_name, onclick: () => { const i = LB.src.id.indexOf(d.burst_best_id); if (i >= 0) { LB.i = i; setPhotoParam(d.burst_best_id, false); showItem(); } else openLightbox({ id: [d.burst_best_id] }, 0, true); } }, 'open the best frame')] : null]))) : null,
    d.similar && !d.video ? h('div', { class: 'irow' }, icon('spark'), h('div', null, h('div', { class: 'a' }, lnk('More photos like this', { like: d.id })))) : null,
  ].flat().filter(Boolean) : [];
  return ai.length ? [h('h4', null, 'AI analysis'), ai] : null;
}

/** The photo's edit log, folded, last; editors only, and nothing shows for a photo never changed here. */
function infoHistory(c) {
  const { d, show } = c;
  if (!show('history') || !S.EDITING) return null;
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
}
/** The small map under Location, drawn once Leaflet is there: a pin, or the circle an approximate position may be in. */
function infoMiniMap(d, mapEl) {
  if (!mapEl) return;
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
export function renderInfo() {
  const d = LB.detail, el = LB.el.info;
  if (!d) return;
  LB.mini?.remove(); LB.mini = null;
  const hid = infoHidden(), show = (k) => !hid.has(k);
  const c = infoContext(d, show);
  const mapEl = d.lat != null && show('location') ? h('div', { class: 'imap' }) : null;
  fill(el,
    h('div', { class: 'ihead' }, h('button', { class: 'iconbtn', title: 'Close info', onclick: toggleInfo }, icon('close')), 'Info',
      h('button', { class: 'pillbtn iactions', style: 'margin-left:auto', title: 'Everything you can do with this photo', onclick: (e) => photoActions(e.currentTarget, d) }, icon('more'), 'Actions'),
      h('button', { class: 'iconbtn' + (LB.prefs ? ' on' : ''), title: 'Choose what this panel shows', onclick: () => { LB.prefs = !LB.prefs; renderInfo(); } }, icon('gear'))),
    LB.prefs ? infoPrefsEditor(renderInfo) : null,
    show('caption') && d.title ? h('div', { class: 'title' }, d.title) : null,
    show('caption') && d.caption ? h('div', { class: 'muted', style: 'margin:6px 0 10px;white-space:pre-wrap' }, d.caption) : null,
    infoDate(c),
    infoFile(c),
    infoEditor(c),
    infoPeople(c),
    infoTags(c),
    infoLocation(c, mapEl),
    infoDetails(c),
    show('file') ? [h('h4', null, 'File'), h('div', { class: 'b muted', style: 'word-break:break-all;user-select:all;font-size:12.5px' }, d.path),
    ] : null,
    infoAi(c),
    infoHistory(c));
  infoMiniMap(d, mapEl);
}
