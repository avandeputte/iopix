/** Metadata editing dialogs: rating, titles, captions, tags, dates, locations and templates. */
import { $, UTC, api, date, dfTimeLocal, fill, fmtWhen, h, icon, nf, norm, plural, post } from './core.js';
import { S, go, hashFor, photosWith, pollSoon, toast } from './state.js';
import { G } from './grid.js';
import { modal } from './actions.js';
import { placeTagsDialog } from './admin.js';
import { baseLayers, loadLeaflet, loadMaps, streetView, tiles } from './map.js';
import { LB, details, getDetail, renderInfo, showItem } from './lightbox.js';
import { awaitReindex, pollStatus, start } from './main.js';

export function applyLocal(d, ch) {
  if ('rating' in ch) d.rating = ch.rating > 0 ? ch.rating : -1;
  for (const k of ['title', 'caption']) if (k in ch) d[k] = ch[k] || null;
  if ('taken' in ch) { d.taken = ch.taken; d.ts = Math.floor(Date.UTC(+ch.taken.slice(0, 4), +ch.taken.slice(5, 7) - 1, +ch.taken.slice(8, 10), +ch.taken.slice(11, 13), +ch.taken.slice(14, 16), +ch.taken.slice(17, 19)) / 1000); d.day = ch.taken.slice(0, 10); }
  if ('gps' in ch) { d.lat = ch.gps ? ch.gps.lat : null; d.lon = ch.gps ? ch.gps.lon : null; d.alt = ch.gps ? ch.gps.alt : null; }
  if (ch.template) { d.creator = ch.template.creator || null; d.copyright = ch.template.copyrightNotice || null; (d.extra ||= {}).copyright = Object.fromEntries(Object.entries(ch.template).filter(([, v]) => v)); }
  for (const id of ch.remove_tags || []) d.tags = d.tags.filter((t) => t.id !== id);
  for (const id of ch.add_tags || []) { const t = S.tagById.get(id); if (t && !d.tags.some((x) => x.id === id)) d.tags.push({ id, name: t.name, full: t.full, person: 0 }); }
  d.edited = 1;
}
/** Every EXIF, IPTC and XMP field of a photo's file and sidecar; editable ones are changed in place, then the
 *  database is read again from the metadata. "Reread from file" is digiKam's Read Metadata From File. */
