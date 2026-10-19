/** Selection bar actions and app-side state: favorites, albums, trash, move, rename, the change log entries. */
import { $, api, date, dfDay, fill, fmtBytes, fmtWhen, h, icon, main, nf, norm, plural, post, store } from './core.js';
import { S, ST, filterParams, go, loadState, onRoute, photosWith, pollSoon, toast, ualbumById } from './state.js';
import { crumbs } from './search.js';
import { G, renderGrid } from './grid.js';
import { copyMetadata, dateEditor, editDialog, editPhotos, locationDialog, tagDialog } from './editing.js';
import { downloadDialog } from './download.js';
import { pageTitle, people } from './pages.js';
import { LB, details, editMenu, openLightbox, showItem, toggleFaces, updateFavBtn } from './lightbox.js';
import { pollStatus } from './main.js';

export function redrawTiles() {
  for (const el of G.rendered.values()) el.remove();
  G.rendered.clear();
  renderGrid(true);
}
export function toggleSelect(i) {
  const id = G.data.id[i];
  G.sel.has(id) ? G.sel.delete(id) : G.sel.add(id);
  G.lastSel = i;
  $(`.tile[data-i="${i}"]`, $('#grid'))?.classList.toggle('sel', G.sel.has(id));
  updateSelBar();
}
export function selectRange(a, b, on) {
  const ids = G.data.id;
  if (on === 'toggle') { on = false; for (let i = a; i <= b; i++) if (!G.sel.has(ids[i])) { on = true; break; } }
  for (let i = a; i <= b; i++) on ? G.sel.add(ids[i]) : G.sel.delete(ids[i]);
  G.lastSel = b;
  redrawTiles();
  updateSelBar();
}
export function clearSelection(silent) {
  if (!G.sel.size && silent) { updateSelBar(); return; }
  G.sel.clear(); G.lastSel = -1;
  if (!silent) redrawTiles();
  updateSelBar();
}
export function updateSelBar() {
  let bar = $('#selbar');
  document.body.classList.toggle('selecting', G.sel.size > 0);
  const sa = $('#grid .selall'), sn = $('#grid .selnone');
  if (sa && G.data) { sa.hidden = G.sel.size >= G.data.count; sn.hidden = !G.sel.size; }
  if (!G.sel.size) { bar?.remove(); return; }
  if (!bar) { bar = h('div', { id: 'selbar' }); document.body.append(bar); }
  const ids = () => [...G.sel];
  const inAlbum = S.route.view === 'photos' && ualbumById(filterParams().get('ualbum'))?.canEdit ? filterParams().get('ualbum') : null;
  const allFav = ids().every((id) => ST.fav.has(id));
  // the actions as icons, like the viewer's toolbar; the name shows on hover (and is what screen readers say)
  const btn = (ic, label, fn) => h('button', { class: 'pillbtn selic', title: label, 'aria-label': label, onclick: fn }, icon(ic));
  fill(bar,
    h('button', { class: 'iconbtn', title: 'Clear selection (Esc)', onclick: () => clearSelection() }, icon('close')),
    h('span', { class: 'n' }, `${nf.format(G.sel.size)} selected`),
    G.data && G.sel.size < G.data.count ? h('a', { class: 'lnk', style: 'margin-left:6px', title: 'Select everything in this view', onclick: () => selectRange(0, G.data.count - 1, true) }, `Select all ${nf.format(G.data.count)}`) : null,
    h('a', { class: 'lnk', style: 'margin-left:6px', title: 'Deselect everything', onclick: () => clearSelection() }, 'Deselect all'),
    h('span', { class: 'sp' }),
    btn(allFav ? 'heart' : 'heartO', allFav ? 'Unfavorite' : 'Favorite', async () => { await setFavorite(ids(), !allFav); }),
    btn('albumAdd', 'Add to album', () => albumDialog(ids())),
    S.EDITING ? btn('tag', 'Tag', () => tagDialog(ids())) : null,
    S.EDITING ? btn('edit', 'Edit', () => editDialog(ids())) : null,
    S.EDITING ? btn('place', 'Set location', () => locationDialog(ids(), null)) : null,  // geotag the whole selection at once
    inAlbum ? btn('remove', 'Remove from album', async () => {
      try { await post(`/api/ualbums/${inAlbum}`, { ids: ids(), on: false }); } catch { toast('Could not update the album.'); return; }
      await loadState(); toast(`Removed ${nf.format(G.sel.size)} from the album.`); G.sel.clear(); S.viewKey = null; onRoute();
    }) : null,
    btn('download', 'Export', () => downloadDialog(photosWith({ ids: ids().join(',') }), G.sel.size)),
    S.CAN_MANAGE ? btn('text', 'Rename by date', () => renameByDate(ids())) : null,
    S.EDITING && G.sel.size > 1 ? btn('dup', 'Copy metadata', () => copyMetadata(ids())) : null,
    S.CAN_MANAGE ? btn('folder', 'Move to a folder', () => movePhotos(ids())) : null,
    S.CAN_MANAGE && S.LOSSLESS ? btn('rotate', 'Rotate or flip', (e) => editMenu(ids(), e.currentTarget, false)) : null,
    S.CAN_MANAGE ? btn('trash', 'Move to trash (Delete key)', () => trashPhotos(ids())) : null);
}
// ---- the change log: entries for one photo (info panel) or the whole library (#/history)
export const HIST_ACTION = { edit: 'Edit', face: 'Face', tag: 'Tag', deleted: 'Deleted', trashed: 'Trashed', moved: 'Moved', restore: 'Restore', sync: 'Sync', database: 'digiKam', revert: 'Undo', added: 'Added', guard: 'Protected', lossless: 'File' };
export const HIST_RESTORE = { undo: ['Undo', 'Put back the values from before this change'], trash: ['Restore from trash', 'Put the photo back where it was'], sidecar: ['Restore sidecar', 'Put the sidecar back as it was just before this change'],
  original: ['Restore original file', 'Put the image file back as it was before the viewer first wrote into it'], deleted: ['Restore photo', 'Put the files back and add the photo to the library again'] };
