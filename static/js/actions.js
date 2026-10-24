/** Selection bar actions and app-side state: favorites, albums, trash, move, rename, the edit log entries. */
import { $, api, date, dfDay, errorBox, fill, fmtBytes, fmtWhen, h, icon, main, nf, norm, plural, post, store } from './core.js';
import { S, ST, filterParams, go, hashFor, loadState, onRoute, photosWith, pollSoon, toast, toastError, ualbumById } from './state.js';
import { crumbs } from './search.js';
import { G, dropPhotos, renderGrid } from './grid.js';
import { copyMetadata, dateEditor, editDialog, editPhotos, locationDialog, tagDialog } from './editing.js';
import { downloadDialog } from './download.js';
import { pageTitle } from './pages.js';
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
    G.data && G.sel.size < G.data.count ? h('button', { type: 'button', class: 'lnk', style: 'margin-left:6px', title: 'Select everything in this view', onclick: () => selectRange(0, G.data.count - 1, true) }, `Select all ${nf.format(G.data.count)}`) : null,
    h('button', { type: 'button', class: 'lnk', style: 'margin-left:6px', title: 'Deselect everything', onclick: () => clearSelection() }, 'Deselect all'),
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
    btn('download', 'Download', () => downloadDialog(photosWith({ ids: ids().join(',') }), G.sel.size)),
    S.CAN_MANAGE ? btn('text', 'Rename by date', () => renameByDate(ids())) : null,
    S.EDITING && G.sel.size > 1 ? btn('dup', 'Copy metadata', () => copyMetadata(ids())) : null,
    S.CAN_MANAGE ? btn('folder', 'Move to a folder', () => movePhotos(ids())) : null,
    S.CAN_MANAGE && S.LOSSLESS ? btn('rotate', 'Rotate or flip', (e) => editMenu(ids(), e.currentTarget, false)) : null,
    S.CAN_MANAGE ? btn('trash', 'Move to trash (Delete key)', () => trashPhotos(ids())) : null);
}
// ---- the edit log: entries for one photo (info panel) or the whole library (#/history)
export const HIST_ACTION = { edit: 'Edit', face: 'Face', tag: 'Tag', deleted: 'Deleted', trashed: 'Trashed', moved: 'Moved', restore: 'Restore', sync: 'Sync', database: 'digiKam', revert: 'Undo', added: 'Added', guard: 'Protected', lossless: 'File' };
export const HIST_RESTORE = { undo: ['Undo', 'Put back the values from before this change'], trash: ['Restore from trash', 'Put the photo back where it was'], sidecar: ['Restore sidecar', 'Put the sidecar back as it was just before this change'],
  original: ['Restore original file', 'Put the image file back as it was before the first write into it'], deleted: ['Restore photo', 'Put the files back and add the photo to the library again'] };
