/** The library's management pages: metadata consistency, place tags, the Faces queue, duplicates and the tag tree (Settings and Maintenance are settings.js). */
import { $, PREF, TOAST_LONG, actBtn, api, date, dfDay, dfMonth, errorBox, fill, fmtBytes, fmtWhen, h, icon, main, nf, norm, plural, post, send, store } from './core.js';
import { S, faceUrl, filterParams, go, hashFor, onRoute, photosWith, pollSoon, toast, toastError } from './state.js';
import { crumbs } from './search.js';
import { confirmDialog, modal, promptDialog } from './actions.js';
import { pageTitle } from './pages.js';
import { details, openLightbox, syncLightbox } from './lightbox.js';
import { personPicker } from './faces.js';
import { badgeTiles, setBadges } from './grid.js';
import { awaitReindex, indexMeta } from './main.js';

const SRC_LABEL = { db: 'Database', sidecar: 'Sidecar', file: 'Image file' };
const FIELD_LABEL = { rating: 'Rating', title: 'Title', caption: 'Caption', tags: 'Tags', taken: 'Date', gps: 'Location' };
export async function showConsistency() {
  let field = '', offset = 0, selected = new Set(), d, wasRunning = false, pollT = 0;
  const head = h('div'), list = h('div'), more = h('button', { class: 'pillbtn', style: 'margin:16px 0', onclick: () => load(true) }, 'Show more');
  const fmtv = (f, v, na) => na ? h('i', { class: 'muted', title: 'This file type cannot hold metadata; the sidecar carries it' }, 'n/a') : v == null ? h('i', { class: 'muted' }, 'absent') : f === 'tags' ? (v.length ? h('span', null, v.map((t) => h('span', { class: 'ichip', style: 'height:24px;font-size:12px;margin:1px' }, t.split('/').pop()))) : h('i', { class: 'muted' }, 'none'))
    : f === 'rating' ? (v ? '★'.repeat(v) : h('i', { class: 'muted' }, 'none')) : (v || h('i', { class: 'muted' }, 'empty'));
  const item = (r) => {
    const cb = h('input', { type: 'checkbox', checked: selected.has(r.id), onchange: (e) => { e.target.checked ? selected.add(r.id) : selected.delete(r.id); bar(); } });
    if (field === 'nosidecar') return h('div', { class: 'crec slim' + (selected.has(r.id) ? ' sel' : '') },
      h('div', { class: 'chead' }, S.EDITING ? cb : null, h('img', { src: `/thumb/${r.id}`, loading: 'lazy', alt: '', onclick: () => openLightbox({ id: [r.id] }, 0, true) }),
        h('div', { style: 'flex:1;min-width:0' }, h('b', null, r.name), h('div', { class: 'muted smaller' }, r.path.replace(/^.*\/photos\//, ''))),
        S.EDITING ? h('button', { class: 'pillbtn', style: 'height:30px', onclick: () => makeSidecars([r.id]) }, 'Create sidecar') : null));
    return h('div', { class: 'crec' + (selected.has(r.id) ? ' sel' : '') },
      h('div', { class: 'chead' }, S.EDITING ? cb : null, h('img', { src: `/thumb/${r.id}`, loading: 'lazy', alt: '', onclick: () => openLightbox({ id: [r.id] }, 0, true) }),
        h('div', null, h('b', null, r.name), h('div', { class: 'muted smaller' }, r.path.replace(/^.*\/photos\//, ''), !r.has_sidecar ? ' · no sidecar' : '', !r.file_xmp ? (r.file_editable ? ' · no metadata in the file' : ' · file type cannot hold metadata') : ''))),
      h('table', { class: 'ctable' }, h('tr', null, h('th'), ...['db', 'sidecar', 'file'].map((s) => h('th', null, SRC_LABEL[s]))),
        ...r.mismatch.map((f) => h('tr', null, h('td', null, FIELD_LABEL[f] || f), ...['db', 'sidecar', 'file'].map((s) => h('td', null, fmtv(f, r.fields[f][s], s === 'file' && !r.file_xmp && !r.file_editable)))))),
      S.EDITING ? h('div', { class: 'cfix' }, h('span', { class: 'muted' }, 'Fix this photo: take the values from the'),
        ...['db', 'sidecar', 'file'].filter((s) => r.mismatch.some((f) => r.fields[f][s] != null)).map((s) => h('button', { class: 'pillbtn', style: 'height:30px', onclick: () => fixOne(r, s) }, SRC_LABEL[s]))) : null);
  };
  // photos without a sidecar: write one from digiKam's database (rating, title, caption, tags), as digiKam would
  const makeSidecars = async (ids) => {
    if (ids.length > 1 && !await confirmDialog(`Create an XMP sidecar for ${nf.format(ids.length)} photos, with the rating, title, caption and tags from digiKam’s database? Nothing else changes.`)) return;
    toast(`Creating ${plural(ids.length, 'sidecar')}…`, null, null, 60000);
    let done = 0, skipped = [];
    for (let i = 0; i < ids.length; i += 200) {
      try { const r = await post('/api/consistency/sync', { ids: ids.slice(i, i + 200), source: 'db', targets: ['sidecar'], fields: Object.keys(FIELD_LABEL) }); done += r.synced; skipped = skipped.concat(r.skipped); }
      catch (e) { toastError(e); break; }
    }
    toast(`Created ${plural(done, 'sidecar')}${skipped.length ? `; ${nf.format(skipped.length)} skipped (${skipped[0].reason})` : ''}.`, null, null, 9000);
    selected.clear(); bar(); setTimeout(load, 2500);
  };
  // one photo: copy the chosen source's values to the other places that can hold them
  const fixOne = async (r, src) => {
    const targets = ['db', 'sidecar', 'file'].filter((t) => t !== src && (t !== 'file' || r.file_editable));
    if (!await confirmDialog(`Copy ${r.mismatch.map((f) => (FIELD_LABEL[f] || f).toLowerCase()).join(', ')} from the ${SRC_LABEL[src].toLowerCase()} to the ${targets.map((t) => SRC_LABEL[t].toLowerCase()).join(' and ')} for ${r.name}?` + (targets.includes('db') ? '\n\nThis writes into digiKam’s database.' : ''))) return;
    try { const res = await post('/api/consistency/sync', { ids: [r.id], source: src, targets, fields: r.mismatch }); toast(res.synced ? `Fixed ${r.name}.` : `Not fixed: ${res.skipped[0]?.reason || 'nothing to copy'}`, null, null, TOAST_LONG); } catch (e) { toastError(e); return; }
    load();
  };
  // the bulk bar: fixed at the bottom of the page, wraps on narrow screens. One click per source ("take the values
  // from"), applied to the other places that can hold them; the detailed choice (fields, targets) is one click away.
  const syncBar = h('div', { class: 'cbulk', id: 'cbulk', hidden: true });
  let advanced = false;
  const runSync = async (src, targets, fields) => {
    const n = selected.size;
    if (!targets.length) { toast('Pick at least one place to copy to, other than the source.'); return; }
    if (!await confirmDialog(`Copy ${fields.map((f) => (FIELD_LABEL[f] || f).toLowerCase()).join(', ')} from the ${SRC_LABEL[src].toLowerCase()} to the ${targets.map((t) => SRC_LABEL[t].toLowerCase()).join(' and ')} for ${nf.format(n)} ${n === 1 ? 'photo' : 'photos'}?` +
      (targets.includes('db') ? '\n\nThis writes into digiKam’s database.' : '') + (targets.includes('file') ? `\n\nImage files are rewritten in place (metadata only; pixels untouched); the original of each is kept in ${S.APP_NAME}’s file-backups folder. Files that cannot hold metadata are skipped.` : '') + '\n\nSidecars are backed up before they change.')) return;
    fill(syncBar, h('span', { class: 'n' }, `Syncing ${nf.format(n)} photos…`));
    try {
      const r = await post('/api/consistency/sync', { ids: [...selected], source: src, targets, fields });
      toast(`Synced ${nf.format(r.synced)} ${r.synced === 1 ? 'photo' : 'photos'}${r.skipped.length ? `; ${nf.format(r.skipped.length)} skipped (${r.skipped[0].reason})` : ''}.`, null, null, 9000);
    } catch (e) { toastError(e); bar(); return; }
    selected.clear(); bar(); load();
  };
  const bar = () => {
    syncBar.hidden = !selected.size;
    document.body.classList.toggle('cbulk-open', selected.size > 0);
    if (!selected.size) return;
    if (field === 'nosidecar') {
      fill(syncBar, h('div', { class: 'cbrow' }, h('button', { class: 'iconbtn', title: 'Clear the selection', onclick: () => { selected.clear(); bar(); load(); } }, icon('close')),
        h('span', { class: 'n' }, `${nf.format(selected.size)} selected`), h('button', { class: 'pillbtn on', onclick: () => makeSidecars([...selected]) }, 'Create sidecars from digiKam’s database')));
      return;
    }
    const quick = ['db', 'sidecar', 'file'].map((src) => h('button', { class: 'pillbtn' + (src === 'sidecar' ? ' on' : ''), title: `Make the other places match the ${SRC_LABEL[src].toLowerCase()}`, 'data-src': src,
      onclick: () => runSync(src, ['db', 'sidecar', 'file'].filter((t) => t !== src), Object.keys(FIELD_LABEL)) }, SRC_LABEL[src]));
    const src = h('select', null, ['db', 'sidecar', 'file'].map((x) => h('option', { value: x }, SRC_LABEL[x])));
    const tg = ['db', 'sidecar', 'file'].map((x) => h('label', null, h('input', { type: 'checkbox', value: x, checked: x !== 'db' }), SRC_LABEL[x]));
    const fl = Object.keys(FIELD_LABEL).map((f) => h('label', null, h('input', { type: 'checkbox', value: f, checked: true }), FIELD_LABEL[f]));
    fill(syncBar,
      h('div', { class: 'cbrow' },
        h('button', { class: 'iconbtn', title: 'Clear the selection', onclick: () => { selected.clear(); bar(); load(); } }, icon('close')),
        h('span', { class: 'n' }, `${nf.format(selected.size)} selected`),
        h('span', { class: 'muted' }, 'Take the values from:'), ...quick,
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'lnk', onclick: () => { advanced = !advanced; bar(); } }, advanced ? 'Fewer options' : 'Choose fields and places…')),
      advanced ? h('div', { class: 'cbrow' },
        h('span', null, 'Copy from ', src, ' to '), h('span', { class: 'cbchecks' }, tg), h('span', { class: 'muted' }, 'fields:'), h('span', { class: 'cbchecks' }, fl),
        h('button', { class: 'pillbtn on', onclick: () => runSync(src.value, tg.map((l) => l.firstChild).filter((c) => c.checked && c.value !== src.value).map((c) => c.value),
          fl.map((l) => l.firstChild).filter((c) => c.checked).map((c) => c.value)) }, icon('spark'), 'Sync')) : null);
  };
  const load = async (append) => {
    if (!append) offset = 0;
    try { d = await api(`/api/consistency?field=${field}&offset=${offset}&limit=60`); } catch (e) { list.replaceChildren(errorBox(e)); return; }
    if (S.route.view !== 'consistency' || !list.isConnected) return;
    const s = d.summary, p = d.progress;
    const tile = (hue, ic, n, label, f) => h(f == null ? 'div' : 'a', { class: 'stat' + (f != null && f === field ? ' on' : '') + (f != null ? ' click' : ''), style: `--hue:var(--c-${hue})`,
      onclick: f == null ? null : () => { field = f; selected.clear(); bar(); load(); } }, h('span', { class: 'sic' }, icon(ic)), h('div', null, h('b', null, nf.format(n)), h('span', null, label)));
    const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
    fill(head,
      p.running ? h('div', { class: 'enote ajob', style: 'border:0;padding:10px 12px' }, h('div', { class: 'ahead' }, h('b', null, p.rescan_all === false ? 'Checking new and changed photos…' : 'Checking the whole library…'), h('span', { class: 'sp' }), `${nf.format(p.done)} / ${nf.format(p.total)}`),
        h('div', { class: 'bar' }, h('i', { style: `width:${pct}%` })), h('div', { class: 'muted', style: 'font-size:12px;margin-top:4px' }, 'Each photo’s sidecar and file are read with exiftool; the list below fills in when it is done.'))
      : p.total === 0 && p.started ? h('div', { class: 'enote' }, 'Nothing to check: every photo was already compared since it last changed. Use “Re-check whole library” to compare them all again.') : null,
      !p.running && p.failed ? h('p', { class: 'warn' }, `${nf.format(p.failed)} photos could not be compared: ${p.error}`) : null,
      s.scanned_at ? h('div', { class: 'muted', style: 'font-size:12.5px;margin:6px 0' }, `Last checked ${fmtWhen(s.scanned_at)}.`) : null,
      s.scanned ? h('div', { class: 'stats cstats' },
        tile('teal', 'check', s.scanned, 'photos checked', null),
        tile('rose', 'dup', s.mismatched, s.mismatched === 1 ? 'photo disagrees' : 'photos disagree', ''),
        tile('amber', 'text', s.no_sidecar, 'without a sidecar', 'nosidecar'),
        ...Object.entries(s.by_field).filter(([, n]) => n).map(([f, n]) => tile('indigo', { rating: 'star', tags: 'tag', taken: 'calendar', gps: 'place' }[f] || 'edit', n, `differ in ${(FIELD_LABEL[f] || f).toLowerCase()}`, f))) : h('p', { class: 'muted' }, 'Nothing scanned yet.'),
      s.scanned ? h('h2', { class: 'sec' }, field === 'nosidecar' ? 'Photos without a sidecar' : field ? `Photos whose ${(FIELD_LABEL[field] || field).toLowerCase()} differs` : 'Photos that disagree',
        h('span', { class: 'muted' }, nf.format(d.total)),
        h('span', { class: 'sp' }),
        S.EDITING && d.total ? h('button', { type: 'button', class: 'lnk', style: 'font-size:13px;font-weight:400', onclick: async () => {  // every photo listed (on this filter), not only the ones shown
          try { const all = await api(`/api/consistency?field=${field}&offset=0&limit=5000`); all.items.forEach((r) => selected.add(r.id)); } catch (e) { toastError(e); return; }
          bar(); load();
        } }, `Select all ${nf.format(d.total)}`) : null) : null,
      field === 'nosidecar' && d.total ? h('p', { class: 'muted mt0' }, `These photos keep their metadata only in digiKam’s database (and the file, when it can hold it). A sidecar is not required, but digiKam and other programs read it, and ${S.APP_NAME} writes edits there. Creating one copies the rating, title, caption and tags from digiKam’s database.`) : null,
      p.error && !p.failed ? h('p', { class: 'warn' }, 'Last error: ' + p.error) : null);
    // a scan that just finished: say so once
    if (wasRunning && !p.running) toast(p.failed ? `Check finished with errors: ${nf.format(p.failed)} photos could not be compared.` : `Check finished: ${nf.format(s.scanned)} photos compared, ${nf.format(s.mismatched)} disagree.`, null, null, TOAST_LONG);
    wasRunning = p.running;
    if (!append) list.replaceChildren();
    if (!d.items.length && !offset) list.append(h('div', { class: 'empty' }, !s.scanned ? '' : field === 'nosidecar' ? 'Every photo has a sidecar.' : 'Everything that was checked agrees.'));
    list.append(...d.items.map(item));
    offset += d.items.length;
    more.hidden = offset >= d.total;
    clearTimeout(pollT);
    if (p.running) pollT = setTimeout(() => { if (S.route.view === 'consistency' && list.isConnected) load(); }, 3000);  // refresh the progress (and the list, from the top) until it is done
  };
  const scan = async (subset, rescanAll) => {
    const q = subset ? filterParams() : new URLSearchParams();
    q.set('rescan_all', rescanAll ? '1' : '0');
    try {
      const r = await send('/api/consistency/scan', q);
      toast(`Checking ${nf.format(r.queued)} photos in the background.`);
    } catch (e) { toast('Could not start the check: ' + e.message, null, null, TOAST_LONG); return; }
    wasRunning = true;
    load();
  };
  main.replaceChildren(h('div', { class: 'page spage cpage' }, crumbs([['Maintenance', '#/maintenance'], ['Metadata consistency', null]]),
    pageTitle('check', 'teal', 'Metadata consistency'),
    h('p', { class: 'muted' }, 'The rating, title, caption and tags of each photo are read from digiKam’s database, its XMP sidecar and the image file, and compared. Fix a photo with its buttons, or tick several and choose the right copy in the bar at the bottom; the other places are made to match. Files that cannot hold metadata (most videos, RAW) show n/a, which is not a disagreement.'),
    h('div', { class: 'ctools' },
      h('button', { class: 'pillbtn on', onclick: () => scan(false, false) }, icon('spark'), 'Check new and changed photos'),
      h('button', { class: 'pillbtn', onclick: async () => { if (await confirmDialog('Re-check every photo in the library? This reads every sidecar and image file and takes a while.')) scan(false, true); } }, 'Check every photo again'),
      [...filterParams().keys()].length ? h('button', { class: 'pillbtn', onclick: () => scan(true, true) }, 'Check the current search only') : null),
    head, list, more), syncBar);
  load();
}

export async function placeTagsDialog(ids) {
  let p;
  try { p = await api('/api/placetags' + (ids ? '?ids=' + ids.join(',') : '')); } catch (e) { toastError(e); return; }
  const body = h('div');
  const draw = () => fill(body,
    h('p', null, `${nf.format(p.to_tag)} of ${nf.format(p.photos)} geotagged photos would get a place tag they do not have yet.`),
    p.to_untag ? h('p', null, `${nf.format(p.to_untag)} photos have a place tag from an earlier position, which would be removed.`) : null,
    p.missing_tags.length ? h('p', null, `${nf.format(p.missing_tags.length)} new tags would be created under “${p.root}” (${nf.format(p.existing_tags)} already exist), for example: `, h('i', null, p.missing_tags.slice(0, 5).join(', '), p.missing_tags.length > 5 ? ', …' : '')) : h('p', null, 'All the needed tags already exist.'),
    p.missing_tags.length && p.digikamRunning ? h('p', { class: 'warn' }, 'digiKam is running. Quit it first; the new tags go into its database.') : null,
    p.progress.running ? h('p', { class: 'enote' }, `Running: ${nf.format(p.progress.done)} / ${nf.format(p.progress.total)} photos tagged, ${p.progress.created} tags created.`) : null,
    p.progress.error ? h('p', { class: 'warn' }, p.progress.error) : null);
  draw();
  const back = modal(ids ? `Place tags for ${nf.format(ids.length)} photos` : 'Place tags for the whole library', body,
    h('p', { class: 'muted small' }, 'Town names come from the offline place lookup and can be a neighboring town. Tags are assigned through the normal edit path, so they reach digiKam’s database with “Apply edits to digiKam database”.'),
    { buttons: ['Close',
      h('button', { class: 'pillbtn on', disabled: !p.to_tag || p.progress.running || !!(p.missing_tags.length && p.digikamRunning), onclick: async () => {
        try { const r = await post('/api/placetags', { ids: ids || [] }); toast(`Tagging ${nf.format(r.to_tag)} photos${r.creating ? `, creating ${r.creating} tags` : ''} in the background.`); } catch (e) { toastError(e); return; }
        const poll = async () => { try { p = await api('/api/placetags' + (ids ? '?ids=' + ids.join(',') : '')); } catch { return; } draw(); if (p.progress.running && back.isConnected) setTimeout(poll, 3000); else if (back.isConnected) { indexMeta(await api('/api/meta')); } };
        setTimeout(poll, 1500);
      } }, icon('place'), 'Create and assign')] });
}

/** Faces page: the decided faces leave the page at once; the batch is written in the background, and a
 *  failure puts them back. DECIDED hides them from the queue until the index has caught up. */
const DECIDED = new Map();  // "photo:x:y:w:h" -> when it was decided
const faceKey = (o) => `${o.image ?? o.photo}:${(o.rect || [o.x, o.y, o.w, o.h]).join(':')}`;
async function facesBatch(ops, msg) {
  const now = Date.now();
  ops.forEach((o) => DECIDED.set(faceKey(o), now));
  toast(`${msg}: ${plural(ops.length, 'face')}.`, null, null, 4000);
  const named = [...new Set(ops.filter((o) => ['name', 'add', 'confirm'].includes(o.op)).map((o) => o.image))];
  if (named.length) setBadges(named, { faces: true });
  post('/api/faces/batch', { ops }).then(async (r) => {
    if (r.failed.length) {
      r.failed.forEach((f) => { const o = ops.find((x) => x.image === f.image); if (o) DECIDED.delete(faceKey(o)); });
      toast(`${r.failed.length} of those could not be changed (${r.failed[0].error}).`, null, null, 9000);
      if (S.route.view === 'faces') onRouteFaces();
    }
    await awaitReindex(() => true, 30, 500);
    details.clear();
    for (const [k, t] of DECIDED) if (t <= now) DECIDED.delete(k);  // the index has them now
  }, (e) => {
    ops.forEach((o) => DECIDED.delete(faceKey(o)));
    toast(`Those face changes did not go through: ${e.message}`, null, null, 9000);
    if (S.route.view === 'faces') onRouteFaces();
  });
  return true;
}
function onRouteFaces() { S.viewKey = null; onRoute(); }
let facesTimer = 0;  // the one timer that follows face grouping; every draw of the page replaces it
export async function showFaces(vp) {
  clearTimeout(facesTimer);
  const tab = (vp && vp.get('tab')) || 'suggested';
  const page = h('div', { class: 'page' });
  const tabs = h('div', { class: 'eseg my10' }, [['suggested', 'Suggestions'], ['unknown', 'Unnamed faces'], ['ignored', 'Ignored']].map(([k, l]) => h('button', { class: k === tab ? 'on' : '', onclick: () => go('faces', photosWith({ tab: k }), true) }, l)));
  const body = h('div');
  main.replaceChildren(page);
  page.append(crumbs([['People', '#/people'], ['Faces', null]]), pageTitle('face', 'amber', 'Faces'), tabs, body);
  let d;
  try { d = await api(`/api/faces/queue?kind=${tab}&limit=500`); } catch (e) { body.replaceChildren(errorBox(e)); return; }
  if (S.route.view !== 'faces') return;
  if (DECIDED.size) {  // decided here a moment ago, not in the index yet: not shown again
    const before = d.faces.length;
    d.faces = d.faces.filter((f) => !DECIDED.has(faceKey(f)));
    d.total = Math.max(0, (d.total || 0) - (before - d.faces.length));
    if (d.groups) d.groups = d.groups.map(([tag, name, count]) => [tag, name, d.faces.filter((f) => f.tag === tag).length]).filter((g) => g[2] > 0);
  }
  const crop = (f) => h('img', { class: 'fcrop', src: `/facecrop/${f.photo}/${f.x}/${f.y}/${f.w}/${f.h}.jpg`, alt: '', loading: 'lazy', title: `${f.file} · ${dfDay.format(date(f.ts))} · click to open the photo`, onclick: () => openLightbox({ id: [f.photo] }, 0, true) });
  const op = (f, o, extra) => ({ op: o, image: f.photo, rect: [f.x, f.y, f.w, f.h], ...extra });
  const refresh = () => showFaces(vp);
  if (!d.faces.length) { body.append(h('div', { class: 'empty' }, tab === 'suggested' ? 'No suggestions waiting. digiKam makes suggestions when you run face recognition there.' : tab === 'unknown' ? 'No unnamed faces.' : 'No ignored faces.')); return; }
  if (tab === 'suggested') {
    body.append(h('div', { class: 'muted intro' }, `${nf.format(d.total)} faces digiKam thinks it recognized, grouped by person, like digiKam’s People view. Confirm a whole group at once, or decide face by face. Everything is written to digiKam’s database (digiKam closed; backup first).`));
    for (const [tag, name, count] of d.groups) {
      const faces = d.faces.filter((f) => f.tag === tag);
      const sel = new Set(faces.map((f, i) => i));
      const grid = h('div', { class: 'fgrid' });
      const draw = () => fill(grid, faces.map((f, i) => h('div', { class: 'fcell' + (sel.has(i) ? ' sel' : '') },
        h('label', { class: 'fchk' }, h('input', { type: 'checkbox', checked: sel.has(i), onchange: (e) => { e.target.checked ? sel.add(i) : sel.delete(i); draw(); } })),
        crop(f),
        h('div', { class: 'facts' },
          h('button', { class: 'tact', title: `Yes, this is ${name}`, onclick: async () => { if (await facesBatch([op(f, 'confirm')], `Confirmed ${name}`)) refresh(); } }, icon('check')),
          h('button', { class: 'tact', title: 'Someone else…', onclick: () => personPicker(async (t) => { if (await facesBatch([op(f, 'name', { tag: t.id })], `Named ${t.name}`)) refresh(); }) }, icon('person')),
          h('button', { class: 'tact', title: 'Not a face to name (ignore)', onclick: async () => { if (await facesBatch([op(f, 'ignore')], 'Ignored')) refresh(); } }, icon('close'))))));
      draw();
      body.append(h('div', { class: 'fgroup' },
        h('div', { class: 'fghead' }, h('img', { src: faceUrl(tag), alt: '' }), h('b', null, name), h('span', { class: 'muted' }, `${nf.format(count)} suggested`), h('span', { class: 'sp' }),
          h('button', { class: 'pillbtn on', onclick: async () => { const chosen = faces.filter((f, i) => sel.has(i)); if (!chosen.length) { toast('Nothing selected in this group.'); return; } if (await facesBatch(chosen.map((f) => op(f, 'confirm')), `Confirmed ${name}`)) refresh(); } }, icon('check'), 'Confirm selected'),
          h('button', { class: 'pillbtn', onclick: async () => { const chosen = faces.filter((f, i) => sel.has(i)); if (!chosen.length) return; if (await facesBatch(chosen.map((f) => op(f, 'unknown')), 'Rejected')) refresh(); } }, 'Reject selected'),
          h('button', { class: 'pillbtn', onclick: () => { sel.size === faces.length ? sel.clear() : faces.forEach((f, i) => sel.add(i)); draw(); } }, 'Toggle all')),
        grid));
    }
    return;
  }
  // unnamed faces: look-alikes grouped (SFace embeddings), each group named or ignored in one go; the viewer never proposes a name
  if (tab === 'unknown' && store.get(PREF.faceGroups, '1') !== '0') {
    let g = null;
    try { g = await api('/api/faces/groups?kind=unknown'); } catch { g = null; }
    if (g && DECIDED.size) {  // decided here a moment ago: gone from the groups too
      const keep = (f) => !DECIDED.has(faceKey(f));
      g.groups = g.groups.map((grp) => ({ ...grp, faces: grp.faces.filter(keep) })).map((grp) => ({ ...grp, n: grp.faces.length })).filter((grp) => grp.faces.length);
      g.ungrouped = g.ungrouped.filter(keep);
    }
    if (S.route.view !== 'faces') return;
    if (g && (g.groups.length || g.missing.length || g.progress.running)) {
      const head = h('div', { class: 'muted intro' }, `${nf.format(d.total)} faces digiKam found but could not name. Look-alikes are shown together so one decision covers the group, with ${S.APP_NAME}’s own “looks like” guess when a named person matches; untick a face to leave it out. `,
        h('button', { type: 'button', class: 'lnk', onclick: () => { store.set(PREF.faceGroups, '0'); refresh(); } }, 'Show as a plain grid'));
      const status = g.progress.running ? h('div', { class: 'enote' }, `Grouping… ${nf.format(g.progress.done)} / ${nf.format(g.progress.total)} faces compared.`)
        : g.missing.length ? h('div', { class: 'enote' }, `${nf.format(g.missing.length)} faces have not been compared yet (the Face grouping job in Maintenance runs in the background).`) : null;
      body.append(head, status);
      const nameAs = async (faces, s) => { if (!faces.length) return; if (await facesBatch(faces.map((f) => op(f, 'name', { tag: s.tag })), `Named ${s.name}`)) refresh(); };
      const looksLike = (s, faces) => s ? h('span', { class: 'lookslike' }, h('img', { src: faceUrl(s.tag), alt: '' }), `looks like ${s.name} · ${Math.round(s.score * 100)}%`,
        S.CAN_MANAGE ? h('button', { class: 'pillbtn on', style: 'height:26px;padding:0 10px', onclick: (e) => { e.stopPropagation(); nameAs(faces(), s); } }, `Name as ${s.name}`) : null) : null;
      const groupBlock = (faces, title, grouped, suggest) => {
        const sel = new Set(faces.map((f, i) => i));
        const grid = h('div', { class: 'fgrid' });
        const chosen = () => faces.filter((f, i) => sel.has(i));
        const draw = () => fill(grid, faces.map((f, i) => h('div', { class: 'fcell' + (sel.has(i) ? ' sel' : '') },
          S.CAN_MANAGE ? h('label', { class: 'fchk' }, h('input', { type: 'checkbox', checked: sel.has(i), onchange: (e) => { e.target.checked ? sel.add(i) : sel.delete(i); draw(); } })) : null, crop(f),
          h('div', { class: 'muted', style: 'font-size:11px;text-align:center' }, dfMonth.format(date(f.ts))),
          !grouped && f.suggest ? h('div', { class: 'fsug', title: `looks like ${f.suggest.name} (${Math.round(f.suggest.score * 100)}%)`, onclick: () => nameAs([f], f.suggest) }, h('img', { src: faceUrl(f.suggest.tag), alt: '' }), f.suggest.name) : null)));
        draw();
        return h('div', { class: 'fgroup' }, h('div', { class: 'fghead' }, h('b', null, title), grouped ? looksLike(suggest, chosen) : null, h('span', { class: 'sp' }),
          S.CAN_MANAGE ? [h('button', { class: 'pillbtn' + (suggest ? '' : ' on'), onclick: () => personPicker(async (t) => { const c = chosen(); if (!c.length) return; if (await facesBatch(c.map((f) => op(f, 'name', { tag: t.id })), `Named ${t.name}`)) refresh(); }) }, icon('person'), grouped ? 'Name these…' : 'Name selected…'),
            h('button', { class: 'pillbtn', onclick: async () => { const c = chosen(); if (!c.length) return; if (await facesBatch(c.map((f) => op(f, 'ignore')), 'Ignored')) refresh(); } }, 'Ignore'),
            h('button', { class: 'pillbtn', onclick: () => { sel.size === faces.length ? sel.clear() : faces.forEach((f, i) => sel.add(i)); draw(); } }, 'Toggle all')] : null), grid);
      };
      if (g.people === 0 && !g.progress.running) body.append(h('div', { class: 'muted note' }, 'Suggestions appear once the named people have been compared too (the Face grouping job does that first).'));
      g.groups.forEach((grp, k) => body.append(groupBlock(grp.faces, `Look-alikes ${k + 1} · ${grp.n} faces`, true, grp.suggest)));
      if (g.ungrouped.length) body.append(groupBlock(g.ungrouped, `${nf.format(g.ungrouped.length)} faces without a look-alike`, false));
      if (g.missing.length) body.append(groupBlock(g.missing, `${nf.format(g.missing.length)} faces not compared yet`, false));
      if (g.progress.running) {  // grouping goes on in the background: the page follows it, but not under a selection being made (a face unticked)
        const follow = () => { facesTimer = setTimeout(() => { if (S.route.view !== 'faces' || !page.isConnected) return; body.querySelector('.fcell:not(.sel)') ? follow() : refresh(); }, 5000); };
        follow();
      }
      return;
    }
  }
  // unnamed or ignored faces: a selectable grid, name or ignore many at once
  const sel = new Set();
  const grid = h('div', { class: 'fgrid' });
  if (tab === 'unknown' && store.get(PREF.faceGroups, '1') === '0') body.append(h('div', { class: 'muted note' }, h('button', { type: 'button', class: 'lnk', onclick: () => { store.set(PREF.faceGroups, '1'); refresh(); } }, 'Group look-alikes'), ' (needs the SFace model from Maintenance › AI analysis and models).'));
  const bar = h('div', { class: 'toolbar my10',});
  const chosen = () => d.faces.filter((f, i) => sel.has(i));
  const drawBar = () => S.CAN_MANAGE && fill(bar,
    h('span', null, `${nf.format(sel.size)} selected of ${nf.format(d.faces.length)}`),
    h('button', { class: 'pillbtn', onclick: () => { sel.size === d.faces.length ? sel.clear() : d.faces.forEach((f, i) => sel.add(i)); draw(); } }, sel.size === d.faces.length ? 'Select none' : 'Select all'),
    h('button', { class: 'pillbtn on', disabled: !sel.size, onclick: () => personPicker(async (t) => { const c = chosen(); if (await facesBatch(c.map((f) => op(f, 'name', { tag: t.id })), `Named ${t.name}`)) refresh(); }) }, icon('person'), 'Name selected…'),
    tab === 'unknown' ? h('button', { class: 'pillbtn', disabled: !sel.size, onclick: async () => { const c = chosen(); if (await facesBatch(c.map((f) => op(f, 'ignore')), 'Ignored')) refresh(); } }, 'Ignore selected') : null,
    tab === 'ignored' ? h('button', { class: 'pillbtn', disabled: !sel.size, onclick: async () => { const c = chosen(); if (await facesBatch(c.map((f) => op(f, 'unknown')), 'Back to unknown')) refresh(); } }, 'Back to unnamed') : null,
    h('button', { class: 'pillbtn', disabled: !sel.size, onclick: async () => { const c = chosen(); if (!await confirmDialog(`Remove ${plural(c.length, 'face region')}? They are deleted from digiKam’s database and the sidecars; there is no undo.`, { ok: 'Remove', danger: true })) return; if (await facesBatch(c.map((f) => op(f, 'remove')), 'Removed')) refresh(); } }, 'Remove selected'));
  const draw = () => { fill(grid, d.faces.map((f, i) => h('div', { class: 'fcell' + (sel.has(i) ? ' sel' : ''), tabindex: 0, role: 'button', onclick: (e) => { if (e.target.tagName === 'IMG' && !e.shiftKey && !sel.size) return; if (!S.CAN_MANAGE) return; e.preventDefault(); sel.has(i) ? sel.delete(i) : sel.add(i); draw(); } },
    S.CAN_MANAGE ? h('label', { class: 'fchk' }, h('input', { type: 'checkbox', checked: sel.has(i), onclick: (e) => e.stopPropagation(), onchange: (e) => { e.target.checked ? sel.add(i) : sel.delete(i); draw(); } })) : null, crop(f),
    h('div', { class: 'muted', style: 'font-size:11px;text-align:center' }, dfMonth.format(date(f.ts)))))); drawBar(); };
  body.append(h('div', { class: 'muted intro' }, tab === 'unknown' ? `${nf.format(d.total)} faces digiKam found but could not name. Tick the ones that are the same person and name them together; tick ones that are not faces to name and ignore them. Click a face to open its photo.` : `${nf.format(d.total)} faces marked as not to be named.`), bar, grid);
  draw();
  if (d.total > d.faces.length) body.append(h('p', { class: 'muted' }, `Showing the first ${nf.format(d.faces.length)}; decide these and the next ones appear.`));
}


export async function showDuplicates() {
  main.replaceChildren(h('div', { class: 'empty' }, 'Looking for duplicates…'));
  let d;
  try { d = await api('/api/duplicates'); } catch { if (S.route.view === 'duplicates') main.replaceChildren(h('div', { class: 'empty' }, 'Could not load duplicates.')); return; }
  if (S.route.view !== 'duplicates') return;
  const src = { id: d.groups.flatMap((g) => g.items.map((x) => x.id)) };
  let k = 0;
  main.replaceChildren(h('div', { class: 'page' }, crumbs([['Explore', '#/explore'], ['Duplicate files', null]]), pageTitle('dup', 'purple', 'Duplicate files'),
    h('div', { class: 'muted' }, d.groups.length ? `${nf.format(d.groups.length)} sets of byte-identical files, ${fmtBytes(d.wasted)} that could be freed. ${S.APP_NAME} never deletes anything; remove the extras in digiKam.`
      : 'No byte-identical files found. digiKam’s file hashes say every item is unique.'),
    d.groups.map((g) => h('div', { class: 'dupgroup' }, h('div', { class: 'muted' }, `${g.items.length} copies · ${fmtBytes(g.size)} each`),
      h('div', { class: 'duprow' }, g.items.map((x) => { const i = k++; return h('div', { class: 'dupitem' },
        h('div', { class: 'tile', 'data-id': x.id, style: `width:${Math.round(150 * Math.min(2, x.w / (x.h || 1)))}px;height:150px`, onclick: () => openLightbox(src, i, true) }, h('img', { src: `/thumb/${x.id}`, loading: 'lazy', alt: '' })),
        h('div', { class: 'nm', title: x.name }, x.name), h('a', { class: 'sb lnk', href: hashFor('photos', photosWith({ album: albumIdByPath(x.album), sort: 'old' })) }, x.album.replace(/^\//, ''))); })))),
    nearDupsSection(src)));
  badgeTiles(main);
  syncLightbox();
}
/** Near-duplicates: look-alike photos found with the content-search embeddings (resized, re-saved, burst neighbors). */
function nearDupsSection(src) {
  const sec = h('div', { style: 'margin-top:36px' });
  let timer = 0;
  const later = (ms) => { clearTimeout(timer); timer = setTimeout(() => { if (S.route.view === 'duplicates' && sec.isConnected) draw(); }, ms); };
  const draw = async () => {
    let d; try { d = await api('/api/neardups'); } catch (e) { fill(sec, errorBox(e)); return; }
    if (S.route.view !== 'duplicates' || !sec.isConnected) return;
    const p = d.progress;
    const level = h('select', null, Object.keys(d.levels).map((k) => h('option', { value: k, selected: k === d.level }, `${k} (${d.levels[k]})`)));
    const run = S.IS_ADMIN ? h('button', { class: 'pillbtn on', disabled: p.running || !d.enabled, onclick: async () => { try { await post('/api/neardups', { level: level.value }); toast('Looking for similar photos in the background.'); } catch (e) { toastError(e, TOAST_LONG); } later(1500); } }, icon('spark'), d.gen ? 'Find again' : 'Find similar photos') : null;
    const note = !d.enabled ? 'Needs content search (SigLIP 2 or CLIP) installed: the similarity comes from its embeddings.'
      : p.running ? `Comparing… ${nf.format(p.done)} / ${nf.format(p.total)} photos.`
      : p.error ? `Last run failed: ${p.error}`
      : !d.gen ? 'Not computed yet. Every photo is compared with every other using the content-search embeddings; a minute or two for a large library.'
      : d.current ? `${nf.format(d.groups.length)} sets of similar photos at the “${d.level}” level. Sharpest copy first in each set.` : `Computed for an older search index (${d.level}); run it again for current results.`;
    fill(sec, h('h2', { style: '--hue:var(--c-purple)' }, h('span', { class: 'hic' }, icon('spark')), 'Similar photos'),
      h('div', { class: 'muted intro' }, `Resized, re-saved or re-edited copies and burst neighbors, which the byte-identical check above cannot see. ${S.APP_NAME} never deletes anything; it only shows the sets.`),
      h('div', { class: 'toolbar my10',}, S.IS_ADMIN ? h('span', null, 'Strictness: ', level) : null, run, h('span', { class: 'muted' }, note)),
      d.groups.map((g) => h('div', { class: 'dupgroup' }, h('div', { class: 'muted' }, `${g.n} similar · ${Math.round(g.score * 100)}% alike${g.identical ? ' · also byte-identical' : ''}`),
        h('div', { class: 'duprow' }, g.items.map((x) => { const i = src.id.push(x.id) - 1; return h('div', { class: 'dupitem' },
          h('div', { class: 'tile', 'data-id': x.id, style: `width:${Math.round(150 * Math.min(2, x.w / (x.h || 1)))}px;height:150px`, onclick: () => openLightbox(src, i, true) }, h('img', { src: `/thumb/${x.id}`, loading: 'lazy', alt: '' })),
          h('div', { class: 'nm', title: x.name }, x.name), h('div', { class: 'sb' }, `${fmtBytes(x.size || 0)}${x.sharpn != null ? ' · sharpness ' + Math.round(x.sharpn * 100) + '%' : ''}`),
          h('a', { class: 'sb lnk', href: hashFor('photos', photosWith({ album: albumIdByPath(x.album), sort: 'old' })) }, x.album.replace(/^\//, ''))); })))));
    badgeTiles(sec);
    if (p.running) later(4000);
  };
  draw();
  return sec;
}
const albumIdByPath = (path) => S.META.albums.find((a) => a.path === path)?.id ?? '';

export async function tagOp(op, t) {
  let r;
  try { r = await post('/api/tags/op', op); } catch (e) { toastError(e); return; }
  toast(op.op === 'icon' ? `“${r.path}” now uses this photo.` : op.op === 'notperson' ? `“${r.path}” is a keyword tag again, no longer listed under People.` : op.op === 'person' ? `“${r.path}” is a person now.` : op.op === 'merge' ? `Merged “${r.path}” into “${r.new_path}” (${nf.format(r.photos)} photos).` : op.op === 'add' ? `Created “${r.path}”.` : `${{ rename: 'Renamed', move: 'Moved', delete: 'Deleted' }[op.op]} “${r.path}”${r.new_path ? ` → “${r.new_path}”` : ''}.` +
    (r.sidecars ? ` Updating ${nf.format(r.photos)} photos’ sidecars in the background.` : ''), null, null, TOAST_LONG);
  await awaitReindex();
  $('#taglist')?.remove();  // the tag datalist is rebuilt from the new tag list on next use
  details.clear();
  if (S.route.view === 'tags') showTags();
  pollSoon(500);
}
const openTags = new Set();  // which tag nodes are expanded, kept across redraws
/** digiKam's own face buckets under People; not real people and not to be renamed, moved or deleted. */
const isFaceBucket = (t) => ['Unknown', 'Ignored', 'Unconfirmed'].includes(t.name) && t.full.startsWith('People/');
/** parent id (0 = top level) -> child tags, optionally leaving some tags out. */
function tagKids(skip = () => false) {
  const kidsOf = new Map();
  for (const t of S.META.tags) { if (skip(t)) continue; const k = t.pid ?? 0; if (!kidsOf.has(k)) kidsOf.set(k, []); kidsOf.get(k).push(t); }
  return kidsOf;
}
function tagTreePicker(onPick, { exclude = () => false, allowTop = true } = {}) {
  const kidsOf = tagKids(isFaceBucket);
  const filter = h('input', { class: 'pagefilter', style: 'margin:0 0 8px;width:100%', placeholder: 'Find a tag…' });
  const list = h('div', { class: 'alist', style: 'max-height:45vh' });
  const row = (t, depth, flat) => {
    const kids = flat ? [] : (kidsOf.get(t.id) || []);
    const box = h('div');
    const node = h('div', null,
      h('div', { class: 'frow' + (exclude(t) ? ' dis' : ''), style: `padding-left:${8 + depth * 18}px`, title: t.full, onclick: () => { if (!exclude(t)) onPick(t); } },
        h('span', { class: 'tw', style: kids.length ? '' : 'visibility:hidden', onclick: (e) => { e.stopPropagation(); if (!box.children.length) box.append(...kids.map((k) => row(k, depth + 1))); box.hidden = !box.hidden; e.currentTarget.style.transform = box.hidden ? '' : 'rotate(90deg)'; } }, icon('right')),
        icon('tag'), h('span', { class: 'nm' }, flat ? t.full.replaceAll('/', ' › ') : t.name), h('span', { class: 'ct' }, nf.format(t.nAll))), box);
    box.hidden = true;
    return node;
  };
  const draw = (f) => list.replaceChildren(...(allowTop && !f ? [h('div', { class: 'frow', onclick: () => onPick(null) }, icon('folder'), h('span', { class: 'nm' }, h('i', null, 'Top level (no parent)')))] : []),
    ...(f ? S.META.tags.filter((t) => t._n.includes(f)).slice(0, 100).map((t) => row(t, 0, true)) : (kidsOf.get(0) || []).map((t) => row(t, 0))));
  filter.oninput = () => draw(norm(filter.value));
  draw('');
  return h('div', null, filter, list);
}
function moveTagDialog(t) {
  const sub = (x) => x.id === t.id || x.full.startsWith(t.full + '/') || x.id === t.pid;
  const move = (parent) => { back.remove(); tagOp({ op: 'move', id: t.id, parent }, t); };
  const back = modal(`Move “${t.name}” into…`,
    h('p', null, `Now at “${t.full.replaceAll('/', ' › ')}”. Its sub-tags and photos move with it. Pick the new parent:`),
    tagTreePicker((dest) => { if (dest && sub(dest)) { toast('A tag cannot be moved into itself.'); return; } move(dest ? dest.id : 0); }, { exclude: sub, allowTop: t.pid != null }),
    { buttons: ['Cancel'] });
}

export function showTags() {
  document.body.classList.toggle('managing', S.manageTags);  // onRoute drops the class with the page; the state itself stays for the next visit
  const kidsOf = tagKids();
  const row = (t, flat) => {
    const kids = flat ? [] : (kidsOf.get(t.id) || []);
    const node = h('div', { class: 'tnode' });
    const box = h('div', { class: 'tkids' });
    const expand = (on) => {
      if (on && !box.children.length) box.append(...kids.map((k) => row(k)));
      node.classList.toggle('open', on);
      box.hidden = !on;
      on ? openTags.add(t.id) : openTags.delete(t.id);
    };
    const tw = h('span', { class: 'tw', style: kids.length ? '' : 'visibility:hidden', onclick: (e) => { e.stopPropagation(); expand(!node.classList.contains('open')); } }, icon('right'));
    box.hidden = true;
    if (!flat && kids.length && openTags.has(t.id)) expand(true);
    const cover = t.face ? h('img', { class: 'rnd', src: faceUrl(t.face), loading: 'lazy', alt: '' })
      : t.cover ? h('img', { src: `/thumb/${t.cover}`, loading: 'lazy', alt: '' }) : h('span', { class: 'ph' }, icon('tag'));
    const special = t.full === 'People' || isFaceBucket(t);
    const can = S.manageTags && !special;
    const isPerson = t.person || t.full.startsWith('People/');
    const trow = h('div', { class: 'trow', title: t.full, tabindex: 0, role: 'button', onclick: () => go('photos', photosWith({ tag: t.id })) }, tw, cover,
      h('span', { class: 'nm' }, flat ? t.full.replaceAll('/', ' › ') : t.name),
      can ? h('span', { class: 'tacts' },
        actBtn('add', 'Add a tag inside this one', async () => { const n = await promptDialog(`New tag inside “${t.name}”`); if (n) tagOp({ op: 'add', parent: t.id, name: n }); }),
        actBtn('edit', 'Rename', async () => { const n = await promptDialog('Rename tag', t.name); if (n && n !== t.name) tagOp({ op: 'rename', id: t.id, name: n }, t); }),
        h('button', { class: 'pillbtn tmove', title: 'Move this tag (with its sub-tags) under another tag, or to the top level', onclick: (e) => { e.stopPropagation(); moveTagDialog(t); } }, icon('folder'), 'Move'),
        t.person && !kids.length ? h('button', { class: 'pillbtn tmove', title: 'The same person entered twice: move everything on this one to another person, then remove this one', onclick: (e) => { e.stopPropagation();
          personPicker(async (into) => { if (into.id !== t.id && await confirmDialog(`Merge “${t.full.replaceAll('/', ' › ')}” into “${into.full.replaceAll('/', ' › ')}”?\n\nIts ${nf.format(t.nAll)} photos and face boxes move to ${into.name}, then “${t.name}” is removed. digiKam's database is backed up first.`)) tagOp({ op: 'merge', id: t.id, into: into.id }, t); });
        } }, 'Merge into…') : null,
        t.person ? h('button', { class: 'pillbtn tmove', title: 'Make this a keyword tag again, off the People page (refused while face boxes carry the name)', onclick: async (e) => { e.stopPropagation(); if (await confirmDialog(`Make “${t.name}” a keyword tag again? It leaves the People page; the photos keep the tag.`, { ok: 'Not a person' })) tagOp({ op: 'notperson', id: t.id }, t); } }, 'Not a person')
          : !kids.length ? h('button', { class: 'pillbtn tmove', title: 'Mark this tag as a person: it shows under People and faces can be named with it', onclick: (e) => { e.stopPropagation(); tagOp({ op: 'person', id: t.id }, t); } }, icon('person'), 'Is a person') : null,
        isPerson ? null : actBtn('trash', 'Delete', async () => { const kidsN = S.META.tags.filter((x) => x.full.startsWith(t.full + '/')).length;
          if (await confirmDialog(`Delete the tag “${t.full}”${kidsN ? ` and its ${kidsN} sub-tags` : ''}?\n\nIt will be removed from ${nf.format(t.nAll)} photos (the photos themselves are not touched).`, { ok: 'Delete', danger: true })) tagOp({ op: 'delete', id: t.id }, t); })) : null,
      h('span', { class: 'ct' }, nf.format(t.nAll)));
    if (can && !flat) {  // drag a tag onto another tag to file it there
      trow.draggable = true;
      trow.ondragstart = (e) => { e.dataTransfer.setData('text/tag', String(t.id)); e.dataTransfer.effectAllowed = 'move'; S.dragTag = t; };
      trow.ondragend = () => { S.dragTag = null; document.querySelectorAll('.trow.over').forEach((x) => x.classList.remove('over')); };
    }
    if (S.manageTags && !special) {
      const ok = () => S.dragTag && S.dragTag.id !== t.id && S.dragTag.pid !== t.id && !t.full.startsWith(S.dragTag.full + '/');
      trow.ondragover = (e) => { if (ok()) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; trow.classList.add('over'); } };
      trow.ondragleave = () => trow.classList.remove('over');
      trow.ondrop = (e) => { e.preventDefault(); trow.classList.remove('over'); if (ok()) { const d = S.dragTag; S.dragTag = null; openTags.add(t.id); tagOp({ op: 'move', id: d.id, parent: t.id }, d); } };
    }
    node.append(trow, box);
    return node;
  };
  const tree = h('div', { class: 'tree' });
  const topZone = h('div', { class: 'dropzone', hidden: !S.manageTags }, icon('folder'), 'Drop a tag here to make it a top-level tag');
  topZone.ondragover = (e) => { if (S.dragTag && S.dragTag.pid != null) { e.preventDefault(); topZone.classList.add('over'); } };
  topZone.ondragleave = () => topZone.classList.remove('over');
  topZone.ondrop = (e) => { e.preventDefault(); topZone.classList.remove('over'); if (S.dragTag && S.dragTag.pid != null) { const d = S.dragTag; S.dragTag = null; tagOp({ op: 'move', id: d.id, parent: 0 }, d); } };
  const draw = (f) => tree.replaceChildren(...(f ? S.META.tags.filter((t) => t._n.includes(f)).sort((a, b) => b.nAll - a.nAll).slice(0, 200).map((t) => row(t, true))
    : (kidsOf.get(0) || []).map((t) => row(t))));
  main.replaceChildren(h('div', { class: 'page' }, pageTitle('tag', 'orange', 'Tags ', h('span', { class: 'muted' }, nf.format(S.META.tags.length))),
    h('div', { class: 'muted' }, 'Selecting a tag also includes everything filed under it.'),
    h('div', { class: 'toolbar wide',},
      h('input', { class: 'pagefilter', placeholder: 'Find a tag…', oninput: (e) => draw(norm(e.target.value)) }),
      S.CAN_MANAGE ? h('button', { class: 'pillbtn mt6' + (S.manageTags ? ' on' : ''), onclick: () => { S.manageTags = !S.manageTags; document.body.classList.toggle('managing', S.manageTags); showTags(); } }, icon('edit'), S.manageTags ? 'Done managing' : 'Manage tags') : null,
      S.manageTags ? h('button', { class: 'pillbtn mt6', onclick: async () => { const n = await promptDialog('New top-level tag'); if (n) tagOp({ op: 'add', parent: 0, name: n }); } }, icon('add'), 'New top-level tag') : null),
    S.manageTags ? h('div', { class: 'enote', style: 'max-width:760px' }, 'To change the hierarchy, drag a tag onto the tag that should contain it, or use its Move button to pick a parent from the tree. Sub-tags and photos move with it. Changes go to digiKam’s database, so digiKam must be closed; a backup is saved first, and the sidecars of affected photos are updated in the background. People tags are managed in digiKam.') : null,
    h('div', { style: 'height:8px' }), topZone, tree));
  draw('');
}