export function histEntry(e, onDone, withPhoto) {
  const btns = S.CAN_MANAGE ? e.restore.map((k) => h('a', { class: 'lnk', title: HIST_RESTORE[k][1], onclick: async () => {
    if (!confirm(`${HIST_RESTORE[k][1]}?`)) return;
    try { const r = await post(`/api/history/${e.id}/restore`, { kind: k }); toast(k === 'deleted' ? 'Photo restored.' : 'Restored.'); details.delete(e.photo); if (r && r.id) details.delete(r.id); } catch (err) { toast(err.message, null, null, 9000); return; }
    S.keepView = true; pollSoon(600); onDone?.();
  } }, HIST_RESTORE[k][0])) : [];
  return h('div', { class: 'hrow' },
    withPhoto ? (e.action === 'deleted' ? h('span', { class: 'ph' }, icon('trash')) : h('img', { src: `/thumb/${e.photo}`, alt: '', loading: 'lazy', onclick: () => openLightbox({ id: [e.photo] }, 0, true) })) : null,
    h('div', { class: 'hmain' }, h('div', null, h('span', { class: 'hact a-' + e.action }, HIST_ACTION[e.action] || e.action), ' ', e.summary),
      h('div', { class: 'muted' }, `${fmtWhen(e.ts)} · ${e.user || ''}`, withPhoto && e.details?.name ? ` · ${e.details.name}` : withPhoto && e.path ? ` · ${e.path.split('/').pop()}` : '', btns.length ? ' · ' : '', ...btns.flatMap((b, i) => (i ? [' · ', b] : [b]))),
      withPhoto && (e.sidecar_copy || e.file_copy) ? h('div', { class: 'muted', style: 'font-size:11.5px;word-break:break-all' }, 'Backups: ',
        e.sidecar_copy ? h('span', { title: e.sidecar_copy }, 'sidecar before this change') : null, e.sidecar_copy && e.file_copy ? ' · ' : null,
        e.file_copy ? h('span', { title: e.file_copy }, `original image file (${e.file_copy.split('/').slice(-3).join('/')})`) : null) : null));
}
export async function showHistory(params) {
  const photo = params.get('photo'), more = h('button', { class: 'pillbtn', style: 'margin:14px 0', hidden: true }, 'Show more');
  let size = store.get('hsize', 'list');
  const list = h('div', { class: 'hlist qitems ' + size });
  let action = params.get('action') || '', offset = 0, timer = 0;
  const sel = h('select', { onchange: (e) => { action = e.target.value; reload(); } }, h('option', { value: '' }, 'Every change'), ...Object.entries(HIST_ACTION).map(([k, l]) => h('option', { value: k, selected: k === action }, l)));
  const q = h('input', { class: 'einput', placeholder: 'Search: file name, words in the change…', style: 'margin:0;min-width:220px;flex:1', oninput: () => { clearTimeout(timer); timer = setTimeout(reload, 350); } });
  const people = (S.META?.tags || []).filter((t) => t.person && t.nAll).sort((a, b) => a.name.localeCompare(b.name));
  const plist = h('datalist', { id: 'hpeople' }, people.map((t) => h('option', { value: t.name })));
  const person = h('input', { class: 'einput', list: 'hpeople', placeholder: 'Person', style: 'margin:0;width:170px', onchange: () => reload(), oninput: () => { if (!person.value) reload(); } });
  const who = h('select', { onchange: () => reload() }, h('option', { value: '' }, 'Anyone'));
  const from = h('input', { type: 'date', class: 'einput', style: 'margin:0;width:150px', title: 'From', onchange: () => reload() });
  const to = h('input', { type: 'date', class: 'einput', style: 'margin:0;width:150px', title: 'Until', onchange: () => reload() });
  const sizes = h('div', { class: 'eseg', title: 'Picture size' }, [['list', 'List'], ['large', 'Large'], ['huge', 'Larger']].map(([k, l]) => h('button', { class: k === size ? 'on' : '', onclick: (e) => {
    size = k; store.set('hsize', k); list.className = 'hlist qitems ' + k; sizes.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.currentTarget)); } }, l)));
  const count = h('span', { class: 'muted' });
  const query = () => {
    const u = new URLSearchParams({ limit: 100, offset });
    if (photo) u.set('photo', photo);
    if (action) u.set('action', action);
    if (q.value.trim()) u.set('q', q.value.trim());
    const t = people.find((x) => x.name.toLowerCase() === person.value.trim().toLowerCase());
    if (t) u.set('person', t.id);
    if (who.value) u.set('user', who.value);
    if (from.value) u.set('since', Math.floor(new Date(from.value + 'T00:00:00').getTime() / 1000));
    if (to.value) u.set('until', Math.floor(new Date(to.value + 'T00:00:00').getTime() / 1000) + 86400);
    return u;
  };
  const load = async (append) => {
    let r; try { r = await api('/api/history?' + query().toString()); } catch (e) { fill(list, h('div', { class: 'empty' }, e.message)); return; }
    if (!append) list.replaceChildren();
    if (r.users && who.options.length === 1) who.append(...r.users.map((u) => h('option', { value: u }, u)));
    if (!r.entries.length && !offset) list.append(h('div', { class: 'empty' }, 'No change matches.'));
    list.append(...r.entries.map((e) => histEntry(e, () => reload(), true)));
    offset += r.entries.length; more.hidden = offset >= r.total;
    fill(count, plural(r.total, 'change'));
  };
  const reload = () => { offset = 0; load(); };
  more.onclick = () => load(true);
  main.replaceChildren(h('div', { class: 'page spage' }, crumbs([['Maintenance', '#/maintenance'], ['Edit log', photo ? '#/history' : null], ...(photo ? [['One photo', null]] : [])]),
    pageTitle('calendar', 'indigo', photo ? 'Edit log of one photo' : 'Edit log'),
    h('p', { class: 'muted', style: 'margin:0 0 10px' }, 'Every change made in the viewer, kept for good: who, when, and the values before and after. Undo puts the earlier values back; the restore links use the backups (sidecar copies, original files, the trash).'),
    h('div', { class: 'ctools hfilters' }, q, photo ? null : [person, plist], sel, who, h('span', { class: 'muted' }, 'from'), from, h('span', { class: 'muted' }, 'to'), to, sizes, count), list, more));
  load();
}
/** Delete photos for good, after a confirmation: files, sidecars, the database rows, everything the viewer knows.
 *  The server copies the files and sidecars to data/deleted-photos and backs digiKam's database up first. */