export async function metadataEditor(d) {
  const body = h('div', { class: 'mdbody' }, h('div', { class: 'muted' }, 'Reading the metadata…'));
  const filter = h('input', { class: 'einput', placeholder: 'Filter fields, e.g. Make, GPS, Keywords', style: 'margin:0;flex:1', oninput: () => draw() });
  const which = h('select', { class: 'sharepick', onchange: () => draw() }, h('option', { value: 'file' }, 'Image file'), h('option', { value: 'sidecar' }, 'Sidecar (XMP)'));
  const changed = new Map();
  const saveBtn = h('button', { class: 'pillbtn on', disabled: true, onclick: () => save() }, 'Save changes');
  let m = null;
  const where = (g) => g.startsWith('XMP-') ? (m.writesFiles ? 'sidecar and file' : 'sidecar') : (m.writesFiles ? 'file' : 'file: needs “Write metadata into image files”');
  const draw = () => {
    if (!m) return;
    const q = norm(filter.value || '');
    const rows = m[which.value].filter((x) => !q || norm(`${x.group} ${x.tag} ${x.value}`).includes(q));
    const groups = new Map();
    rows.forEach((x) => { if (!groups.has(x.group)) groups.set(x.group, []); groups.get(x.group).push(x); });
    fill(body, rows.length ? [...groups].map(([g, list]) => h('details', { class: 'mdgroup', open: groups.size < 6 || !!q },
      h('summary', null, h('b', null, g), h('span', { class: 'muted' }, ` · ${plural(list.length, 'field')}${!list.some((x) => x.editable) ? ' · read only' : S.CAN_MANAGE ? ' · edits go to the ' + where(g) : ''}`)),
      h('table', { class: 'mdtable' }, h('tbody', null, list.map((x) => {
        const key = `${x.group}:${x.tag}`;
        const cell = S.CAN_MANAGE && x.editable ? h('input', { class: 'mdin', value: changed.has(key) ? changed.get(key).value : x.value,
          oninput: (e) => { if (e.target.value === x.value) changed.delete(key); else changed.set(key, { group: x.group, tag: x.tag, value: e.target.value }); saveBtn.disabled = !changed.size; fill(saveBtn, changed.size ? `Save ${plural(changed.size, 'change')}` : 'Save changes'); } })
          : h('span', { class: x.editable ? '' : 'muted' }, x.value);
        return h('tr', null, h('td', { class: 'mdk', title: key }, x.tag), h('td', null, cell));
      }))))) : h('div', { class: 'muted' }, which.value === 'sidecar' && !m.hasSidecar ? 'This photo has no sidecar yet; the first edit creates one.' : 'No field matches.'));
  };
  const load = async () => { try { m = await api(`/api/metadata/${d.id}`); } catch (e) { fill(body, h('div', { class: 'warn' }, e.message)); return; } draw(); };
  const save = async () => {
    saveBtn.disabled = true; fill(saveBtn, 'Saving…');
    try { await post(`/api/metadata/${d.id}`, { changes: [...changed.values()] }); } catch (e) { toast(e.message, null, null, 10000); saveBtn.disabled = false; fill(saveBtn, `Save ${plural(changed.size, 'change')}`); return; }
    toast(`${plural(changed.size, 'field')} saved; the library now says the same.`); changed.clear(); fill(saveBtn, 'Save changes');
    details.delete(d.id); S.keepView = true; pollSoon(600); load();
  };
  const reread = h('button', { class: 'pillbtn', title: 'Fill the library’s fields for this photo again from its file and sidecar (digiKam: Read Metadata From File)', onclick: async () => {
    try { await post(`/api/metadata/${d.id}/reread`, {}); toast('Read again from the file and sidecar.'); details.delete(d.id); S.keepView = true; pollSoon(600); } catch (e) { toast(e.message, null, null, 9000); } } }, 'Reread from file');
  const add = S.CAN_MANAGE ? (() => {
    const g = h('select', { class: 'sharepick' }, ['XMP-dc', 'XMP-photoshop', 'XMP-iptcCore', 'XMP-xmp', 'XMP-xmpRights', 'IFD0', 'ExifIFD', 'GPS', 'IPTC'].map((x) => h('option', null, x)));
    const t = h('input', { class: 'einput', placeholder: 'Field, e.g. Headline', style: 'margin:0;width:160px' });
    const v = h('input', { class: 'einput', placeholder: 'Value', style: 'margin:0;flex:1;min-width:140px' });
    return h('div', { class: 'mdadd' }, h('span', { class: 'muted' }, 'Add a field'), g, t, v, h('button', { class: 'pillbtn', onclick: () => {
      if (!t.value.trim()) return; changed.set(`${g.value}:${t.value.trim()}`, { group: g.value, tag: t.value.trim(), value: v.value }); saveBtn.disabled = false; fill(saveBtn, `Save ${plural(changed.size, 'change')}`); toast('Added; save to write it.'); } }, 'Add'));
  })() : null;
  const back = modal(`All metadata · ${d.name}`, h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' }, filter, which), body, add,
    h('div', { class: 'modal-btns' }, S.CAN_MANAGE ? reread : null, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close'), S.CAN_MANAGE ? saveBtn : null));
  back.querySelector('.modal').classList.add('mdmodal');
  load();
}
/** Copy chosen fields from one photo to the others of a selection, through the normal edit path. */
export async function copyMetadata(ids) {
  let src = ids[0];
  const thumbs = h('div', { class: 'fcovers' }, ids.slice(0, 60).map((id) => h('img', { class: 'fcov' + (id === src ? ' on' : ''), src: `/thumb/${id}`, alt: '', onclick: (e) => {
    src = id; thumbs.querySelectorAll('.fcov').forEach((x) => x.classList.toggle('on', x === e.currentTarget)); } })));
  const F = [['rating', 'Rating'], ['labels', 'Pick and color labels'], ['title', 'Title'], ['caption', 'Caption'], ['tags', 'Keywords (added)'], ['gps', 'Location'], ['taken', 'Date and time taken'], ['rights', 'Creator and copyright']];
  const boxes = Object.fromEntries(F.map(([k]) => [k, h('input', { type: 'checkbox', checked: ['tags', 'gps', 'rights'].includes(k) })]));
  const go_ = h('button', { class: 'pillbtn on', onclick: async () => {
    const s = await getDetail(src);
    const ch = {};
    if (boxes.rating.checked) ch.rating = Math.max(0, s.rating || 0);
    if (boxes.labels.checked) { ch.pick = s.pick || 0; ch.color = s.color || 0; }
    if (boxes.title.checked) ch.title = s.title || '';
    if (boxes.caption.checked) ch.caption = s.caption || '';
    if (boxes.tags.checked) { const t = s.tags.filter((x) => !x.person).map((x) => x.full); if (t.length) ch.add_tags = t; }
    if (boxes.gps.checked) ch.gps = s.lat != null ? { lat: s.lat, lon: s.lon, alt: s.alt ?? null } : null;
    if (boxes.taken.checked && s.taken) ch.taken = s.taken.slice(0, 19);
    if (boxes.rights.checked) { const tpl = {}; if (s.creator) tpl.creator = s.creator; if (s.copyright) tpl.copyrightNotice = s.copyright; if (Object.keys(tpl).length) ch.template = tpl; }
    const targets = ids.filter((x) => x !== src);
    if (!Object.keys(ch).length || !targets.length) { toast('Nothing to copy.'); return; }
    back.remove();
    if (await editPhotos(targets, ch)) toast(`Copied to ${plural(targets.length, 'photo')}. Each change is in the change log with undo.`);
  } }, 'Copy to the others');
  const back = modal(`Copy metadata to ${plural(ids.length - 1, 'photo')}`, h('div', { class: 'muted', style: 'margin-bottom:6px' }, 'Copy from this photo (click to choose):'), thumbs,
    h('div', { class: 'expopts' }, F.map(([k, l]) => h('label', { style: 'display:flex;gap:6px;align-items:center;cursor:pointer' }, boxes[k], l))),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'), go_));
}
export async function editPhotos(ids, changes) {
  let r;
  try { r = await post('/api/edit', { ids, changes }); } catch (e) { toast('Could not save the change: ' + e.message); return false; }
  // a time shift lands differently on each photo, so those are re-fetched rather than patched locally
  for (const id of ids) { if ('shift' in changes) details.delete(id); else details.get(id)?.then((d) => applyLocal(d, changes)); }
  if (LB.open && ids.includes(LB.src.id[LB.i])) {
    const d = await getDetail(LB.src.id[LB.i]);
    if (LB.open && LB.src.id[LB.i] === d.id) { LB.detail = d; if (LB.info) renderInfo(); }  // unless the user has moved on meanwhile
    flashEdit(changes);
  }
  if (r.failed.length) toast(`${r.failed.length} could not be changed: ${r.failed[0].error}`);
  else if (ids.length > 1) toast(`Saved to ${nf.format(r.edited)} sidecar files. digiKam shows it after “Reread Metadata From File”, or apply them to its database from Explore.`);
  S.keepView = true;
  pollSoon();
  return true;
}
export function flashEdit(ch) {
  const msg = 'rating' in ch ? (ch.rating ? '★'.repeat(ch.rating) : 'Rating cleared') : null;
  if (msg) toast(msg, null, null, 1200);
}

export async function recentTags() { if (!S.RECENT_TAGS) try { S.RECENT_TAGS = (await api('/api/maintenance')).recentTags || []; } catch { S.RECENT_TAGS = []; } return S.RECENT_TAGS; }
export async function noteRecent(ids) { try { S.RECENT_TAGS = (await post('/api/tags/recent', { ids })).recent; } catch { /* cosmetic */ } }
export async function createTagThenPick(full, onPick) {
  // "Parent/Child" creates the child under an existing parent; a bare name makes a top-level tag
  const parts = full.split('/').map((s) => s.trim()).filter(Boolean);
  const name = parts.pop();
  let parent = 0;
  if (parts.length) { const p = S.META.tags.find((t) => !t.person && t.full.toLowerCase() === parts.join('/').toLowerCase()); if (!p) { toast(`Parent tag “${parts.join(' › ')}” does not exist.`); return; } parent = p.id; }
  if (!confirm(`Create the tag “${full}” in digiKam’s database? digiKam must be closed; a backup is made first.`)) return;
  let r;
  try { r = await post('/api/tags/op', { op: 'add', parent, name }); } catch (e) { toast(e.message, null, null, 9000); return; }
  await awaitReindex((m) => m.tags.some((t) => t.id === r.id));
  $('#taglist')?.remove();
  const t = S.tagById.get(r.id);
  if (t) onPick(t); else toast(`Created “${r.path}”; it will appear shortly.`);
}
export function tagPicker(onPick, placeholder = 'Add a tag…', { recent = true } = {}) {
  const pick = (t) => { noteRecent([t.id]); onPick(t); };
  const wrap = h('div', { class: 'tpick' });
  if (recent) recentTags().then((ids) => { const row = ids.map((id) => S.tagById.get(id)).filter((t) => t && !t.person); if (row.length) wrap.prepend(h('div', { class: 'recent' }, h('span', { class: 'muted' }, 'Recent: '), row.map((t) => h('span', { class: 'ichip', title: t.full.replaceAll('/', ' › '), onclick: () => pick(t) }, t.name)))); });
  wrap.append(tagPickerInput(pick, placeholder));
  return wrap;
}
export function tagPickerInput(onPick, placeholder) {
  const id = 'taglist';
  if (!$('#' + id)) document.body.append(h('datalist', { id }, S.META.tags.filter((t) => !t.person && !t.full.startsWith('People')).map((t) => h('option', { value: t.full }))));
  const inp = h('input', { class: 'pagefilter', style: 'margin:0;width:100%', placeholder, list: id });
  const tryPick = () => { const t = S.META.tags.find((x) => !x.person && x.full.toLowerCase() === inp.value.trim().toLowerCase()); if (t) { inp.value = ''; onPick(t); } };
  inp.oninput = tryPick;
  inp.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') { tryPick(); if (inp.value.trim() && S.CAN_MANAGE) { const v = inp.value.trim(); inp.value = ''; createTagThenPick(v, onPick); } else if (inp.value) toast('Pick an existing tag from the list.'); } };
  if (S.CAN_MANAGE) inp.title = 'Type an existing tag, or a new one as “Parent/New tag” and press Enter to create it';
  return inp;
}
export function editControls(cur, save) {
  // cur: {rating} of a single photo, or {} for a batch (digiKam's pick and color labels are not offered here)
  return [
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Rating'), h('div', { class: 'stars' }, [1, 2, 3, 4, 5].map((n) => h('button', { class: n <= (cur.rating || 0) ? 'on' : '', title: `${n} star${n > 1 ? 's' : ''}`,
      onclick: () => save({ rating: cur.rating === n ? 0 : n }) }, icon('star'))), h('button', { class: 'clr', title: 'No rating', onclick: () => save({ rating: 0 }) }, icon('close')))),
  ];
}
export function tagDialog(ids) {
  const back = modal(`Tag ${plural(ids.length, 'photo')}`,
    h('p', { class: 'muted' }, 'Pick a recent tag, type to find one, or type “Parent/New tag” and press Enter to create it. Each tag is applied as soon as you pick it.'),
    tagPicker((t) => editPhotos(ids, { add_tags: [t.id] })),
    h('div', { class: 'erow', style: 'margin-top:14px' }, h('span', { class: 'el' }, 'Remove'), tagPicker((t) => editPhotos(ids, { remove_tags: [t.id] }), 'Remove a tag from all…', { recent: false })),
    S.CAN_MANAGE ? h('div', { style: 'margin-top:14px' }, h('a', { class: 'lnk', onclick: () => { back.remove(); placeTagsDialog(ids); } }, 'Add place tags from GPS positions…')) : null,
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn on', onclick: () => back.remove() }, 'Done')));
}
export function editDialog(ids) {
  const save = (ch) => editPhotos(ids, ch);
  const back = modal(`Edit ${plural(ids.length, 'item')}`,
    h('p', null, 'Each change is saved straight away to the XMP sidecar files. digiKam’s database is not touched.'),
    editControls({}, save),
    ...(() => {  // title and caption for every selected photo, applied on demand (not on each keystroke)
      const title = h('input', { class: 'einput', placeholder: 'Title for all', style: 'margin:0;flex:1', onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') e.target.nextSibling.click(); } });
      const caption = h('textarea', { class: 'einput', placeholder: 'Caption for all', rows: 2, style: 'margin:0;flex:1', onkeydown: (e) => e.stopPropagation() });
      const apply = (field, el) => h('button', { class: 'pillbtn', style: 'height:32px', onclick: async () => {
        const v = el.value.trim();
        if (!v && !confirm(`Clear the ${field} of ${plural(ids.length, 'photo')}?`)) return;
        if (await save({ [field]: v })) toast(`${field[0].toUpperCase() + field.slice(1)} ${v ? 'set' : 'cleared'} on ${plural(ids.length, 'photo')}.`);
      } }, 'Apply');
      return [h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Title'), title, apply('title', title)),
        h('div', { class: 'erow', style: 'align-items:flex-start' }, h('span', { class: 'el' }, 'Caption'), caption, apply('caption', caption))];
    })(),
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Add tag'), tagPicker((t) => save({ add_tags: [t.id] }))),
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Remove tag'), tagPicker((t) => save({ remove_tags: [t.id] }), 'Remove a tag…')),
    h('h4', null, 'Date'), dateEditor(ids, null),
    h('h4', null, 'Location and template'),
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, h('button', { class: 'pillbtn', onclick: () => locationDialog(ids, null) }, icon('place'), 'Set location on map'),
      h('button', { class: 'pillbtn', onclick: () => templatePicker(ids) }, icon('copyright'), 'Apply template'),
      h('button', { class: 'pillbtn', onclick: () => gpxDialog(ids) }, icon('map'), 'Correlate with GPS track')),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn on', onclick: () => back.remove() }, 'Done')));
}

export async function dbDialog() {
  let st;
  try { st = await api('/api/edits/db'); } catch { toast('Could not check the digiKam database status.'); return; }
  const msg = h('p', { class: st.digikamRunning ? 'warn' : '' }, st.digikamRunning
    ? 'digiKam is running. Quit digiKam first; its database must not be changed while it is open.'
    : st.ask ? 'The viewer cannot see from here whether digiKam is open, so it does not check.' : 'digiKam is closed, so its database can be updated now.');
  const applyBtn = h('button', { class: 'pillbtn on', disabled: !!st.digikamRunning || !st.pending, onclick: async () => {
    applyBtn.textContent = 'Updating…';
    try {
      const r = await post('/api/edits/db', {});
      back.remove();
      toast(`Updated ${plural(r.applied, 'item')} in digiKam’s database.` + (r.skipped.length ? ` ${r.skipped.length} skipped: ${r.skipped[0].reason}.` : ''), null, null, 9000);
      S.keepView = true; pollSoon();
    } catch (e) { applyBtn.textContent = 'Apply to digiKam database'; msg.className = 'warn'; msg.textContent = e.message; }
  } }, 'Apply to digiKam database');
  const back = modal('Apply edits to digiKam’s database',
    h('p', null, `${plural(st.pending, 'photo has', 'photos have')} edits that are saved in XMP sidecars but not yet in digiKam’s database.`),
    h('p', null, 'This writes the same ratings, labels, titles, captions and tags into digikam4.db. A copy of the database is saved in the viewer’s data/db-backups folder first.'),
    msg,
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'),
      h('a', { class: 'pillbtn', href: hashFor('photos', photosWith({ edited: 1 })), onclick: () => back.remove() }, 'Show them'), applyBtn));
}

export const TEMPLATE_LABEL = { creator: 'Creator', copyrightNotice: 'Copyright notice', rightsUsageTerms: 'Usage terms', creatorJobTitle: 'Job title', provider: 'Credit', source: 'Source', instructions: 'Instructions',
  'creatorContactInfo.address': 'Address', 'creatorContactInfo.city': 'City', 'creatorContactInfo.provinceState': 'State / province', 'creatorContactInfo.postalCode': 'Postal code', 'creatorContactInfo.country': 'Country', 'creatorContactInfo.phone': 'Phone', 'creatorContactInfo.email': 'Email', 'creatorContactInfo.webUrl': 'Web' };

export async function loadTemplates() { if (!S.TEMPLATES) try { S.TEMPLATES = (await api('/api/maintenance')).templates || []; } catch { S.TEMPLATES = []; } return S.TEMPLATES; }
export function dateEditor(ids, current, after) {
  // current: "YYYY-MM-DDTHH:MM:SS" for one photo, or null for a batch
  const inp = h('input', { class: 'einput', type: 'datetime-local', step: 1, value: (current || '').slice(0, 16), style: 'margin:0;width:auto' });
  const setBtn = h('button', { class: 'pillbtn', onclick: async () => { if (!inp.value) return; const t = inp.value.length === 16 ? inp.value + ':00' : inp.value; if (await editPhotos(ids, { taken: t })) after?.(); } }, 'Set date');
  const amount = h('input', { class: 'einput', type: 'number', value: 1, style: 'margin:0;width:70px' });
  const unit = h('select', null, [['3600', 'hours'], ['60', 'minutes'], ['86400', 'days'], ['1', 'seconds']].map(([v, l]) => h('option', { value: v }, l)));
  const shift = (sign) => async () => { const s = sign * Math.round(+amount.value * +unit.value); if (!s) return; if (await editPhotos(ids, { shift: s })) after?.(); };
  return h('div', { class: 'dateedit' },
    h('div', { class: 'erow' }, h('span', { class: 'el' }, ids.length > 1 ? 'Set all to' : 'Taken'), inp, setBtn),
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Shift by'), amount, unit, h('button', { class: 'pillbtn', title: 'Move the date later', onclick: shift(1) }, '+ later'), h('button', { class: 'pillbtn', title: 'Move the date earlier', onclick: shift(-1) }, '− earlier')),
    h('div', { class: 'muted', style: 'font-size:12px' }, 'Shifting keeps each photo’s own time and moves it by the same amount, for a camera clock that was wrong.'));
}
/** Face detection on request: a photo (answered at once) or a folder (in the background). Faces already
 *  marked, by digiKam or anything else, are kept; only faces nothing marked become new unnamed faces. */
export async function detectFaces(ids, folder) {
  if (folder && !confirm(`Look for faces in the ${nf.format(folder.nAll)} photos of “${folder.path.replace(/^\//, '')}” (with its subfolders)? New faces are added to digiKam’s database as unnamed faces; faces already marked stay as they are.`)) return;
  if (!folder) toast('Looking for faces…', null, null, 30000);
  let r;
  try { r = await post('/api/faces/detect', folder ? { album: folder.id } : { ids }); } catch (e) { toast(e.message, null, null, 9000); return; }
  if (!r.done) { toast(`Looking for faces in ${plural(r.queued, 'photo')} in the background; progress in Maintenance › AI analysis and models.`, null, null, 9000); return; }
  toast(r.added ? `Found ${plural(r.added, 'new face')}; ${r.found - r.added ? `${plural(r.found - r.added, 'face was', 'faces were')} already marked.` : ''} Name ${r.added === 1 ? 'it' : 'them'} under People or here.` : r.found ? `No new faces: the ${plural(r.found, 'face')} found ${r.found === 1 ? 'is' : 'are'} already marked.` : 'No faces found.', null, null, 9000);
  if (r.added && LB.open) { (ids || []).forEach((i) => details.delete(i)); S.keepView = true; setTimeout(() => { pollStatus().then(() => { if (LB.open) showItem(); }); }, 800); }
}
export async function locationDialog(ids, current, after) {
  // current: {lat, lon, alt} or null. Drag the marker or click the map; coordinates can also be typed.
  const lat = h('input', { class: 'einput', type: 'number', step: 'any', placeholder: 'latitude', value: current?.lat ?? '', style: 'margin:0;width:140px' });
  const lon = h('input', { class: 'einput', type: 'number', step: 'any', placeholder: 'longitude', value: current?.lon ?? '', style: 'margin:0;width:140px' });
  const alt = h('input', { class: 'einput', type: 'number', step: 'any', placeholder: 'altitude m', value: current?.alt ?? '', style: 'margin:0;width:110px', oninput: () => { altAuto = false; fill(altNote); } });
  // the altitude follows the position (ground height from Google or Open-Meteo) until it is typed by hand
  let altAuto = current?.alt == null, altTimer = 0;
  const altNote = h('span', { class: 'muted', style: 'font-size:12px' });
  const fetchAlt = () => {
    clearTimeout(altTimer);
    if (!altAuto || !lat.value || !lon.value) return;
    altTimer = setTimeout(async () => {
      const la = lat.value, lo = lon.value;
      try {
        const r = await api(`/api/elevation?lat=${encodeURIComponent(la)}&lon=${encodeURIComponent(lo)}`);
        if (!altAuto || la !== lat.value || lo !== lon.value) return;
        alt.value = r.alt; fill(altNote, `ground height from ${r.source}`);
      } catch { fill(altNote, 'no altitude service reachable'); }
    }, 400);
  };
  const mapEl = h('div', { class: 'imap', style: 'height:360px;margin:10px 0' });
  // address or landmark search (OpenStreetMap Nominatim through the server; GeoNames towns offline)
  let searchTimer = 0;
  const results = h('div', { class: 'alist', style: 'max-height:160px;margin-top:4px', hidden: true });
  const search = h('input', { class: 'einput', placeholder: 'Search an address or place, e.g. “Mt Lebanon High School”', style: 'margin:0;flex:1;min-width:240px',
    onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); clearTimeout(searchTimer); run(); } if (e.key === 'Escape') { results.hidden = true; } },
    oninput: () => { clearTimeout(searchTimer); searchTimer = setTimeout(run, 500); } });
  const run = async () => {
    const q = search.value.trim();
    if (q.length < 2) { results.hidden = true; return; }
    let r;
    try { r = await api('/api/geocode?q=' + encodeURIComponent(q)); } catch (e) { toast(e.message); return; }
    if (search.value.trim() !== q) return;
    results.hidden = false;
    const note = r.note ? h('div', { class: 'muted', style: 'padding:4px 8px;font-size:12px' }, r.note) : null;
    fill(results, note, r.results.length ? r.results.map((x) => h('div', { class: 'frow', style: 'height:auto;padding:6px 8px', title: `${x.lat}, ${x.lon}`, onclick: () => {
      lat.value = x.lat; lon.value = x.lon; results.hidden = true; search.value = x.name; place();
      if (map) map.setView([x.lat, x.lon], x.zoom || 14);
    } }, icon('place'), h('span', { class: 'nm', style: 'white-space:normal' }, x.name, x.detail ? h('span', { class: 'muted' }, ' · ' + x.detail) : null)))
      : h('div', { class: 'muted', style: 'padding:6px 8px' }, r.source === 'offline' ? 'No town by that name in the offline place list (no internet connection for the full search).' : 'Nothing found.'));
  };
  // "Suggest location": the places of the photos taken nearest in time, and on request a landmark Google recognises
  const sugBox = h('div', { class: 'alist', style: 'max-height:200px;margin-top:4px', hidden: true });
  const useSpot = (x) => { lat.value = x.lat; lon.value = x.lon; if (x.alt != null) { alt.value = x.alt; altAuto = false; fill(altNote, 'altitude from that photo'); } sugBox.hidden = true; place(); if (map) map.setView([x.lat, x.lon], 16); };
  const ago = (m) => { const a = Math.abs(m); const t = a < 60 ? `${a} min` : a < 1440 ? `${Math.round(a / 6) / 10} h` : `${Math.round(a / 144) / 10} days`; return m === 0 ? 'same time' : m < 0 ? `${t} before` : `${t} after`; };
  const suggest = async () => {
    sugBox.hidden = false; fill(sugBox, h('div', { class: 'muted', style: 'padding:6px 8px' }, 'Looking at the photos taken around the same time…'));
    let r; try { r = await api(`/api/location/suggest?id=${ids[0]}`); } catch (e) { fill(sugBox, h('div', { class: 'muted', style: 'padding:6px 8px' }, e.message)); return; }
    const landmark = r.landmarks ? h('div', { class: 'frow', style: 'height:auto;padding:6px 8px', onclick: async (e) => {
      const row = e.currentTarget; fill(row, icon('spark'), h('span', { class: 'nm muted' }, 'Asking Google Cloud Vision…'));
      let L2; try { L2 = await post('/api/location/landmark', { id: ids[0] }); } catch (err) { fill(row, icon('spark'), h('span', { class: 'nm muted', style: 'white-space:normal' }, err.message)); return; }
      row.replaceWith(...(L2.landmarks.length ? L2.landmarks.map((x) => h('div', { class: 'frow', style: 'height:auto;padding:6px 8px', onclick: () => useSpot(x) }, icon('spark'),
        h('span', { class: 'nm', style: 'white-space:normal' }, x.name, h('span', { class: 'muted' }, ` · landmark, ${Math.round(x.score * 100)}% sure`))))
        : [h('div', { class: 'muted', style: 'padding:6px 8px' }, 'Google recognised no landmark in this photo.')]));
    } }, icon('spark'), h('span', { class: 'nm' }, 'Recognise a landmark in the photo', h('span', { class: 'muted' }, ' · Google Cloud Vision, about $0.0015 a photo after 1,000 free a month'))) : null;
    fill(sugBox, r.suggestions.length ? r.suggestions.map((x) => h('div', { class: 'frow', style: 'height:auto;padding:6px 8px', title: `${x.lat}, ${x.lon}`, onclick: () => useSpot(x) },
      h('img', { src: `/thumb/${x.id}`, alt: '', style: 'width:34px;height:34px;object-fit:cover;border-radius:5px;flex:none' }),
      h('span', { class: 'nm', style: 'white-space:normal' }, x.place || `${x.lat.toFixed(4)}, ${x.lon.toFixed(4)}`, h('span', { class: 'muted' }, ` · ${x.name}, taken ${ago(x.minutes)}${x.same_camera ? '' : ' (another camera)'}`))))
      : h('div', { class: 'muted', style: 'padding:6px 8px' }, 'No photo with a location was taken within 6 hours of this one.'), landmark);
  };
  const back = modal(ids.length > 1 ? `Set location for ${nf.format(ids.length)} photos` : 'Set location',
    h('p', { class: 'muted' }, ids.length > 1 ? `The same position goes to all ${nf.format(ids.length)} selected photos, replacing any they already have. ` : '', 'Click the map or drag the pin, or type coordinates. Saved to the sidecar (and file, if on) and shown in digiKam after its database is updated.'),
    h('div', { style: 'display:flex;gap:8px;align-items:center;margin-top:6px' }, search,
      h('button', { class: 'pillbtn', title: 'Places of the photos taken around the same time, or a landmark Google recognises', onclick: suggest }, icon('spark'), 'Suggest location'),
      h('button', { class: 'pillbtn', title: 'Look at the spot in Street View before saving', onclick: () => { if (!lat.value || !lon.value) { toast('Pick a spot first.'); return; } streetView(+lat.value, +lon.value, 'Street View'); } }, 'Street View')), results, sugBox,
    mapEl,
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center' }, lat, lon, alt, altNote,
      h('button', { class: 'pillbtn', title: 'Use the location of the previous photo in the grid', onclick: () => { const i = G.data?.id.indexOf(ids[0]); for (let k = (i ?? 0) - 1; k >= 0; k--) { if (G.geo.has(k)) { getDetail(G.data.id[k]).then((d) => { lat.value = d.lat; lon.value = d.lon; alt.value = d.alt ?? ''; altAuto = d.alt == null; fill(altNote); place(); }); return; } } toast('No earlier photo in this view has a location.'); } }, 'Copy from previous photo')),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'),
      current ? h('button', { class: 'pillbtn', onclick: async () => { if (confirm('Remove the location from ' + (ids.length > 1 ? 'these photos' : 'this photo') + '?')) { back.remove(); if (await editPhotos(ids, { gps: null })) after?.(); } } }, 'Remove location') : null,
      h('button', { class: 'pillbtn on', onclick: async () => { const la = +lat.value, lo = +lon.value; if (!lat.value || !lon.value || Math.abs(la) > 90 || Math.abs(lo) > 180) { toast('Enter a valid latitude and longitude.'); return; } back.remove(); if (await editPhotos(ids, { gps: { lat: la, lon: lo, alt: alt.value === '' ? null : +alt.value } })) after?.(); } }, icon('place'), 'Save location')));
  let map, marker;
  const place = () => { fetchAlt(); if (!map || !lat.value || !lon.value) return; const ll = [+lat.value, +lon.value]; marker ? marker.setLatLng(ll) : (marker = L.marker(ll, { draggable: true }).addTo(map).on('dragend', () => { const p = marker.getLatLng(); lat.value = p.lat.toFixed(6); lon.value = p.lng.toFixed(6); fetchAlt(); })); map.setView(ll, Math.max(map.getZoom(), 12)); };
  // a pasted "40.4441, -79.9623" (comma, semicolon or space separated; N/S/E/W suffixes allowed) fills both fields
  const pastePair = (e) => {
    const t = (e.clipboardData || window.clipboardData)?.getData('text') || '';
    const m = t.trim().match(/^(-?\d+(?:\.\d+)?)\s*°?\s*([NS])?\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*°?\s*([EW])?$/i);
    if (!m) return;
    e.preventDefault();
    const la = +m[1] * (m[2] && m[2].toUpperCase() === 'S' ? -1 : 1), lo = +m[3] * (m[4] && m[4].toUpperCase() === 'W' ? -1 : 1);
    if (Math.abs(la) > 90 || Math.abs(lo) > 180) { toast('That does not look like a latitude, longitude pair.'); return; }
    lat.value = la; lon.value = lo; place();
    if (map) map.setView([la, lo], Math.max(map.getZoom(), 12));
  };
  lat.onpaste = lon.onpaste = pastePair;
  try { await loadLeaflet(false); } catch { mapEl.replaceChildren(h('div', { class: 'empty' }, 'The map needs an internet connection; coordinates can still be typed.')); return; }
  const L = window.L;
  await loadMaps();
  map = L.map(mapEl).setView(current ? [current.lat, current.lon] : [40.4, -80], current ? 13 : 4);
  baseLayers(map);
  map.on('click', (e) => { lat.value = e.latlng.lat.toFixed(6); lon.value = e.latlng.lng.toFixed(6); place(); });
  lat.onchange = lon.onchange = place;
  place();
}
export async function gpxDialog(ids) {
  const file = h('input', { type: 'file', accept: '.gpx,application/gpx+xml' });
  const tzNow = -new Date().getTimezoneOffset() * 60;  // this browser's offset, as a sensible default
  const offset = h('select', null, [...Array(27)].map((_, i) => i - 12).map((hh) => h('option', { value: hh * 3600, selected: hh * 3600 === tzNow }, `UTC${hh >= 0 ? '+' : ''}${hh}`)));
  const shiftAmt = h('input', { class: 'einput', type: 'number', value: 0, style: 'margin:0;width:70px' });
  const shiftUnit = h('select', null, [['60', 'minutes'], ['1', 'seconds'], ['3600', 'hours']].map(([v, l]) => h('option', { value: v }, l)));
  const gap = h('select', null, [[120, '2 minutes'], [600, '10 minutes'], [1800, '30 minutes'], [3600, '1 hour'], [21600, '6 hours']].map(([v, l]) => h('option', { value: v, selected: v === 600 }, l)));
  const onlyMissing = h('input', { type: 'checkbox', checked: true });
  const result = h('div');
  const mapEl = h('div', { class: 'imap', style: 'height:260px;margin-top:10px', hidden: true });
  let token = null, preview = null;
  const params = () => ({ utc_offset: +offset.value, clock_shift: Math.round(+shiftAmt.value * +shiftUnit.value), max_gap: +gap.value, only_missing: onlyMissing.checked });
  const run = async () => {
    if (!file.files[0]) { toast('Choose a .gpx file first.'); return; }
    const fd = new FormData();
    fd.append('file', file.files[0]); fd.append('ids', ids.join(','));
    Object.entries(params()).forEach(([k, v]) => fd.append(k, v === true ? '1' : v === false ? '0' : v));
    fill(result, h('p', { class: 'muted' }, 'Matching…'));
    let r;
    try { const res = await fetch('/api/gpx/preview', { method: 'POST', body: fd }); r = await res.json(); if (!res.ok) throw new Error(r.detail || res.status); } catch (e) { fill(result, h('p', { class: 'warn' }, e.message)); return; }
    token = r.token; preview = r;
    const t = r.track;
    fill(result,
      h('p', null, `Track: ${nf.format(t.points)} points, ${t.km} km, ${fmtWhen(t.start)} to ${dfTimeLocal.format(date(t.end))} (in this browser’s time zone).`),
      h('p', null, h('b', null, `${nf.format(r.matched)} of ${nf.format(r.considered)} photos match`), r.unmatched ? ` (${nf.format(r.unmatched)} were taken too far from any track point)` : '', r.skipped_with_position ? `; ${nf.format(r.skipped_with_position)} already had a position and were left alone.` : '.'),
      r.matched ? h('div', { class: 'muted', style: 'font-size:12.5px' }, 'Typical gap between photo time and nearest track point: ', `${Math.round(r.matches.reduce((a, m) => a + m.gap, 0) / r.matches.length)} s`, '. If photos land in the wrong place, the camera clock or the UTC offset is off; adjust and match again.') : null);
    mapEl.hidden = !r.matched;
    if (r.matched) try {
      await loadLeaflet(false);
      const L = window.L;
      mapEl.replaceChildren();
      await loadMaps();
      const map = L.map(mapEl).fitBounds([[t.bbox[0], t.bbox[1]], [t.bbox[2], t.bbox[3]]]);
      tiles().addTo(map);
      r.matches.slice(0, 500).forEach((m) => L.circleMarker([m.lat, m.lon], { radius: 4, color: '#1a73e8', fillOpacity: .8 }).bindTooltip(m.name).addTo(map));
    } catch { mapEl.hidden = true; }
  };
  const back = modal(`Correlate ${nf.format(ids.length)} photos with a GPS track`,
    h('p', { class: 'muted' }, 'Like digiKam’s Geolocation Editor: each photo gets the position the track recorded at the moment it was taken. Track times are UTC; tell it what time zone the camera clock was set to.'),
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'GPX file'), file),
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Camera zone'), offset, h('span', { class: 'muted', style: 'font-size:12px' }, 'what the camera’s clock was set to')),
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Clock error'), shiftAmt, shiftUnit, h('span', { class: 'muted', style: 'font-size:12px' }, 'negative if the camera ran fast')),
    h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Max gap'), gap, h('label', { style: 'display:flex;gap:6px;align-items:center;margin-left:12px' }, onlyMissing, 'only photos without a position')),
    h('div', { style: 'margin:8px 0' }, h('button', { class: 'pillbtn', onclick: run }, icon('spark'), 'Match')),
    result, mapEl,
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'),
      h('button', { class: 'pillbtn on', onclick: async () => {
        if (!token || !preview?.matched) { toast('Match first.'); return; }
        if (!confirm(`Write positions to ${nf.format(preview.matched)} photos? Sidecars are backed up first; digiKam gets them with “Apply edits to digiKam database”.`)) return;
        try { const r = await post('/api/gpx/apply', { token, ids, ...params() }); back.remove(); toast(`Positions written to ${nf.format(r.edited)} photos.`); S.keepView = true; pollSoon(); }
        catch (e) { toast(e.message, null, null, 9000); }
      } }, icon('place'), 'Apply positions')));
  file.onchange = run;
}
export async function templatePicker(ids, after) {
  const tpls = await loadTemplates();
  const back = modal(`Apply a metadata template to ${plural(ids.length, 'photo')}`,
    tpls.length ? h('div', { class: 'alist' }, tpls.map((t) => h('div', { class: 'frow', style: 'height:auto;padding:6px 8px', onclick: async () => { back.remove(); if (await editPhotos(ids, { template: Object.fromEntries(Object.keys(TEMPLATE_LABEL).map((k) => [k, t.fields[k] || ''])) })) after?.(); } },
      icon('copyright'), h('div', null, h('b', null, t.name), h('div', { class: 'muted', style: 'font-size:12px' }, Object.entries(t.fields).filter(([, v]) => v).map(([k, v]) => `${TEMPLATE_LABEL[k]}: ${v}`).join(' · ')))))) 
      : h('p', { class: 'muted' }, 'No templates yet. Define them in Settings > Metadata templates.'),
    h('p', { class: 'muted', style: 'font-size:12px' }, 'Applying a template sets every template field; fields left empty in the template are cleared on the photo, as in digiKam.'),
    h('div', { class: 'modal-btns' }, h('a', { class: 'pillbtn', href: '#/settings', onclick: () => back.remove() }, 'Manage templates'), h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel')));
}
export function templatesSection(m) {
  const list = h('div');
  let tpls = (m.templates || []).map((t) => ({ ...t, fields: { ...t.fields } }));
  const save = async () => { try { await post('/api/settings', { templates: tpls }); S.TEMPLATES = null; toast('Templates saved.'); } catch (e) { toast(e.message); } draw(); };
  const edit = (t, isNew) => {
    const name = h('input', { class: 'einput', style: 'margin:0', placeholder: 'Template name', value: t.name || '' });
    const inputs = Object.keys(TEMPLATE_LABEL).map((k) => [k, h('input', { class: 'einput', style: 'margin:0', placeholder: TEMPLATE_LABEL[k], value: t.fields[k] || '' })]);
    const back = modal(isNew ? 'New metadata template' : `Edit “${t.name}”`,
      h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Name'), name),
      h('div', { class: 'alist', style: 'max-height:50vh' }, inputs.map(([k, inp]) => h('div', { class: 'erow' }, h('span', { class: 'el', style: 'width:120px' }, TEMPLATE_LABEL[k]), inp))),
      h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'),
        h('button', { class: 'pillbtn on', onclick: () => { if (!name.value.trim()) { name.focus(); return; } t.name = name.value.trim(); inputs.forEach(([k, inp]) => { t.fields[k] = inp.value.trim(); }); if (isNew) tpls.push(t); back.remove(); save(); } }, 'Save')));
  };
  const draw = () => fill(list, tpls.map((t) => h('div', { class: 'srow' }, h('div', null, h('b', null, t.name), h('div', { class: 'muted', style: 'font-size:13px' }, Object.entries(t.fields).filter(([, v]) => v).map(([k, v]) => `${TEMPLATE_LABEL[k]}: ${v}`).join(' · ') || 'empty')),
      h('span', { style: 'display:flex;gap:6px' }, h('button', { class: 'pillbtn', onclick: () => edit(t, false) }, 'Edit'), h('button', { class: 'pillbtn', onclick: () => { if (confirm(`Delete the template “${t.name}”? Photos it was applied to are not changed.`)) { tpls = tpls.filter((x) => x !== t); save(); } } }, 'Delete')))),
    h('div', { style: 'margin-top:10px' }, h('button', { class: 'pillbtn', onclick: () => edit({ name: '', fields: {} }, true) }, icon('add'), 'New template')));
  draw();
  return h('div', null, h('h2', null, 'Metadata templates'), h('div', { class: 'muted', style: 'max-width:820px' }, 'Like digiKam’s templates: creator, copyright, credit, source, instructions and contact details, applied to any selection in one go from the Edit dialog or the info panel.'), list);
}