export function histEntry(e, onDone, withPhoto) {
  const btns = S.CAN_MANAGE ? e.restore.map((k) => h('button', { type: 'button', class: 'lnk', title: HIST_RESTORE[k][1], onclick: async () => {
    try { const r = await post(`/api/history/${e.id}/restore`, { kind: k }); toast(k === 'deleted' ? 'Photo restored.' : 'Restored.'); details.delete(e.photo); if (r && r.id) details.delete(r.id); } catch (err) { toastError(err); return; }
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
  const photo = params.get('photo'), more = h('button', { class: 'pillbtn my14', hidden: true }, 'Show more');
  let size = store.get('hsize', 'list');
  const list = h('div', { class: 'hlist qitems ' + size });
  let action = params.get('action') || '', offset = 0, timer = 0, token = 0;  // token: a slow earlier search must not land after a newer one
  const sel = h('select', { onchange: (e) => { action = e.target.value; reload(); } }, h('option', { value: '' }, 'Every change'), ...Object.entries(HIST_ACTION).map(([k, l]) => h('option', { value: k, selected: k === action }, l)));
  const q = h('input', { class: 'einput', placeholder: 'Search: file name, words in the change…', style: 'min-width:220px;flex:1', oninput: () => { clearTimeout(timer); timer = setTimeout(reload, 350); } });
  const people = (S.META?.tags || []).filter((t) => t.person && t.nAll).sort((a, b) => a.name.localeCompare(b.name));
  const plist = h('datalist', { id: 'hpeople' }, people.map((t) => h('option', { value: t.name })));
  const person = h('input', { class: 'einput', list: 'hpeople', placeholder: 'Person', style: 'width:170px', onchange: () => reload(), oninput: () => { if (!person.value) reload(); } });
  const who = h('select', { onchange: () => reload() }, h('option', { value: '' }, 'Anyone'));
  const from = h('input', { type: 'date', class: 'einput', style: 'width:150px', title: 'From', onchange: () => reload() });
  const to = h('input', { type: 'date', class: 'einput', style: 'width:150px', title: 'Until', onchange: () => reload() });
  const sizes = h('div', { class: 'eseg', title: 'Thumbnail size' }, [['list', 'List'], ['large', 'Large'], ['huge', 'Larger']].map(([k, l]) => h('button', { class: k === size ? 'on' : '', onclick: (e) => {
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
    const mine = ++token;
    let r; try { r = await api('/api/history?' + query().toString()); } catch (e) { if (mine === token) fill(list, errorBox(e)); return; }
    if (mine !== token) return;
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
    h('p', { class: 'muted', style: 'margin:0 0 10px' }, `Every change made in ${S.APP_NAME}, kept for good: who, when, and the values before and after. Undo puts the earlier values back; the restore links use the backups (sidecar copies, original files, the trash).`),
    h('div', { class: 'ctools hfilters' }, q, photo ? null : [person, plist], sel, who, h('span', { class: 'muted' }, 'from'), from, h('span', { class: 'muted' }, 'to'), to, sizes, count), list, more));
  load();
}
/** Delete photos for good, after a confirmation: files, sidecars, the database rows, everything the viewer knows.
 *  The server copies the files and sidecars to data/deleted-photos and backs digiKam's database up first. */
/** Move photos to the trash (digiKam's .dtrash, so digiKam's trash shows them too), asked first (an app dialog).
 *  The photos leave the screen at once and the server takes them out of every page before it answers; the files
 *  and digiKam's rows follow in the background. Undo waits for that, then puts them back; a photo that could not
 *  be moved comes back with a message. The library's catching up later redraws nothing in view. */
export async function trashPhotos(ids, after) {
  const what = ids.length === 1 ? 'this photo' : `${plural(ids.length, 'photo')}`;
  if (!await confirmDialog(`Move ${what} to the trash? ${ids.length === 1 ? 'It' : 'They'} can be put back from Folders › Trash.`, { ok: 'Move to trash', danger: true })) return;
  if (S.route.view === 'photos') dropPhotos(ids);
  ids.forEach((i) => { G.sel.delete(i); details.delete(i); });
  updateSelBar();
  after?.();
  const comeBack = () => { S.keepView = true; S.viewKey = null; onRoute(); };
  let job;
  try { job = (await post('/api/photos/trash', { ids, background: true })).job; } catch (e) { comeBack(); toastError(e, 10000); return; }
  S.quietUntil = Date.now() + 60000;  // the index catches up in the background: the page is not drawn again for it
  const result = (async () => {
    for (let i = 0; i < 40; i++) {
      const r = await api(`/api/photos/trash/${job}?wait=25`);
      if (r.done) return r;
    }
    throw new Error('The move to the trash is taking long; see Folders › Trash.');
  })();
  toast(`${ids.length === 1 ? 'Photo' : plural(ids.length, 'photo')} moved to the trash.`, 'Undo', async () => {
    try { const r = await result; if (!r.infos?.length) { toast('Nothing was moved, so there is nothing to put back.'); return; } await post('/api/trash/restore', { infos: r.infos }); toast('Restored.'); } catch (e) { toastError(e); return; }
    S.keepView = true; S.viewKey = null; pollSoon(600);
  }, 9000);
  result.then((r) => {
    if (!r.failed?.length) return;
    toast(`${plural(r.failed.length, 'photo')} could not be moved to the trash: ${r.failed[0].error}`, null, null, 10000);
    comeBack();
  }).catch((e) => toastError(e));
}
/** Pick a folder of the library; onPick(folder) with {id, path}. */
export async function folderPicker(title, onPick, exclude) {
  let d; try { d = await api('/api/folders'); } catch (e) { toastError(e); return; }
  const all = d.folders.map((f) => ({ ...f, _n: norm(f.path) })).filter((f) => !exclude || !exclude(f)).sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }));
  const inp = h('input', { class: 'pagefilter', style: 'width:100%', placeholder: 'Type part of a folder name…' });
  const list = h('div', { class: 'alist', style: 'max-height:45vh;margin-top:8px' });
  const draw = () => { const f = norm(inp.value); fill(list, all.filter((x) => !f || x._n.includes(f)).slice(0, 200).map((x) => h('div', { class: 'frow', onclick: () => { back.remove(); onPick(x); } }, icon('folder'), h('span', { class: 'nm' }, x.path), x.nAll ? h('span', { class: 'ct' }, nf.format(x.nAll)) : null))); };
  inp.oninput = draw; inp.onkeydown = (e) => e.stopPropagation();
  const back = modal(title, inp, list, h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel')));
  draw(); inp.focus();
}
export function movePhotos(ids, after) {
  folderPicker(`Move ${plural(ids.length, 'photo')} to…`, async (f) => {
    let r;
    try { r = await post('/api/photos/move', { ids, album: f.id }); } catch (e) { toastError(e, 10000); return; }
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
  const eg = h('div', { class: 'muted small' }, 'For example: ' + ex.date);
  const go_ = h('button', { class: 'pillbtn on', onclick: async () => {
    go_.disabled = true; fill(go_, 'Renaming…');
    let r;
    try { r = await post('/api/photos/rename-pattern', { ids, pattern: pat.value }); } catch (e) { toastError(e); back.remove(); return; }
    back.remove();
    for (const x of r.renamed) details.delete(x.id);
    S.keepView = true; pollSoon(600);
    const undo = async () => { let n = 0; for (const x of r.renamed) { try { await post('/api/photos/rename', { id: x.id, name: x.was }); n++; } catch { /* reported below */ } } toast(`${plural(n, 'name')} put back.`); S.keepView = true; pollSoon(600); };
    toast(`${plural(r.renamed.length, 'file')} renamed${r.unchanged ? `, ${nf.format(r.unchanged)} already named so` : ''}${r.failed.length ? `; ${nf.format(r.failed.length)} not: ${r.failed[0].error}` : ''}.`, r.renamed.length ? 'Undo' : null, undo, 12000);
  } }, 'Rename');
  const back = modal(`Rename ${plural(ids.length, 'file')} by date`, h('div', { class: 'stack',}, pat, eg,
    h('p', { class: 'muted', style: 'font-size:12.5px;margin:0' }, 'The files are renamed in their folders, with their sidecars. Extensions stay. A name already taken gets -2, -3. digiKam sees the same files under their new names.')),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel'), go_));
}
export async function renamePhoto(d) {
  const n = await promptDialog('New file name (the extension stays)', d.name);
  if (!n || n.trim() === d.name) return;
  try { const r = await post('/api/photos/rename', { id: d.id, name: n.trim() }); if (r.failed.length) throw new Error(r.failed[0].error); toast(`Renamed to ${r.moved[0]?.name || n.trim()}.`); } catch (e) { toastError(e); return; }
  details.delete(d.id); pollSoon(600);
  if (LB.open && LB.src.id[LB.i] === d.id) setTimeout(() => { LB.detail = null; showItem(); }, 1500);
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
  const back = h('div', { class: 'modal-back', onclick: (e) => { if (e.target === back) back.remove(); } },
    h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': typeof title === 'string' ? title : null, tabindex: '-1' }, h('h3', null, title), ...kids));
  // Escape closes it whatever has focus (fields stop keydown from bubbling, so this listens on the way down);
  // the one that must be answered (sign-in required) stays
  back.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !back.classList.contains('required')) { e.stopPropagation(); e.preventDefault(); back.remove(); } }, true);
  const before = document.activeElement;
  const remove = back.remove.bind(back);
  back.remove = () => { remove(); if (before && before.isConnected && typeof before.focus === 'function') before.focus(); };
  document.body.append(back);
  // focus goes into the dialog: its first field, else its first button, else the dialog itself
  setTimeout(() => { if (!back.isConnected || back.contains(document.activeElement)) return; const f = back.querySelector('input:not([type=checkbox]):not([type=hidden]), textarea, select') || back.querySelector('.modal-btns button, button'); (f || back.firstChild).focus(); }, 0);
  return back;
}
/** A yes/no question in the app's own dialog (never the browser's). Resolves true for the main button.
 *  opts: ok (the main button's label), danger (a red main button, focus starts on Cancel), cancel (its label), title. */
export function confirmDialog(message, opts = {}) {
  return new Promise((resolve) => {
    const paras = String(message).split(/\n\n+/).map((t) => h('p', null, t));
    let settled = false;
    const finish = (v) => { if (settled) return; settled = true; resolve(v); };
    const ok = h('button', { class: 'pillbtn ' + (opts.danger ? 'danger' : 'on'), onclick: () => { finish(true); back.remove(); } }, opts.ok || 'OK');
    const cancel = h('button', { class: 'pillbtn', onclick: () => { finish(false); back.remove(); } }, opts.cancel || 'Cancel');
    const back = modal(opts.title || (paras.length > 1 ? paras.shift().textContent : 'Confirm'), ...paras, h('div', { class: 'modal-btns' }, cancel, ok));
    const remove = back.remove; back.remove = () => { remove(); finish(false); };  // closed any other way (Escape, the backdrop): no
    back.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !opts.danger && e.target !== cancel) { e.preventDefault(); ok.click(); } });  // Enter on Cancel cancels
    setTimeout(() => (opts.danger ? cancel : ok).focus(), 0);
  });
}
/** A one-line text entry in the app's own dialog. Resolves the text (trimmed), or null when cancelled.
 *  opts: value, placeholder, type ('password' for one), ok (the main button's label), title. */
export function promptDialog(message, opts = {}) {
  return new Promise((resolve) => {
    const input = h('input', { class: 'einput', type: opts.type || 'text', value: opts.value || '', placeholder: opts.placeholder || '', autocomplete: opts.type === 'password' ? 'new-password' : 'off' });
    let settled = false;
    const finish = (v) => { if (settled) return; settled = true; resolve(v); };
    const done = () => { finish(input.value.trim()); back.remove(); };
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); done(); } };
    const back = modal(opts.title || message, opts.title ? h('p', null, message) : null, input,
      h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => { finish(null); back.remove(); } }, 'Cancel'), h('button', { class: 'pillbtn on', onclick: done }, opts.ok || 'OK')));
    const remove = back.remove; back.remove = () => { remove(); finish(null); };  // closed any other way: cancelled
    setTimeout(() => { input.focus(); if (opts.value) input.select(); }, 0);
  });
}
/** Maintenance › Metadata quality: photos without a location, and faces stored twice, the same person in two places, boxes outside the photo or empty.
 *  The check runs every time the page is opened, and again with "Check again". */