/** Move photos to the trash (digiKam's .dtrash, so digiKam's trash shows them too): no question, Undo in the message. */
export async function trashPhotos(ids, after) {
  let r;
  try { r = await post('/api/photos/trash', { ids }); } catch (e) { toast(e.message, null, null, 10000); return; }
  ids.forEach((i) => { details.delete(i); G.sel.delete(i); });
  updateSelBar();
  const failed = r.failed.length ? ` ${nf.format(r.failed.length)} could not be: ${r.failed[0].error}.` : '';
  if (r.trashed.length) {
    const infos = r.infos || [];  // the trash records, for Undo
    toast(`${plural(r.trashed.length, 'photo')} moved to the trash.${failed}`, infos.length ? 'Undo' : null, async () => {
      try { await post('/api/trash/restore', { infos }); toast('Restored.'); } catch (e) { toast(e.message, null, null, 9000); return; }
      S.keepView = true; S.viewKey = null; pollSoon(600);
    }, 9000);
  } else toast(`Nothing was moved.${failed}`, null, null, 9000);
  after?.(r);
  S.keepView = true; S.viewKey = null; await pollStatus(); onRoute();
}
/** Pick a folder of the library; onPick(folder) with {id, path}. */
export async function folderPicker(title, onPick, exclude) {
  let d; try { d = await api('/api/folders'); } catch (e) { toast(e.message); return; }
  const all = d.folders.map((f) => ({ ...f, _n: norm(f.path) })).filter((f) => !exclude || !exclude(f)).sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }));
  const inp = h('input', { class: 'pagefilter', style: 'margin:0;width:100%', placeholder: 'Type part of a folder name…' });
  const list = h('div', { class: 'alist', style: 'max-height:45vh;margin-top:8px' });
  const draw = () => { const f = norm(inp.value); fill(list, all.filter((x) => !f || x._n.includes(f)).slice(0, 200).map((x) => h('div', { class: 'frow', onclick: () => { back.remove(); onPick(x); } }, icon('folder'), h('span', { class: 'nm' }, x.path), x.nAll ? h('span', { class: 'ct' }, nf.format(x.nAll)) : null))); };
  inp.oninput = draw; inp.onkeydown = (e) => e.stopPropagation();
  const back = modal(title, inp, list, h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel')));
  draw(); inp.focus();
}
export function movePhotos(ids, after) {
  folderPicker(`Move ${plural(ids.length, 'photo')} to…`, async (f) => {
    let r;
    try { r = await post('/api/photos/move', { ids, album: f.id }); } catch (e) { toast(e.message, null, null, 10000); return; }
    ids.forEach((i) => details.delete(i));
    toast(`${plural(r.moved.length, 'photo')} moved to ${f.path}.${r.failed.length ? ` ${nf.format(r.failed.length)} could not be: ${r.failed[0].error}.` : ''}`, null, null, 8000);
    after?.(r);
    S.keepView = true; S.viewKey = null; await pollStatus(); onRoute();
  });
}
/** Rename the selected files by the date they were taken; ids, sidecars and history stay with them, Undo puts the names back. */
export function renameByDate(ids) {
  const ex = { date: '2026-10-17_143205.jpg', 'date-name': '2026-10-17_IMG_1234.jpg', 'date-seq': '2026-10-17_001.jpg' };
  const pat = h('select', { class: 'sharepick', onchange: () => fill(eg, 'For example: ' + ex[pat.value]) }, [['date', 'Date and time'], ['date-name', 'Date and original name'], ['date-seq', 'Date and number']].map(([v, l]) => h('option', { value: v }, l)));
  const eg = h('div', { class: 'muted', style: 'font-size:12.5px' }, 'For example: ' + ex.date);
  const go_ = h('button', { class: 'pillbtn on', onclick: async () => {
    go_.disabled = true; fill(go_, 'Renaming…');
    let r;
    try { r = await post('/api/photos/rename-pattern', { ids, pattern: pat.value }); } catch (e) { toast(e.message, null, null, 9000); back.remove(); return; }
    back.remove();
    for (const x of r.renamed) details.delete(x.id);
    S.keepView = true; pollSoon(600);
    const undo = async () => { let n = 0; for (const x of r.renamed) { try { await post('/api/photos/rename', { id: x.id, name: x.was }); n++; } catch { /* reported below */ } } toast(`${plural(n, 'name')} put back.`); S.keepView = true; pollSoon(600); };
    toast(`${plural(r.renamed.length, 'file')} renamed${r.unchanged ? `, ${nf.format(r.unchanged)} already named so` : ''}${r.failed.length ? `; ${nf.format(r.failed.length)} not: ${r.failed[0].error}` : ''}.`, r.renamed.length ? 'Undo' : null, undo, 12000);
  } }, 'Rename');
  const back = modal(`Rename ${plural(ids.length, 'file')} by date`, h('div', { style: 'display:grid;gap:8px' }, pat, eg,
    h('p', { class: 'muted', style: 'font-size:12.5px;margin:0' }, 'The files are renamed in their folders, with their sidecars. Extensions stay. A name already taken gets -2, -3. digiKam sees the same files under their new names.')),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'), go_));
}
export async function renamePhoto(d) {
  const n = prompt('New file name (the extension stays)', d.name);
  if (!n || n.trim() === d.name) return;
  try { const r = await post('/api/photos/rename', { id: d.id, name: n.trim() }); if (r.failed.length) throw new Error(r.failed[0].error); toast(`Renamed to ${r.moved[0]?.name || n.trim()}.`); } catch (e) { toast(e.message, null, null, 9000); return; }
  details.delete(d.id); pollSoon(600);
  if (LB.open && LB.src.id[LB.i] === d.id) setTimeout(() => { LB.detail = null; showItem(); }, 1500);
}
export function deletePhotos(ids, after) {
  const n = ids.length;
  const go = h('button', { class: 'pillbtn danger', onclick: async () => {
    go.disabled = true; go.textContent = 'Deleting…';
    let r;
    try { r = await post('/api/photos/delete', { ids, confirm: true }); } catch (e) { back.remove(); toast(e.message, null, null, 10000); return; }
    back.remove();
    const where = (r.backup || '').split('/').slice(-2).join('/');
    toast(`Deleted ${plural(r.deleted, 'photo')}.${r.failed.length ? ` ${nf.format(r.failed.length)}: ${r.failed[0].error}.` : ''} A copy is kept in the data folder (${where}).`, null, null, 12000);
    ids.forEach((i) => { details.delete(i); G.sel.delete(i); ST.fav.delete(i); });
    updateSelBar();
    after?.(r);
    S.keepView = true; S.viewKey = null; await pollStatus(); onRoute();
  } }, icon('trash'), n === 1 ? 'Delete the photo' : `Delete ${nf.format(n)} photos`);
  const back = modal(n === 1 ? 'Delete this photo?' : `Delete ${nf.format(n)} photos?`,
    h('div', { class: 'delthumbs' }, ids.slice(0, 12).map((id) => h('img', { src: `/thumb/${id}`, alt: '' })), n > 12 ? h('span', { class: 'muted' }, `+${nf.format(n - 12)}`) : null),
    h('p', null, `${n === 1 ? 'The file and its sidecar are' : 'The files and their sidecars are'} deleted from the disk and removed from ${S.SYNC_OFF ? 'the library' : 'digiKam’s database'}, with ${n === 1 ? 'its' : 'their'} tags, faces, captions, favorites and album places.`),
    h('p', { class: 'muted', style: 'font-size:12.5px' }, 'Before anything is removed, the files and sidecars are copied to the viewer’s data folder (deleted-photos) and the database is backed up, so they can be recovered by hand.'),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'), go));
}
export async function setFavorite(ids, on) {
  try { await post('/api/favorites', { ids, on }); } catch { toast('Could not save the favorite.'); return; }
  ids.forEach((id) => (on ? ST.fav.add(id) : ST.fav.delete(id)));
  ST.rev++;
  if (S.route.view === 'photos' && G.data) redrawTiles();
  updateSelBar();
  if (LB.open) updateFavBtn();
  if (ids.length > 1) toast(`${on ? 'Added' : 'Removed'} ${nf.format(ids.length)} ${on ? 'to' : 'from'} favorites.`);
}
export function modal(title, ...kids) {
  const back = h('div', { class: 'modal-back', onclick: (e) => { if (e.target === back) back.remove(); } }, h('div', { class: 'modal' }, h('h3', null, title), ...kids));
  document.body.append(back);
  return back;
}
/** Maintenance › Metadata quality (was Face quality): photos without a location, and faces stored twice, the same person in two places, boxes outside the photo or empty.
 *  The check runs every time the page is opened, and again with "Check again". */
export async function showQuality() {
  const body = h('div', null, h('div', { class: 'muted' }, 'Checking every face in the library…'));
  main.replaceChildren(h('div', { class: 'page spage cpage' }, crumbs([['Maintenance', '#/maintenance'], ['Face quality', null]]),
    pageTitle('face', 'amber', 'Metadata quality'),
    h('p', { class: 'muted', style: 'max-width:820px;margin-top:0' }, 'Every face region in digiKam’s database is checked: the same person stored twice a few pixels apart (shown twice in the people list), the same person in two separate places, and boxes that stick out of the photo or are empty. Fixes go through the normal face changes, so each one is in the change log with undo.'),
    body));
  let q;
  try { q = await api('/api/quality'); } catch (e) { fill(body, h('div', { class: 'empty' }, e.message)); return; }
  if (S.route.view !== 'quality') return;
  const p = q.progress || {};
  const again = h('button', { class: 'pillbtn', onclick: () => showQuality() }, icon('spark'), 'Check again');
  // the photos of a list open in the viewer as a set (step through them with the arrows), faces shown
  const thumb = (x, all) => h('img', { src: `/thumb/${x.photo}`, alt: '', loading: 'lazy', title: 'Open in the viewer', onclick: () => {
    const ids = (all || [x]).map((y) => y.photo).filter((v, i, arr) => arr.indexOf(v) === i);
    openLightbox({ id: ids }, Math.max(0, ids.indexOf(x.photo)), true);
    setTimeout(() => { if (LB.open && !LB.faces) toggleFaces(); }, 600);
  } });
  const size = store.get('qsize', 'large');
  // one entry handled: it folds away where it is, its list's count goes down, and the page stays where it was
  const gone = (ev) => {
    const row = ev?.target?.closest('.hrow'), sec = row?.closest('.qsec');
    if (!row) return false;
    row.classList.add('qgone');
    setTimeout(() => row.remove(), 250);
    const n = sec?.querySelector('h2 .muted');
    if (n) n.textContent = nf.format(Math.max(0, (parseInt(n.textContent.replace(/\D/g, ''), 10) || 1) - 1));
    return true;
  };
  // a list: its first entries, and "Load the next ..." appending more in place (the page is not redrawn)
  const sec = (title, n, intro, acts, pager) => {
    const all = pager?.list || [];  // the list itself grows, so the buttons that act on all shown see the new ones
    const box = h('div', { class: 'hlist qitems ' + size }, all.map((x) => pager.row(x, all)));
    const more = h('button', { class: 'pillbtn', style: 'margin-top:10px' });
    const label = () => { const left = n - all.length; more.hidden = !pager || left <= 0; fill(more, `Load the next ${nf.format(Math.min(PAGE, left))}`, h('span', { class: 'muted' }, ` · ${nf.format(left)} more`)); };
    more.onclick = async () => {
      more.disabled = true;
      try {
        const r = await api(`/api/quality/items?check=${pager.key}&offset=${all.length}&limit=${PAGE}`);
        const fresh = r.items.filter((x) => !all.some((y) => y.photo === x.photo && (y.name || '') === (x.name || '') && JSON.stringify(y.rect || y.rects || '') === JSON.stringify(x.rect || x.rects || '')));
        all.push(...fresh);
        box.append(...fresh.map((x) => pager.row(x, all)));
        if (!fresh.length) n = all.length;
      } catch (e) { toast(e.message, null, null, 9000); }
      more.disabled = false; label();
    };
    label();
    return h('div', { class: 'qsec' },
      h('h2', null, title, h('span', { class: 'muted', style: 'font-size:14px;font-weight:400;margin-left:8px' }, nf.format(n))),
      intro ? h('p', { class: 'muted', style: 'margin:0 0 10px;max-width:820px' }, intro) : null,
      acts ? h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px' }, acts) : null,
      all.length ? box : null, more);
  };
  const PAGE = 120;
  const busy = p.running ? h('div', { class: 'enote' }, p.files ? `Fixing the image files… ${nf.format(p.done)} / ${nf.format(p.total)}.` : `Removing duplicate faces… ${nf.format(p.done)} / ${nf.format(p.total)} photos.`) : null;
  if (p.running) setTimeout(() => { if (S.route.view === 'quality') showQuality(); }, 4000);
  const near = q.near;
  const fitOne = async (x, ev) => { try { const r = await post('/api/quality/fit', { photos: [x.photo] }); toast(r.fitted ? 'Box trimmed to the photo.' : r.removed ? 'Empty box removed.' : 'Nothing to fix there.'); } catch (e) { toast(e.message, null, null, 9000); return; } if (!gone(ev)) setTimeout(showQuality, 1200); };
  const fitAll = q.invalid.count && S.CAN_MANAGE ? h('button', { class: 'pillbtn on', onclick: async () => {
    if (!confirm(`Trim ${plural(q.invalid.count, 'face box', 'face boxes')} to the edges of their photos (empty ones are removed)? Each change is in the change log with undo.`)) return;
    try { const r = await post('/api/quality/fit', {}); toast(`${nf.format(r.fitted)} trimmed, ${nf.format(r.removed)} removed${r.failed ? `, ${nf.format(r.failed)} failed` : ''}.`, null, null, 7000); } catch (e) { toast(e.message, null, null, 9000); return; }
    setTimeout(showQuality, 1500);
  } }, 'Fit them all inside their photos') : null;
  const sizes = h('div', { class: 'eseg', title: 'Picture size' }, [['list', 'List'], ['large', 'Large'], ['huge', 'Larger']].map(([k, l]) => h('button', { class: k === size ? 'on' : '', onclick: () => { store.set('qsize', k); showQuality(); } }, l)));
  fill(body, h('div', { style: 'display:flex;gap:10px;align-items:center;margin:4px 0 6px;flex-wrap:wrap' }, again, h('span', { class: 'muted' }, `${nf.format(q.checked)} face regions checked ${fmtWhen(q.at)}.`), h('span', { style: 'flex:1' }), sizes), busy,
    sec('Stored twice', near.extra, near.extra ? `${plural(near.extra, 'region is a copy', 'regions are copies')} of another one of the same person a few pixels away, on ${plural(near.photos, 'photo')}.` : 'No face is stored twice.',
      near.extra && S.CAN_MANAGE && !p.running ? [h('button', { class: 'pillbtn on', onclick: async () => {
        if (!confirm(`Remove ${nf.format(near.extra)} duplicate face regions on ${plural(near.photos, 'photo')}? One of each pair is kept; the sidecars are rewritten.`)) return;
        try { const r = await post('/api/quality/fix', {}); toast(`Cleaning up ${plural(r.photos, 'photo')} in the background.`); } catch (e) { toast(e.message, null, null, 9000); }
        setTimeout(showQuality, 1500);
      } }, `Remove ${nf.format(near.extra)} duplicates`)]
        : p.finished && !p.files && !p.running && S.CAN_MANAGE ? [h('button', { class: 'pillbtn', onclick: async () => {
          if (!confirm('Rewrite the face regions inside the image files of the cleaned photos that still carry the duplicates? Each file is backed up first and its pixels are checked unchanged.')) return;
          try { const r = await post('/api/quality/fix', { files: true }); toast(`Looking at ${plural(r.photos, 'image file')} in the background.`); } catch (e) { toast(e.message, null, null, 9000); }
          setTimeout(showQuality, 1500);
        } }, 'Fix the image files too')] : null,
      { list: near.examples, key: 'near', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ` · ${x.apart} px apart`)) }),
    sec('Same person in two places', q.separate.count, 'Usually one of the two is someone else, named by mistake. Open the photo, show the faces (F), and rename, adjust or remove the wrong box.', null,
      { list: q.separate.items, key: 'separate', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ` · ${x.rects.length} boxes`)) }),
    (() => {  // photos whose faces are all unnamed: name them, ignore them, or accept them as they are
      const u = q.unnamed, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'unnamed', on }); toast(on ? `${plural(ids.length, 'photo')} accepted as they are.` : 'Shown again.'); } catch (e) { toast(e.message, null, null, 9000); return; } if (!gone(ev)) showQuality(); };
      const ignore = async (ids, ev) => {
        if (!ev && !confirm(`Mark the unnamed faces on ${plural(ids.length, 'photo')} as not faces to name? Each change is in the change log with undo.`)) return;
        try { const r = await post('/api/quality/ignore', { photos: ids }); toast(`${plural(r.ignored, 'face')} ignored${r.failed ? `, ${nf.format(r.failed)} failed` : ''}.`); } catch (e) { toast(e.message, null, null, 9000); return; }
        if (!gone(ev)) setTimeout(showQuality, 1200);
      };
      const nameThem = (x) => { openLightbox({ id: [x.photo] }, 0, true); setTimeout(() => { if (!LB.faces) toggleFaces(); }, 700); };
      return sec('Faces without a name', u.count,
        `Photos with faces where none is named.${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted as they are ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Name the faces, mark them as not faces to name, or accept the photo as it is: it leaves this list until its faces change.`,
        items.length ? [h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown'),
          S.CAN_MANAGE ? h('button', { class: 'pillbtn', onclick: () => ignore(items.map((x) => x.photo)) }, 'Ignore their faces') : null,
          u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=unnamed'); if (acc.photos.length && confirm(`List the ${plural(acc.photos.length, 'accepted photo')} again?`)) accept(acc.photos, false); } }, 'Show the accepted ones again') : null]
          : u.accepted ? [h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=unnamed'); if (acc.photos.length && confirm(`List the ${plural(acc.photos.length, 'accepted photo')} again?`)) accept(acc.photos, false); } }, 'Show the accepted ones again')] : null,
        { list: items, key: 'unnamed', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, plural(x.faces, 'unnamed face')), ' · ',
          h('a', { class: 'lnk', onclick: () => nameThem(x) }, 'Name them'), ' · ',
          S.CAN_MANAGE ? [h('a', { class: 'lnk', onclick: (ev) => ignore([x.photo], ev) }, 'Ignore'), ' · '] : null,
          h('a', { class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is'))) });
    })(),
    (() => {  // people in the photo (object detection), nobody marked or tagged
      const u = q.untagged || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'untagged', on }); toast(on ? `${plural(ids.length, 'photo')} accepted as they are.` : 'Shown again.'); } catch (e) { toast(e.message, null, null, 9000); return; } if (!gone(ev)) showQuality(); };
      const find = async (ids, ev) => {
        let r;
        try { r = await post('/api/faces/detect', { ids }); toast(r.done ? `${plural(r.added || 0, 'face')} found${r.added ? '; name them from the photo, or under Faces without a name after Check again' : ''}.` : `Looking for faces in ${plural(r.queued, 'photo')} in the background.`, null, null, 7000); } catch (e) { toast(e.message, null, null, 9000); return; }
        if (ev && r.done && r.added) gone(ev); else if (!ev) setTimeout(showQuality, 2500);
      };
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=untagged'); if (acc.photos.length && confirm(`List the ${plural(acc.photos.length, 'accepted photo')} again?`)) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      return sec('People, but nobody tagged', u.count,
        `Photos where object detection sees people but no face is marked and no person is tagged (crowds, people from behind, strangers…).${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Find their faces to name them, or accept the photo as it is: it leaves this list unless it changes.`,
        [items.length ? h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown') : null,
          items.length && S.FACE_DETECT && S.CAN_MANAGE ? h('button', { class: 'pillbtn', onclick: () => find(items.map((x) => x.photo)) }, 'Find faces in all shown') : null, again].filter(Boolean),
        { list: items, key: 'untagged', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, plural(x.people, 'person', 'people')), ' detected · ',
          S.FACE_DETECT && S.CAN_MANAGE ? [h('a', { class: 'lnk', onclick: (ev) => find([x.photo], ev) }, 'Find faces'), ' · '] : null,
          h('a', { class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is'))) });
    })(),
    (() => {  // no location: set one, or accept the photo as it is
      const u = q.nolocation || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'nolocation', on }); toast(on ? `${plural(ids.length, 'photo')} accepted without a location.` : 'Shown again.'); } catch (e) { toast(e.message, null, null, 9000); return; } if (!gone(ev)) showQuality(); };
      const place = (ids, ev) => locationDialog(ids, null, () => { if (!gone(ev)) setTimeout(showQuality, 1200); });
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=nolocation'); if (acc.photos.length && confirm(`List the ${plural(acc.photos.length, 'accepted photo')} again?`)) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      return sec('No location', u.count,
        `Photos and videos with no place recorded.${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted without one ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Set the location (the dialog suggests the places of photos taken around the same time), place them on the map (Map › Place photos), or accept the photo as it is: it leaves this list for good.`,
        [items.length && S.EDITING ? h('button', { class: 'pillbtn', onclick: () => place(items.map((x) => x.photo)) }, 'Set location for all shown') : null,
          items.length ? h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown') : null,
          h('a', { class: 'pillbtn', href: '#/map' }, 'Open the map'), again].filter(Boolean),
        { list: items, key: 'nolocation', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' · ', x.ts ? dfDay.format(date(x.ts)) : 'no date', ' · ',
          S.EDITING ? [h('a', { class: 'lnk', onclick: (ev) => place([x.photo], ev) }, 'Set location'), ' · '] : null,
          h('a', { class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is'))) });
    })(),
    (() => {  // a date that cannot be right (1900, the future, 1970-01-01 00:00:00): set the right one, or accept it
      const u = q.baddate || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'baddate', on }); toast(on ? `${plural(ids.length, 'date')} accepted as ${ids.length === 1 ? 'it is' : 'they are'}.` : 'Shown again.'); } catch (e) { toast(e.message, null, null, 9000); return; } if (!gone(ev)) showQuality(); };
      const fixDate = (x, ev) => { const back = modal(`Date of ${x.name}`, h('p', { class: 'muted', style: 'margin-top:0' }, `Now ${x.taken.replace('T', ' ')}: ${x.reason}.`),
        dateEditor([x.photo], x.taken.startsWith('1') && +x.taken.slice(0, 4) < 1901 ? null : x.taken, () => { back.remove(); if (!gone(ev)) setTimeout(showQuality, 1200); }),
        h('div', { style: 'text-align:right;margin-top:10px' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close'))); };
      const useFolder = async (list, ev) => {
        let n = 0;
        for (const x of list) { if (x.folder_date && await editPhotos([x.photo], { taken: x.folder_date })) n++; }
        if (list.length > 1) toast(`${plural(n, 'photo')} dated from ${n === 1 ? 'its' : 'their'} folder.`);
        if (!gone(ev)) setTimeout(showQuality, 1200);
      };
      const withFolder = items.filter((x) => x.folder_date);
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=baddate'); if (acc.photos.length && confirm(`List the ${plural(acc.photos.length, 'accepted photo')} again?`)) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      return sec('Implausible date', u.count,
        `Photos and videos dated before 1901, in the future, or exactly at a clock’s starting point (1 January 1970 at midnight and the like): a date the camera or a program wrote when it knew none.${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} A folder named for that year counts as meant (scans of old prints). Set the right date, take the folder’s date, or accept it as it is.`,
        [withFolder.length && S.EDITING ? h('button', { class: 'pillbtn', onclick: () => { if (confirm(`Date ${plural(withFolder.length, 'photo')} from the folder ${withFolder.length === 1 ? 'it is' : 'they are'} in (its date, at noon)?`)) useFolder(withFolder); } }, `Take the folder’s date for ${nf.format(withFolder.length)}`) : null,
          items.length ? h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown') : null, again].filter(Boolean),
        { list: items, key: 'baddate', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' · ', x.taken.replace('T', ' '), ' · ', x.reason, ' · in ',
          h('a', { class: 'lnk', href: `#/photos?album=${x.folder_id}&sort=old` }, x.folder.replace(/^\//, '') || 'the library root'), h('div', { style: 'font-size:12.5px;margin-top:2px' },
          S.EDITING ? [h('a', { class: 'lnk', onclick: (ev) => fixDate(x, ev) }, 'Set the date'), ' · '] : null,
          S.EDITING && x.folder_date ? [h('a', { class: 'lnk', onclick: (ev) => useFolder([x], ev) }, `Use the folder’s date (${x.folder_date.slice(0, 10)})`), ' · '] : null,
          h('a', { class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is')))) });
    })(),
    (() => {  // the date is outside the date the folder is named for: fix the date, move the file, or accept it
      const u = q.wrongfolder || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'wrongfolder', on }); toast(on ? `${plural(ids.length, 'photo')} accepted where ${ids.length === 1 ? 'it is' : 'they are'}.` : 'Shown again.'); } catch (e) { toast(e.message, null, null, 9000); return; } if (!gone(ev)) showQuality(); };
      const moveTo = async (list, ev) => {
        const by = new Map();
        for (const x of list) if (x.suggest) (by.get(x.suggest.id) || by.set(x.suggest.id, []).get(x.suggest.id)).push(x.photo);
        let n = 0, failed = 0;
        for (const [album, ids] of by) { try { const r = await post('/api/photos/move', { ids, album }); n += r.moved.length; failed += r.failed.length; } catch (e) { toast(e.message, null, null, 9000); return; } }
        ids_(list).forEach((i) => details.delete(i));
        toast(`${plural(n, 'photo')} moved to the folder for ${n === 1 ? 'its' : 'their'} date${failed ? `; ${nf.format(failed)} could not be` : ''}.`, null, null, 7000);
        S.keepView = true; pollSoon(600);
        if (!gone(ev)) setTimeout(showQuality, 1500);
      };
      const ids_ = (list) => list.map((x) => x.photo);
      const fixDate = (x, ev) => { const back = modal(`Date of ${x.name}`, h('p', { class: 'muted', style: 'margin-top:0' }, `In “${x.folder.replace(/^\//, '')}”, a folder for ${x.expect}.`),
        dateEditor([x.photo], x.taken, () => { back.remove(); if (!gone(ev)) setTimeout(showQuality, 1200); }), h('div', { style: 'text-align:right;margin-top:10px' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close'))); };
      const pick = (x, ev) => movePhotos([x.photo], () => { gone(ev); });
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=wrongfolder'); if (acc.photos.length && confirm(`List the ${plural(acc.photos.length, 'accepted photo')} again?`)) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      return sec('Date doesn’t match the folder', u.count,
        `Photos and videos whose date is outside the date their folder is named for, like a photo from 2005 in 2020/202001: either the date in the file is wrong, or the file is in the wrong folder. A folder named for a day also holds the month after it (a trip or an event).${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Fix the date, move the file, or accept it where it is: it comes back only if its date or folder changes.`,
        [items.length ? h('button', { class: 'pillbtn', onclick: () => accept(ids_(items)) }, 'Accept all shown') : null, again].filter(Boolean),
        { list: items, key: 'wrongfolder', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' · taken ', x.ts ? dfDay.format(date(x.ts)) : x.taken.slice(0, 10), ' · in ',
          h('a', { class: 'lnk', href: `#/photos?album=${x.folder_id}&sort=old` }, x.folder.replace(/^\//, '')), h('div', { style: 'font-size:12.5px;margin-top:2px' },
          S.EDITING ? [h('a', { class: 'lnk', onclick: (ev) => fixDate(x, ev) }, 'Fix the date'), ' · '] : null,
          S.CAN_MANAGE ? [x.suggest ? h('a', { class: 'lnk', title: 'Move the file (with its sidecar) to the folder for its date', onclick: (ev) => moveTo([x], ev) }, `Move to ${x.suggest.path.replace(/^\//, '')}`) : h('a', { class: 'lnk', onclick: (ev) => pick(x, ev) }, 'Move to a folder…'), ' · '] : null,
          h('a', { class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is')))) });
    })(),
    sec('Outside the photo or empty', q.invalid.count, 'A box that sticks out of the photo can be trimmed to its edges here, or moved and resized in the viewer (click the face, “Adjust the box”). Empty boxes are removed.', fitAll ? [fitAll] : null,
      { list: q.invalid.items, key: 'invalid', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' ',
        x.rect ? `${x.rect[2]} × ${x.rect[3]} at ${x.rect[0]}, ${x.rect[1]} on a ${x.size[0]} × ${x.size[1]} photo` : 'unreadable rectangle',
        S.CAN_MANAGE && x.fix ? [' · ', h('a', { class: 'lnk', onclick: (ev) => fitOne(x, ev) }, x.fix === 'fit' ? 'Fit inside' : 'Remove')] : null)) }));
}
/** Import: photos and videos from this device (or shared to the installed app) go into folders by date,
 *  duplicates are left out, each new file gets a sidecar. */
export async function showImport(params) {
  const body = h('div');
  main.replaceChildren(h('div', { class: 'page' }, pageTitle('add', 'teal', 'Import'),
    h('p', { class: 'muted', style: 'max-width:820px;margin-top:0' }, 'Photos and videos from this device go into folders by the date they were taken, under the library root. Files already in the library (same size and contents) are left out. Each new file gets a sidecar with its own metadata, and digiKam reads the full metadata on its next run. On Android, the installed app also appears in the Share sheet; on an iPhone, choose from the photo library below.'),
    body));
  if (!S.CAN_MANAGE) { fill(body, h('div', { class: 'empty' }, 'Importing needs an editor or admin account.')); return; }
  let lay; try { lay = await api('/api/import/layouts'); } catch (e) { fill(body, h('div', { class: 'empty' }, e.message)); return; }
  let token = params.get('staged') || null, plan = null;
  const layoutSel = h('select', { class: 'sharepick' }, lay.layouts.map((l) => h('option', { value: l.key, selected: l.key === lay.layout }, `${l.pattern}  ·  ${l.example}`)));
  const remember = S.IS_ADMIN ? h('label', { style: 'display:flex;gap:6px;align-items:center;font-size:12.5px;cursor:pointer' }, h('input', { type: 'checkbox', checked: true }), 'Remember as the default layout') : null;
  const dupSel = h('select', { class: 'sharepick' }, h('option', { value: 'skip', selected: true }, 'Leave duplicates out'), h('option', { value: 'import' }, 'Import duplicates too'));
  const status = h('div', { class: 'muted', style: 'margin:8px 0' });
  const table = h('div');
  const run = h('button', { class: 'pillbtn on', hidden: true });
  const drop = h('div', { class: 'dropzone' }, icon('add'), h('div', null, h('b', null, 'Drop photos here'), h('div', { class: 'muted' }, 'or')),
    h('button', { class: 'pillbtn', onclick: () => pick(false) }, 'Choose photos…'), h('button', { class: 'pillbtn', onclick: () => pick(true) }, 'Take a photo'));
  const pick = (camera) => {
    const inp = h('input', { type: 'file', multiple: !camera, accept: 'image/*,video/*', hidden: true });
    if (camera) inp.setAttribute('capture', 'environment');
    inp.onchange = () => { const files = [...inp.files]; inp.remove(); if (files.length) send(files); };
    document.body.append(inp); inp.click();
  };
  const send = async (files) => {
    fill(status, `Uploading ${plural(files.length, 'file')}…`);
    try {
      let done = 0;
      for (let i = 0; i < files.length; i += 20) {  // in batches, so a phone upload does not hold one request for minutes
        const fd = new FormData(); files.slice(i, i + 20).forEach((f) => fd.append('files', f)); if (token) fd.append('token', token); fd.append('layout', layoutSel.value);
        const res = await fetch('/api/import/stage', { method: 'POST', body: fd });
        const r = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(r.detail || `${res.status}`);
        token = r.token; plan = r; done += Math.min(20, files.length - i);
        fill(status, `Uploaded ${nf.format(done)} of ${nf.format(files.length)}…`);
      }
    } catch (e) { fill(status, h('span', { class: 'warn' }, e.message)); return; }
    location.hash = `#/import?staged=${token}`;
    draw();
  };
  const draw = async () => {
    if (!token) { fill(status, ''); fill(table); run.hidden = true; return; }
    if (!plan) { try { plan = await api(`/api/import/${token}?layout=${layoutSel.value}`); } catch (e) { fill(status, h('span', { class: 'warn' }, e.message)); token = null; return; } }
    const items = plan.items;
    fill(status, `${plural(items.length, 'file')} ready: ${nf.format(plan.new)} new${plan.duplicates ? `, ${nf.format(plan.duplicates)} already in the library` : ''}${items.some((i) => i.error) ? `, ${nf.format(items.filter((i) => i.error).length)} not accepted` : ''}.`);
    fill(table, h('table', { class: 'stable wide' }, h('thead', null, h('tr', null, ['File', 'Taken', 'Goes to', ''].map((c) => h('th', null, c)))),
      h('tbody', null, items.map((i) => h('tr', { class: i.error ? 'muted' : i.duplicate_of ? 'dup' : '' },
        h('td', null, i.name, h('div', { class: 'muted', style: 'font-size:11.5px' }, fmtBytes(i.size))),
        h('td', null, i.when ? i.when.replace('T', ' ').slice(0, 16) : ''),
        h('td', null, i.folder || ''),
        h('td', null, i.error ? h('span', { class: 'warn' }, i.error) : i.duplicate_of ? h('a', { class: 'lnk', onclick: () => openLightbox({ id: [i.duplicate_of] }, 0, true) }, 'already in the library') : h('span', { class: 'mst ok' }, 'new')))))));
    run.hidden = false;
    fill(run, icon('add'), plan.new ? `Import ${nf.format(dupSel.value === 'import' ? items.filter((i) => !i.error).length : plan.new)}` : 'Nothing new to import');
    run.disabled = !plan.new && dupSel.value !== 'import';
  };
  layoutSel.onchange = () => { plan = null; draw(); };
  dupSel.onchange = () => draw();
  run.onclick = async () => {
    run.disabled = true; fill(run, 'Importing…');
    let r;
    try { r = await post(`/api/import/${token}/run`, { layout: layoutSel.value, duplicates: dupSel.value, remember: !!remember?.firstChild.checked }); } catch (e) { toast(e.message, null, null, 10000); run.disabled = false; draw(); return; }
    const folders = [...new Set(r.added.map((a) => a.folder))];
    toast(`${plural(r.added.length, 'photo')} imported${folders.length ? ` into ${folders.slice(0, 3).join(', ')}${folders.length > 3 ? '…' : ''}` : ''}${r.skipped.length ? `; ${plural(r.skipped.length, 'file')} left out` : ''}.`, r.added.length ? 'Show' : null, () => go('photos', photosWith({ album: undefined, sort: 'new' })), 12000);
    token = null; plan = null; location.hash = '#/import'; S.keepView = true; pollSoon(600);
    fill(status, r.added.length ? h('span', null, `${plural(r.added.length, 'photo')} imported. `, h('a', { class: 'lnk', href: '#/folders' }, 'Open Folders')) : 'Nothing was imported.'); fill(table); run.hidden = true;
  };
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { const files = [...e.dataTransfer.files]; if (files.length) send(files); });
  fill(body, drop,
    h('div', { style: 'display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:14px 0 4px' }, h('span', null, 'Folders: '), layoutSel, remember, h('span', { style: 'margin-left:8px' }, 'Duplicates: '), dupSel),
    status, table, h('div', { style: 'margin-top:12px;display:flex;gap:8px' }, run, token ? h('button', { class: 'pillbtn', onclick: async () => { await post(`/api/import/${token}/discard`, {}).catch(() => {}); token = null; plan = null; location.hash = '#/import'; draw(); } }, 'Discard') : null));
  if (params.get('error') === 'off') fill(status, h('span', { class: 'warn' }, 'Imports are turned off on this server.'));
  if (params.get('error') === 'signin') fill(status, h('span', { class: 'warn' }, 'Sign in as an editor, then share the photos again.'));
  draw();
}
/** The trash: what was moved there here or in digiKam (the same .dtrash folders), with restore and delete for good. */
export async function showTrash() {
  const body = h('div');
  main.replaceChildren(h('div', { class: 'page' }, pageTitle('trash', 'slate', 'Trash'),
    h('p', { class: 'muted', style: 'max-width:820px;margin-top:0' }, 'Photos moved to the trash, here or in digiKam, kept with their sidecars in each album root’s .dtrash folder until they are restored or deleted for good. Click a photo to select it.'), body));
  let d; try { d = await api('/api/trash'); } catch (e) { fill(body, h('div', { class: 'empty' }, e.message)); return; }
  if (S.route.view !== 'trash') return;
  const sel = new Set();
  const act = async (path, payload, msg) => { try { const r = await post(path, payload); toast(msg(r), null, null, 8000); } catch (e) { toast(e.message, null, null, 9000); return; } pollSoon(600); showTrash(); };
  const chosen = () => (sel.size ? d.items.filter((t) => sel.has(t.info)) : d.items);
  const bar = h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0 12px' });
  const drawBar = () => fill(bar,
    h('span', { class: 'muted' }, d.items.length ? `${plural(d.items.length, 'item')} · ${fmtBytes(d.bytes)}${sel.size ? ` · ${nf.format(sel.size)} selected` : ''}` : 'The trash is empty.'),
    d.items.length ? h('button', { class: 'pillbtn', onclick: () => act('/api/trash/restore', { infos: chosen().map((t) => t.info) }, (r) => `${plural(r.restored.length, 'photo')} restored${r.failed.length ? `, ${r.failed.length} not (${r.failed[0].error})` : ''}.`) }, sel.size ? `Restore ${nf.format(sel.size)}` : 'Restore all') : null,
    d.items.length ? h('button', { class: 'pillbtn danger', onclick: () => { const c = chosen(); if (!confirm(`Delete ${plural(c.length, 'photo')} for good? This cannot be undone.`)) return; act('/api/trash/empty', sel.size ? { infos: c.map((t) => t.info) } : { all: true }, (r) => `${plural(r.deleted, 'photo')} deleted for good.`); } }, sel.size ? `Delete ${nf.format(sel.size)} for good` : 'Empty the trash') : null,
    sel.size ? h('a', { class: 'lnk', onclick: () => { sel.clear(); drawBar(); draw(); } }, 'Select none') : null);
  const grid = h('div', { class: 'cards' });
  const draw = () => fill(grid, d.items.map((t) => h('div', { class: 'card' + (sel.has(t.info) ? ' sel' : ''), onclick: () => { sel.has(t.info) ? sel.delete(t.info) : sel.add(t.info); drawBar(); draw(); } },
    h('span', { class: 'cv' }, t.exists ? h('img', { src: `/api/trash/thumb?info=${encodeURIComponent(t.info)}`, alt: '', loading: 'lazy' }) : h('span', { class: 'muted' }, 'file missing'),
      sel.has(t.info) ? h('span', { class: 'privbadge' }, icon('check')) : null),
    h('div', { class: 'nm' }, t.name), h('div', { class: 'sb' }, `${t.when ? fmtWhen(Math.floor(new Date(t.when).getTime() / 1000)) : ''} · ${t.path.split('/').slice(-2, -1)[0] || ''}`))));
  drawBar(); draw();
  fill(body, bar, grid);
}
/** Maintenance › Server log: the end of data/viewer.log, filtered by level and text; page requests hidden unless asked. */
export function logDialog() {
  const level = h('select', null, h('option', { value: 'any' }, 'Everything'), h('option', { value: 'warning' }, 'Warnings and errors'), h('option', { value: 'error' }, 'Errors only'));
  const text = h('input', { class: 'einput', placeholder: 'Filter, e.g. sync or a file name', style: 'margin:0;flex:1;min-width:140px', onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') load(); } });
  const reqs = h('input', { type: 'checkbox' });
  const pre = h('pre', { class: 'logview' }, 'Loading…');
  const info = h('div', { class: 'muted', style: 'font-size:12px' });
  async function load() {
    let r; try { r = await api(`/api/log?lines=500&level=${level.value}&requests=${reqs.checked ? 1 : 0}&contains=${encodeURIComponent(text.value.trim())}`); } catch (e) { pre.textContent = e.message; return; }
    pre.textContent = r.lines.length ? r.lines.join('\n') : (r.note || 'Nothing matches.');
    pre.scrollTop = pre.scrollHeight;
    fill(info, `${r.file} · ${fmtBytes(r.size_bytes)} · showing the last ${nf.format(r.returned)} lines`);
  }
  level.onchange = load; reqs.onchange = load;
  const back = modal('Server log',
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px' }, level, text,
      h('label', { style: 'display:flex;gap:6px;align-items:center;font-size:13px;cursor:pointer' }, reqs, 'Page requests'),
      h('button', { class: 'pillbtn', onclick: load }, 'Refresh')),
    pre, info,
    h('div', { class: 'modal-btns' }, h('a', { class: 'pillbtn', href: '/api/log/download', download: '' }, 'Download the log'), h('button', { class: 'pillbtn on', onclick: () => back.remove() }, 'Close')));
  back.firstChild.classList.add('wide');
  load();
}
/** Shared (everyone sees it; editors and admins change it) or private (only you): a select when both are allowed. */
export function sharingPick(def) {
  if (ST.canShare && ST.canPrivate) return h('select', { class: 'sharepick', title: 'Who sees this album' },
    h('option', { value: '1', selected: def !== false }, 'Shared with everyone'), h('option', { value: '0', selected: def === false }, 'Private (only me)'));
  return h('span', { class: 'muted', style: 'font-size:12.5px', 'data-shared': ST.canShare ? '1' : '0' }, ST.canShare ? 'Shared with everyone' : 'Private (only you see it)');
}
export const pickedShared = (el) => (el.tagName === 'SELECT' ? el.value === '1' : el.dataset.shared === '1');
export function albumDialog(ids) {
  const name = h('input', { class: 'pagefilter', style: 'margin:0;flex:1;min-width:160px', placeholder: 'New album name' });
  const share = sharingPick();
  const add = async (id, label) => {
    try {
      if (id == null) { if (!name.value.trim()) { name.focus(); return; } await post('/api/ualbums', { name: name.value.trim(), ids, shared: pickedShared(share) }); label = name.value.trim(); }
      else await post(`/api/ualbums/${id}`, { ids, on: true });
    } catch (e) { toast(e.message || 'Could not update the album.'); return; }
    await loadState();
    back.remove();
    toast(`Added ${nf.format(ids.length)} to “${label}”.`, 'Open', () => { const a = ST.albums.find((x) => x.name === label); if (a) go('photos', photosWith({ ualbum: a.id })); });
  };
  const editable = ST.albums.filter((a) => a.canEdit);
  const back = modal(`Add ${plural(ids.length, 'item')} to an album`,
    h('div', { class: 'alist' }, editable.map((a) => h('div', { class: 'frow', onclick: () => add(a.id, a.name) }, icon(a.shared ? 'folder' : 'lock'), h('span', { class: 'nm' }, a.name), h('span', { class: 'ct' }, nf.format(a.n))))),
    ST.canShare || ST.canPrivate ? h('div', { style: 'display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center' }, name, share, h('button', { class: 'pillbtn on', onclick: () => add(null) }, icon('add'), 'Create')) : h('p', { class: 'muted' }, 'Sign in to make albums.'),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel')));
  name.onkeydown = (e) => { if (e.key === 'Enter') add(null); };
  if (!editable.length) name.focus();
}