let qualityTimer = 0;  // the one pending redraw of the quality page; a new one replaces it
export async function showQuality() {
  clearTimeout(qualityTimer);
  document.body.classList.remove('cbulk-open');  // the ticked photos' bar of a report drawn before
  const body = h('div', null, h('div', { class: 'muted' }, 'Checking every face in the library…'));
  // redraw the page a moment later, unless the user has gone elsewhere or the page was drawn again meanwhile
  const refreshQuality = (ms) => { clearTimeout(qualityTimer); qualityTimer = setTimeout(() => { if (S.route.view === 'quality' && body.isConnected) showQuality(); }, ms); };
  main.replaceChildren(h('div', { class: 'page spage cpage' }, crumbs([['Maintenance', '#/maintenance'], ['Metadata quality', null]]),
    pageTitle('face', 'amber', 'Metadata quality'),
    h('p', { class: 'muted intro mt0' }, 'Each check is its own report. Every face region in digiKam’s database is checked: the same person stored twice a few pixels apart (shown twice in the people list), the same person in two separate places, and boxes that stick out of the photo or are empty. Fixes go through the normal face changes, so each one is in the edit log with undo.'),
    body));
  let q;
  try { q = await api('/api/quality'); } catch (e) { fill(body, errorBox(e)); return; }
  if (S.route.view !== 'quality') return;
  const p = q.progress || {};
  const again = h('button', { class: 'pillbtn', onclick: () => showQuality() }, icon('spark'), 'Check again');
  // the photos of a list open in the viewer as a set (step through them with the arrows), faces shown
  const thumb = (x, all) => h('img', { src: `/thumb/${x.photo}`, alt: '', loading: 'lazy', title: `Open in ${S.APP_NAME}`, onclick: () => {
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
  const goneMany = (key, ids) => {
    const box = body.querySelector(`.qsec[data-check="${key}"]`);
    let n = 0;
    ids.forEach((id) => { const row = box?.querySelector(`.hrow[data-photo="${id}"]`); if (row) { n++; row.classList.add('qgone'); setTimeout(() => row.remove(), 250); } });
    const c = box?.querySelector('h2 .muted');
    if (c && n) c.textContent = nf.format(Math.max(0, (parseInt(c.textContent.replace(/\D/g, ''), 10) || n) - n));
  };
  // a list: its first entries, and "Load the next ..." appending more in place (the page is not redrawn)
  const sec = (title, n, intro, acts, pager) => {
    const all = pager?.list || [];  // the list itself grows, so the buttons that act on all shown see the new ones
    const box = h('div', { class: 'hlist qitems ' + size }, all.map((x) => pager.row(x, all)));
    const more = h('button', { class: 'pillbtn mt10' });
    const label = () => { const left = n - all.length; more.hidden = !pager || left <= 0; fill(more, `Load the next ${nf.format(Math.min(PAGE, left))}`, h('span', { class: 'muted' }, ` · ${nf.format(left)} more`)); };
    more.onclick = async () => {
      more.disabled = true;
      try {
        const r = await api(`/api/quality/items?check=${pager.key}&offset=${all.length}&limit=${PAGE}`);
        const fresh = r.items.filter((x) => !all.some((y) => y.photo === x.photo && (y.name || '') === (x.name || '') && JSON.stringify(y.rect || y.rects || '') === JSON.stringify(x.rect || x.rects || '')));
        all.push(...fresh);
        box.append(...fresh.map((x) => pager.row(x, all)));
        if (!fresh.length) n = all.length;
      } catch (e) { toastError(e); }
      more.disabled = false; label();
    };
    label();
    return h('div', { class: 'qsec', 'data-check': pager?.key },
      h('h2', { class: 'sec' }, title, h('span', { class: 'muted' }, nf.format(n))),
      intro ? h('p', { class: 'muted', style: 'margin:0 0 10px;max-width:820px' }, intro) : null,
      acts ? h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px' }, acts) : null,
      box, more);
  };
  const PAGE = 120;
  const busy = p.running ? h('div', { class: 'enote' }, p.files ? `Fixing the image files… ${nf.format(p.done)} / ${nf.format(p.total)}.` : `Removing duplicate faces… ${nf.format(p.done)} / ${nf.format(p.total)} photos.`) : null;
  if (p.running) refreshQuality(4000);
  const near = q.near;
  const fitOne = async (x, ev) => { try { const r = await post('/api/quality/fit', { photos: [x.photo] }); toast(r.fitted ? 'Box trimmed to the photo.' : r.removed ? 'Empty box removed.' : 'Nothing to fix there.'); } catch (e) { toastError(e); return; } if (!gone(ev)) refreshQuality(1200); };
  const fitAll = q.invalid.count && S.CAN_MANAGE ? h('button', { class: 'pillbtn on', onclick: async () => {
    if (!await confirmDialog(`Trim ${plural(q.invalid.count, 'face box', 'face boxes')} to the edges of their photos (empty ones are removed)? Each change is in the edit log with undo.`)) return;
    try { const r = await post('/api/quality/fit', {}); toast(`${nf.format(r.fitted)} trimmed, ${nf.format(r.removed)} removed${r.failed ? `, ${nf.format(r.failed)} failed` : ''}.`, null, null, 7000); } catch (e) { toastError(e); return; }
    refreshQuality(1500);
  } }, 'Fit them all inside their photos') : null;
  const sizes = h('div', { class: 'eseg', title: 'Thumbnail size' }, [['list', 'List'], ['large', 'Large'], ['huge', 'Larger']].map(([k, l]) => h('button', { class: k === size ? 'on' : '', onclick: () => { store.set('qsize', k); showQuality(); } }, l)));
  const reports = [];  // [key, build]: each check is its own report, opened from the tab bar or the overview
  const report = (key, build) => reports.push([key, build]);
  report('near', () => sec('Stored twice', near.extra, near.extra ? `${plural(near.extra, 'region is a copy', 'regions are copies')} of another one of the same person a few pixels away, on ${plural(near.photos, 'photo')}.` : 'No face is stored twice.',
      near.extra && S.CAN_MANAGE && !p.running ? [h('button', { class: 'pillbtn on', onclick: async () => {
        if (!await confirmDialog(`Remove ${nf.format(near.extra)} duplicate face regions on ${plural(near.photos, 'photo')}? One of each pair is kept; the sidecars are rewritten.`)) return;
        try { const r = await post('/api/quality/fix', {}); toast(`Cleaning up ${plural(r.photos, 'photo')} in the background.`); } catch (e) { toastError(e); }
        refreshQuality(1500);
      } }, `Remove ${nf.format(near.extra)} duplicates`)]
        : p.finished && !p.files && !p.running && S.CAN_MANAGE ? [h('button', { class: 'pillbtn', onclick: async () => {
          if (!await confirmDialog('Rewrite the face regions inside the image files of the cleaned photos that still carry the duplicates? Each file is backed up first and its pixels are checked unchanged.')) return;
          try { const r = await post('/api/quality/fix', { files: true }); toast(`Looking at ${plural(r.photos, 'image file')} in the background.`); } catch (e) { toastError(e); }
          refreshQuality(1500);
        } }, 'Fix the image files too')] : null,
      { list: near.examples, key: 'near', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ` · ${x.apart} px apart`)) }));
  report('separate', () => sec('Same person in two places', q.separate.count, 'Usually one of the two is someone else, named by mistake. Open the photo, show the faces (F), and rename, adjust or remove the wrong box.', null,
      { list: q.separate.items, key: 'separate', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ` · ${x.rects.length} boxes`)) }));
  report('unnamed', () => {  // photos whose faces are all unnamed: name them, ignore them, or accept them as they are
      const u = q.unnamed, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'unnamed', on }); toast(on ? `${plural(ids.length, 'photo')} accepted as they are.` : 'Shown again.'); } catch (e) { toastError(e); return; } if (!gone(ev)) showQuality(); };
      const ignore = async (ids, ev) => {
        if (!ev && !await confirmDialog(`Mark the unnamed faces on ${plural(ids.length, 'photo')} as not faces to name? Each change is in the edit log with undo.`)) return;
        try { const r = await post('/api/quality/ignore', { photos: ids }); toast(`${plural(r.ignored, 'face')} ignored${r.failed ? `, ${nf.format(r.failed)} failed` : ''}.`); } catch (e) { toastError(e); return; }
        if (!gone(ev)) refreshQuality(1200);
      };
      const nameThem = (x) => { openLightbox({ id: [x.photo] }, 0, true); setTimeout(() => { if (!LB.faces) toggleFaces(); }, 700); };
      return sec('Faces without a name', u.count,
        `Photos with faces where none is named.${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted as they are ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Name the faces, mark them as not faces to name, or accept the photo as it is: it leaves this list until its faces change.`,
        items.length ? [h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown'),
          S.CAN_MANAGE ? h('button', { class: 'pillbtn', onclick: () => ignore(items.map((x) => x.photo)) }, 'Ignore their faces') : null,
          u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=unnamed'); if (acc.photos.length) accept(acc.photos, false); } }, 'Show the accepted ones again') : null]
          : u.accepted ? [h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=unnamed'); if (acc.photos.length) accept(acc.photos, false); } }, 'Show the accepted ones again')] : null,
        { list: items, key: 'unnamed', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, plural(x.faces, 'unnamed face')), ' · ',
          h('button', { type: 'button', class: 'lnk', onclick: () => nameThem(x) }, 'Name them'), ' · ',
          S.CAN_MANAGE ? [h('button', { type: 'button', class: 'lnk', onclick: (ev) => ignore([x.photo], ev) }, 'Ignore'), ' · '] : null,
          h('button', { type: 'button', class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is'))) });
  });
  report('untagged', () => {  // people in the photo (object detection), nobody marked or tagged
      const u = q.untagged || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'untagged', on }); toast(on ? `${plural(ids.length, 'photo')} accepted as they are.` : 'Shown again.'); } catch (e) { toastError(e); return; } if (!gone(ev)) showQuality(); };
      const find = async (ids, ev) => {
        let r;
        try { r = await post('/api/faces/detect', { ids }); toast(r.done ? `${plural(r.added || 0, 'face')} found${r.added ? '; name them from the photo, or under Faces without a name after Check again' : ''}.` : `Looking for faces in ${plural(r.queued, 'photo')} in the background.`, null, null, 7000); } catch (e) { toastError(e); return; }
        if (ev && r.done && r.added) gone(ev); else if (!ev) refreshQuality(2500);
      };
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=untagged'); if (acc.photos.length) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      return sec('People, but nobody tagged', u.count,
        `Photos where object detection sees people but no face is marked and no person is tagged (crowds, people from behind, strangers…).${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Find their faces to name them, or accept the photo as it is: it leaves this list unless it changes.`,
        [items.length ? h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown') : null,
          items.length && S.FACE_DETECT && S.CAN_MANAGE ? h('button', { class: 'pillbtn', onclick: () => find(items.map((x) => x.photo)) }, 'Find faces in all shown') : null, again].filter(Boolean),
        { list: items, key: 'untagged', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, plural(x.people, 'person', 'people')), ' detected · ',
          S.FACE_DETECT && S.CAN_MANAGE ? [h('button', { type: 'button', class: 'lnk', onclick: (ev) => find([x.photo], ev) }, 'Find faces'), ' · '] : null,
          h('button', { type: 'button', class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is'))) });
  });
  report('nolocation', () => {  // no location: set one, or accept the photo as it is
      const u = q.nolocation || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'nolocation', on }); toast(on ? `${plural(ids.length, 'photo')} accepted without a location.` : 'Shown again.'); } catch (e) { toastError(e); return; } if (!gone(ev)) showQuality(); };
      // the rows fold away where they are; the page is never drawn again under the user (they would lose their place)
      const place = (ids, ev) => locationDialog(ids, null, () => { if (!(ev && gone(ev))) goneMany('nolocation', ids); });
      // tick the photos of one place (a trip, a party) and act on them together from a bar fixed at the bottom of the
      // page, as the consistency page does: no scrolling back up to the buttons
      const sel = new Set();
      const bar = h('div', { class: 'cbulk', id: 'qbulk', hidden: true });
      const tickBtn = h('button', { class: 'pillbtn', onclick: () => tickAll(sel.size < items.length) }, 'Tick all shown');
      const acceptTicked = async () => {
        const ids = [...sel];
        try { await post('/api/quality/accept', { photos: ids, check: 'nolocation', on: true }); toast(`${plural(ids.length, 'photo')} accepted without a location.`); } catch (e) { toastError(e); return; }
        goneMany('nolocation', ids); tickAll(false);
      };
      const relabel = () => {
        tickBtn.textContent = sel.size && sel.size >= items.length ? 'Untick all' : 'Tick all shown';
        bar.hidden = !sel.size;
        document.body.classList.toggle('cbulk-open', sel.size > 0);
        fill(bar, h('div', { class: 'cbrow' }, h('span', { class: 'n' }, `${plural(sel.size, 'photo')} ticked`),
          h('button', { class: 'pillbtn on', onclick: () => { const ids = [...sel]; locationDialog(ids, null, () => { goneMany('nolocation', ids); tickAll(false); }); } }, icon('place'), 'Set their location…'),
          h('button', { class: 'pillbtn', onclick: acceptTicked }, 'Accept them as they are'),
          h('span', { style: 'flex:1' }),
          h('button', { class: 'pillbtn', onclick: () => tickAll(false) }, 'Untick all')));
      };
      const pick = (x) => h('label', { class: 'chk qpick', title: 'Tick several photos to set their location at once' }, h('input', { type: 'checkbox', 'aria-label': `Tick ${x.name}`, checked: sel.has(x.photo), onchange: (e) => { if (e.target.checked) sel.add(x.photo); else sel.delete(x.photo); relabel(); } }));
      const tickAll = (on) => { items.forEach((x) => (on ? sel.add(x.photo) : sel.delete(x.photo))); body.querySelectorAll('.qsec[data-check="nolocation"] .qpick input').forEach((i) => { i.checked = sel.has(+i.closest('.hrow').dataset.photo); }); relabel(); };
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=nolocation'); if (acc.photos.length) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      const el = sec('No location', u.count,
        `Photos and videos with no place recorded.${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted without one ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Set the location (the dialog suggests the places of photos taken around the same time), place them on the map (Map › Place photos), or accept the photo as it is: it leaves this list for good.`,
        [items.length && S.EDITING ? tickBtn : null,
          items.length && S.EDITING ? h('button', { class: 'pillbtn', onclick: () => place(items.map((x) => x.photo)) }, 'Set location for all shown') : null,
          items.length ? h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown') : null,
          h('a', { class: 'pillbtn', href: '#/map' }, 'Open the map'), again].filter(Boolean),
        { list: items, key: 'nolocation', row: (x, all) => h('div', { class: 'hrow', 'data-photo': x.photo }, S.EDITING ? pick(x) : null, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' · ', x.ts ? dfDay.format(date(x.ts)) : 'no date', ' · ',
          S.EDITING ? [h('button', { type: 'button', class: 'lnk', onclick: (ev) => place([x.photo], ev) }, 'Set location'), ' · '] : null,
          h('button', { type: 'button', class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is'))) });
      el.append(bar);
      return el;
  });
  report('baddate', () => {  // a date that cannot be right (1900, the future, 1970-01-01 00:00:00): set the right one, or accept it
      const u = q.baddate || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'baddate', on }); toast(on ? `${plural(ids.length, 'date')} accepted as ${ids.length === 1 ? 'it is' : 'they are'}.` : 'Shown again.'); } catch (e) { toastError(e); return; } if (!gone(ev)) showQuality(); };
      const fixDate = (x, ev) => { const back = modal(`Date of ${x.name}`, h('p', { class: 'muted mt0' }, `Now ${x.taken.replace('T', ' ')}: ${x.reason}.`),
        dateEditor([x.photo], x.taken.startsWith('1') && +x.taken.slice(0, 4) < 1901 ? null : x.taken, () => { back.remove(); if (!gone(ev)) refreshQuality(1200); }),
        h('div', { style: 'text-align:right;margin-top:10px' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close'))); };
      const useFolder = async (list, ev) => {
        let n = 0;
        for (const x of list) { if (x.folder_date && await editPhotos([x.photo], { taken: x.folder_date })) n++; }
        if (list.length > 1) toast(`${plural(n, 'photo')} dated from ${n === 1 ? 'its' : 'their'} folder.`);
        if (!gone(ev)) refreshQuality(1200);
      };
      const withFolder = items.filter((x) => x.folder_date);
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=baddate'); if (acc.photos.length) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      return sec('Implausible date', u.count,
        `Photos and videos dated before 1901, in the future, or exactly at a clock’s starting point (1 January 1970 at midnight and the like): a date the camera or a program wrote when it knew none.${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} A folder named for that year counts as meant (scans of old prints). Set the right date, take the folder’s date, or accept it as it is.`,
        [withFolder.length && S.EDITING ? h('button', { class: 'pillbtn', onclick: async () => { if (await confirmDialog(`Date ${plural(withFolder.length, 'photo')} from the folder ${withFolder.length === 1 ? 'it is' : 'they are'} in (its date, at noon)?`)) useFolder(withFolder); } }, `Take the folder’s date for ${nf.format(withFolder.length)}`) : null,
          items.length ? h('button', { class: 'pillbtn', onclick: () => accept(items.map((x) => x.photo)) }, 'Accept all shown') : null, again].filter(Boolean),
        { list: items, key: 'baddate', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' · ', x.taken.replace('T', ' '), ' · ', x.reason, ' · in ',
          h('a', { class: 'lnk', href: `#/photos?album=${x.folder_id}&sort=old` }, x.folder.replace(/^\//, '') || 'the library root'), h('div', { style: 'font-size:12.5px;margin-top:2px' },
          S.EDITING ? [h('button', { type: 'button', class: 'lnk', onclick: (ev) => fixDate(x, ev) }, 'Set the date'), ' · '] : null,
          S.EDITING && x.folder_date ? [h('button', { type: 'button', class: 'lnk', onclick: (ev) => useFolder([x], ev) }, `Use the folder’s date (${x.folder_date.slice(0, 10)})`), ' · '] : null,
          h('button', { type: 'button', class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is')))) });
  });
  report('wrongfolder', () => {  // the date is outside the date the folder is named for: fix the date, move the file, or accept it
      const u = q.wrongfolder || { count: 0, items: [], accepted: 0 }, items = u.items;
      const accept = async (ids, on = true, ev) => { try { await post('/api/quality/accept', { photos: ids, check: 'wrongfolder', on }); toast(on ? `${plural(ids.length, 'photo')} accepted where ${ids.length === 1 ? 'it is' : 'they are'}.` : 'Shown again.'); } catch (e) { toastError(e); return; } if (!gone(ev)) showQuality(); };
      const moveTo = async (list, ev) => {
        const by = new Map();
        for (const x of list) if (x.suggest) (by.get(x.suggest.id) || by.set(x.suggest.id, []).get(x.suggest.id)).push(x.photo);
        let n = 0, failed = 0;
        for (const [album, ids] of by) { try { const r = await post('/api/photos/move', { ids, album }); n += r.moved.length; failed += r.failed.length; } catch (e) { toastError(e); return; } }
        ids_(list).forEach((i) => details.delete(i));
        toast(`${plural(n, 'photo')} moved to the folder for ${n === 1 ? 'its' : 'their'} date${failed ? `; ${nf.format(failed)} could not be` : ''}.`, null, null, 7000);
        S.keepView = true; pollSoon(600);
        if (!gone(ev)) refreshQuality(1500);
      };
      const ids_ = (list) => list.map((x) => x.photo);
      const fixDate = (x, ev) => { const back = modal(`Date of ${x.name}`, h('p', { class: 'muted mt0' }, `In “${x.folder.replace(/^\//, '')}”, a folder for ${x.expect}.`),
        dateEditor([x.photo], x.taken, () => { back.remove(); if (!gone(ev)) refreshQuality(1200); }), h('div', { style: 'text-align:right;margin-top:10px' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close'))); };
      const pick = (x, ev) => movePhotos([x.photo], () => { gone(ev); });
      const again = u.accepted ? h('button', { class: 'pillbtn', onclick: async () => { const acc = await api('/api/quality/accepted?check=wrongfolder'); if (acc.photos.length) accept(acc.photos, false); } }, 'Show the accepted ones again') : null;
      return sec('Date doesn’t match the folder', u.count,
        `Photos and videos whose date is outside the date their folder is named for, like a photo from 2005 in 2020/202001: either the date in the file is wrong, or the file is in the wrong folder. A folder named for a day also holds the month after it (a trip or an event).${u.accepted ? ` ${plural(u.accepted, 'photo')} you accepted ${u.accepted === 1 ? 'is' : 'are'} not listed.` : ''} Fix the date, move the file, or accept it where it is: it comes back only if its date or folder changes.`,
        [items.length ? h('button', { class: 'pillbtn', onclick: () => accept(ids_(items)) }, 'Accept all shown') : null, again].filter(Boolean),
        { list: items, key: 'wrongfolder', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' · taken ', x.ts ? dfDay.format(date(x.ts)) : x.taken.slice(0, 10), ' · in ',
          h('a', { class: 'lnk', href: `#/photos?album=${x.folder_id}&sort=old` }, x.folder.replace(/^\//, '')), h('div', { style: 'font-size:12.5px;margin-top:2px' },
          S.EDITING ? [h('button', { type: 'button', class: 'lnk', onclick: (ev) => fixDate(x, ev) }, 'Fix the date'), ' · '] : null,
          S.CAN_MANAGE ? [x.suggest ? h('button', { type: 'button', class: 'lnk', title: 'Move the file (with its sidecar) to the folder for its date', onclick: (ev) => moveTo([x], ev) }, `Move to ${x.suggest.path.replace(/^\//, '')}`) : h('button', { type: 'button', class: 'lnk', onclick: (ev) => pick(x, ev) }, 'Move to a folder…'), ' · '] : null,
          h('button', { type: 'button', class: 'lnk', onclick: (ev) => accept([x.photo], true, ev) }, 'Accept as it is')))) });
  });
  report('invalid', () => sec('Outside the photo or empty', q.invalid.count, `A box that sticks out of the photo can be trimmed to its edges here, or moved and resized in ${S.APP_NAME} (click the face, “Adjust the box”). Empty boxes are removed.`, fitAll ? [fitAll] : null,
      { list: q.invalid.items, key: 'invalid', row: (x, all) => h('div', { class: 'hrow' }, thumb(x, all), h('div', { class: 'hmain' }, h('b', null, x.name), ' ',
        x.rect ? `${x.rect[2]} × ${x.rect[3]} at ${x.rect[0]}, ${x.rect[1]} on a ${x.size[0]} × ${x.size[1]} photo` : 'unreadable rectangle',
        S.CAN_MANAGE && x.fix ? [' · ', h('button', { type: 'button', class: 'lnk', onclick: (ev) => fitOne(x, ev) }, x.fix === 'fit' ? 'Fit inside' : 'Remove')] : null)) }));
  const COUNTS = { near: near.extra, separate: q.separate.count, unnamed: q.unnamed.count, untagged: (q.untagged || {}).count || 0, nolocation: (q.nolocation || {}).count || 0, baddate: (q.baddate || {}).count || 0, wrongfolder: (q.wrongfolder || {}).count || 0, invalid: q.invalid.count };
  const TITLES = { near: 'Stored twice', separate: 'Same person in two places', unnamed: 'Faces without a name', untagged: 'People, but nobody tagged', nolocation: 'No location', baddate: 'Implausible date', wrongfolder: 'Date doesn’t match the folder', invalid: 'Outside the photo or empty' };
  const ABOUT = { near: 'The same person stored twice a few pixels apart', separate: 'The same person in two places on one photo', unnamed: 'Photos whose faces have no name', untagged: 'People detected, nobody marked or tagged', nolocation: 'Photos and videos with no place recorded', baddate: 'Dates before 1901, in the future, or at a clock’s starting point', wrongfolder: 'Dated outside the date the folder is named for', invalid: 'Face boxes that stick out of the photo, or are empty' };
  // one report at a time: the tab bar (with each report's count), or the overview with a row per check
  const built = new Map(), view = h('div'), bar = h('div', { class: 'stabs', role: 'tablist' });
  const overview = () => h('div', { class: 'qsec qoverview' }, reports.map(([k]) => h('div', { class: 'srow' }, h('div', null, h('b', null, TITLES[k]), h('div', { class: 'muted note' }, ABOUT[k])),
    h('button', { class: 'pillbtn' + (COUNTS[k] ? ' on' : ''), onclick: () => show(k) }, COUNTS[k] ? `Open · ${nf.format(COUNTS[k])}` : 'Nothing found'))));
  const show = (k) => {
    if (k !== 'overview' && !reports.some(([key]) => key === k)) k = 'overview';
    if (!built.has(k)) built.set(k, k === 'overview' ? overview() : reports.find(([key]) => key === k)[1]());
    fill(view, built.get(k));
    document.body.classList.toggle('cbulk-open', !!view.querySelector('.cbulk:not([hidden])'));
    [...bar.children].forEach((b) => { b.classList.toggle('on', b.dataset.tab === k); b.setAttribute('aria-selected', b.dataset.tab === k); });
    store.set('qcheck', k);
    if ((S.route.params.get('check') || 'overview') !== k) { if (k === 'overview') S.route.params.delete('check'); else S.route.params.set('check', k); history.replaceState(null, '', hashFor('quality', S.route.params)); }
  };
  bar.append(h('button', { class: 'pillbtn', role: 'tab', 'data-tab': 'overview', onclick: () => show('overview') }, 'Overview'),
    ...reports.map(([k]) => h('button', { class: 'pillbtn', role: 'tab', 'data-tab': k, onclick: () => show(k) }, TITLES[k], COUNTS[k] ? h('span', { class: 'muted' }, ` ${nf.format(COUNTS[k])}`) : null)));
  fill(body, h('div', { style: 'display:flex;gap:10px;align-items:center;margin:4px 0 6px;flex-wrap:wrap' }, again, h('span', { class: 'muted' }, `${nf.format(q.checked)} face regions checked ${fmtWhen(q.at)}.`), h('span', { style: 'flex:1' }), sizes), busy, bar, view);
  show(S.route.params.get('check') || store.get('qcheck', 'overview'));
}
/** Import: photos and videos from this device (or shared to the installed app) go into folders by date,
 *  duplicates are left out, each new file gets a sidecar. */
export async function showImport(params) {
  const body = h('div');
  main.replaceChildren(h('div', { class: 'page' }, crumbs([['Folders', '#/folders'], ['Import', null]]), pageTitle('add', 'teal', 'Import'),
    h('p', { class: 'muted intro mt0' }, 'Photos and videos from this device go into folders by the date they were taken, under the library root. Files already in the library (same size and contents) are left out. Each new file gets a sidecar with its own metadata, and digiKam reads the full metadata on its next run. On Android, the installed app also appears in the Share sheet; on an iPhone, choose from the photo library below.'),
    body));
  if (!S.CAN_MANAGE) { fill(body, h('div', { class: 'empty' }, 'Importing needs an editor or admin account.')); return; }
  let lay; try { lay = await api('/api/import/layouts'); } catch (e) { fill(body, errorBox(e)); return; }
  let token = params.get('staged') || null, plan = null;
  const layoutSel = h('select', { class: 'sharepick' }, lay.layouts.map((l) => h('option', { value: l.key, selected: l.key === lay.layout }, `${l.pattern}  ·  ${l.example}`)));
  const remember = S.IS_ADMIN ? h('label', { style: 'display:flex;gap:6px;align-items:center;font-size:12.5px;cursor:pointer' }, h('input', { type: 'checkbox', checked: true }), 'Remember as the default layout') : null;
  const dupSel = h('select', { class: 'sharepick' }, h('option', { value: 'skip', selected: true }, 'Leave duplicates out'), h('option', { value: 'import' }, 'Import duplicates too'));
  const status = h('div', { class: 'muted my8' });
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
    history.replaceState(null, '', `#/import?staged=${token}`); onRoute();  // a one-off state: Back does not return to it
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
        h('td', null, i.error ? h('span', { class: 'warn' }, i.error) : i.duplicate_of ? h('button', { type: 'button', class: 'lnk', onclick: () => openLightbox({ id: [i.duplicate_of] }, 0, true) }, 'already in the library') : h('span', { class: 'mst ok' }, 'new')))))));
    run.hidden = false;
    fill(run, icon('add'), plan.new ? `Import ${nf.format(dupSel.value === 'import' ? items.filter((i) => !i.error).length : plan.new)}` : 'Nothing new to import');
    run.disabled = !plan.new && dupSel.value !== 'import';
  };
  layoutSel.onchange = () => { plan = null; draw(); };
  dupSel.onchange = () => draw();
  run.onclick = async () => {
    run.disabled = true; fill(run, 'Importing…');
    let r;
    try { r = await post(`/api/import/${token}/run`, { layout: layoutSel.value, duplicates: dupSel.value, remember: !!remember?.firstChild.checked }); } catch (e) { toastError(e, 10000); run.disabled = false; draw(); return; }
    const folders = [...new Set(r.added.map((a) => a.folder))];
    toast(`${plural(r.added.length, 'photo')} imported${folders.length ? ` into ${folders.slice(0, 3).join(', ')}${folders.length > 3 ? '…' : ''}` : ''}${r.skipped.length ? `; ${plural(r.skipped.length, 'file')} left out` : ''}.`, r.added.length ? 'Show' : null, () => go('photos', photosWith({ sort: 'new' })), 12000);
    token = null; plan = null; history.replaceState(null, '', '#/import'); onRoute(); S.keepView = true; pollSoon(600);
    fill(status, r.added.length ? h('span', null, `${plural(r.added.length, 'photo')} imported. `, h('a', { class: 'lnk', href: '#/folders' }, 'Open Folders')) : 'Nothing was imported.'); fill(table); run.hidden = true;
  };
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { const files = [...e.dataTransfer.files]; if (files.length) send(files); });
  fill(body, drop,
    h('div', { style: 'display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:14px 0 4px' }, h('span', null, 'Folders: '), layoutSel, remember, h('span', { style: 'margin-left:8px' }, 'Duplicates: '), dupSel),
    status, table, h('div', { style: 'margin-top:12px;display:flex;gap:8px' }, run, token ? h('button', { class: 'pillbtn', onclick: async () => { if (!await confirmDialog('Discard the staged photos? They are removed from the server; nothing has reached the library yet.', { ok: 'Discard', danger: true })) return; await post(`/api/import/${token}/discard`, {}).catch(() => {}); token = null; plan = null; location.hash = '#/import'; draw(); } }, 'Discard') : null));
  if (params.get('error') === 'off') fill(status, h('span', { class: 'warn' }, 'Imports are turned off on this server.'));
  if (params.get('error') === 'signin') fill(status, h('span', { class: 'warn' }, 'Sign in as an editor, then share the photos again.'));
  draw();
}
/** The trash: what was moved there here or in digiKam (the same .dtrash folders), with restore and delete for good. */
export async function showTrash() {
  const body = h('div');
  main.replaceChildren(h('div', { class: 'page' }, crumbs([['Folders', '#/folders'], ['Trash', null]]), pageTitle('trash', 'slate', 'Trash'),
    h('p', { class: 'muted intro mt0' }, 'Photos moved to the trash, here or in digiKam, kept with their sidecars in each album root’s .dtrash folder until they are restored or deleted for good. Click a photo to select it.'), body));
  let d; try { d = await api('/api/trash'); } catch (e) { fill(body, errorBox(e)); return; }
  if (S.route.view !== 'trash') return;
  const sel = new Set();
  const act = async (path, payload, msg) => { try { const r = await post(path, payload); toast(msg(r), null, null, 8000); } catch (e) { toastError(e); return; } pollSoon(600); showTrash(); };
  const chosen = () => (sel.size ? d.items.filter((t) => sel.has(t.info)) : d.items);
  const bar = h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0 12px' });
  const drawBar = () => fill(bar,
    h('span', { class: 'muted' }, d.items.length ? `${plural(d.items.length, 'photo')} · ${fmtBytes(d.bytes)}${sel.size ? ` · ${nf.format(sel.size)} selected` : ''}` : 'The trash is empty.'),
    d.items.length ? h('button', { class: 'pillbtn', onclick: () => act('/api/trash/restore', { infos: chosen().map((t) => t.info) }, (r) => `${plural(r.restored.length, 'photo')} restored${r.failed.length ? `, ${r.failed.length} not (${r.failed[0].error})` : ''}.`) }, sel.size ? `Restore ${nf.format(sel.size)}` : 'Restore all') : null,
    d.items.length ? h('button', { class: 'pillbtn danger', onclick: async () => { const c = chosen(); if (!await confirmDialog(`Delete ${plural(c.length, 'photo')} for good? The files and sidecars are removed from the disk; this cannot be undone.`, { ok: 'Delete for good', danger: true })) return; act('/api/trash/empty', sel.size ? { infos: c.map((t) => t.info) } : { all: true }, (r) => `${plural(r.deleted, 'photo')} deleted for good.`); } }, sel.size ? `Delete ${nf.format(sel.size)} for good` : 'Empty the trash') : null,
    sel.size ? h('button', { type: 'button', class: 'lnk', onclick: () => { sel.clear(); drawBar(); draw(); } }, 'Select none') : null);
  const grid = h('div', { class: 'cards' });
  const draw = () => fill(grid, d.items.map((t) => h('div', { class: 'card' + (sel.has(t.info) ? ' sel' : ''), tabindex: 0, role: 'button', onclick: () => { sel.has(t.info) ? sel.delete(t.info) : sel.add(t.info); drawBar(); draw(); } },
    h('span', { class: 'cv' }, t.exists ? h('img', { src: `/api/trash/thumb?info=${encodeURIComponent(t.info)}`, alt: '', loading: 'lazy' }) : h('span', { class: 'muted' }, 'file missing'),
      sel.has(t.info) ? h('span', { class: 'privbadge' }, icon('check')) : null),
    h('div', { class: 'nm' }, t.name), h('div', { class: 'sb' }, `${t.when ? fmtWhen(Math.floor(new Date(t.when).getTime() / 1000)) : ''} · ${t.path.split('/').slice(-2, -1)[0] || ''}`))));
  drawBar(); draw();
  fill(body, bar, grid);
}
/** Maintenance › Server log: the end of data/viewer.log, filtered by level and text; page requests hidden unless asked. */
export function logDialog() {
  const level = h('select', null, h('option', { value: 'any' }, 'Everything'), h('option', { value: 'warning' }, 'Warnings and errors'), h('option', { value: 'error' }, 'Errors only'), h('option', { value: 'auth' }, 'Sign-ins'));
  const text = h('input', { class: 'einput', placeholder: 'Filter, e.g. sync or a file name', style: 'flex:1;min-width:140px', onkeydown: (e) => { e.stopPropagation(); if (e.key === 'Enter') load(); } });
  const reqs = h('input', { type: 'checkbox' });
  const pre = h('pre', { class: 'logview' }, 'Loading…');
  const info = h('div', { class: 'muted smaller' });
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
    h('div', { class: 'modal-btns' }, h('a', { class: 'pillbtn', href: '/api/log/download', download: '' }, 'Download the log'), h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close')));
  back.firstChild.classList.add('wide');
  load();
}
/** Shared (everyone sees it; editors and admins change it) or private (only you): a select when both are allowed. */
export function sharingPick(def) {
  if (ST.canShare && ST.canPrivate) return h('select', { class: 'sharepick', title: 'Who sees this album' },
    h('option', { value: '1', selected: def !== false }, 'Shared with everyone'), h('option', { value: '0', selected: def === false }, 'Private (only me)'));
  return h('span', { class: 'muted small', 'data-shared': ST.canShare ? '1' : '0' }, ST.canShare ? 'Shared with everyone' : 'Private (only you see it)');
}
export const pickedShared = (el) => (el.tagName === 'SELECT' ? el.value === '1' : el.dataset.shared === '1');
export function albumDialog(ids) {
  const name = h('input', { class: 'pagefilter', style: 'flex:1;min-width:160px', placeholder: 'New album name' });
  const share = sharingPick();
  let busy = false;  // Enter and the button together, or two quick Enters, must not make two albums
  const add = async (id, label) => {
    if (busy) return;
    busy = true;
    try {
      if (id == null) { if (!name.value.trim()) { name.focus(); busy = false; return; } await post('/api/ualbums', { name: name.value.trim(), ids, shared: pickedShared(share) }); label = name.value.trim(); }
      else await post(`/api/ualbums/${id}`, { ids, on: true });
    } catch (e) { toast(e.message || 'Could not update the album.'); busy = false; return; }
    await loadState();
    back.remove();
    toast(`Added ${nf.format(ids.length)} to “${label}”.`, 'Open', () => { const a = ST.albums.find((x) => x.name === label); if (a) go('photos', photosWith({ ualbum: a.id })); });
  };
  const editable = ST.albums.filter((a) => a.canEdit);
  const back = modal(`Add ${plural(ids.length, 'photo')} to an album`,
    h('div', { class: 'alist' }, editable.map((a) => h('div', { class: 'frow', onclick: () => add(a.id, a.name) }, icon(a.shared ? 'folder' : 'lock'), h('span', { class: 'nm' }, a.name), h('span', { class: 'ct' }, nf.format(a.n))))),
    ST.canShare || ST.canPrivate ? h('div', { style: 'display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center' }, name, share, h('button', { class: 'pillbtn on', onclick: () => add(null) }, icon('add'), 'Create')) : h('p', { class: 'muted' }, 'Sign in to make albums.'),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel')));
  name.onkeydown = (e) => { if (e.key === 'Enter') add(null); };
  if (!editable.length) name.focus();
}
