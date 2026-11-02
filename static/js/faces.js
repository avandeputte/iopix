/** The face tools of the viewer: the face and object overlays, the face menus, naming (the person picker), drawing,
 *  adjusting and removing face boxes, and faceOp, which shows a change at once and sends it behind. */
import { $, api, h, icon, nf, plural, post } from './core.js';
import { S, faceUrl, toast, toastError } from './state.js';
import { setBadges } from './grid.js';
import { listPicker, modal } from './actions.js';
import { tagOp } from './admin.js';
import { awaitReindex } from './main.js';
import { LB, details, filterFromLightbox, getDetail, popMenu, setMode, showItem } from './lightbox.js';
import { renderInfo } from './info.js';

// face changes made here in the last minute, per photo: a copy of the photo fetched from the server while the
// index has not caught up yet must not show the old faces for a moment, so they are applied to it again
const RECENT_FACE_OPS = new Map();  // photo id -> [{op, at}]
export const reapplyRecent = (id, d) => {
  const ops = (RECENT_FACE_OPS.get(id) || []).filter((x) => Date.now() - x.at < 60000);
  if (!ops.length) { RECENT_FACE_OPS.delete(id); return d; }
  RECENT_FACE_OPS.set(id, ops);
  if (d && d.faces) ops.forEach((x) => applyFaceLocally(d, x.op));
  return d;
};
export function toggleObjects() {
  LB.objects = !LB.objects;
  LB.el.objectsBtn.classList.toggle('on', LB.objects);
  renderFaces();
}
export function toggleFaces() {
  LB.faces = !LB.faces;
  LB.el.facesBtn.classList.toggle('on', LB.faces);
  renderFaces();
}
/** Face regions on the photo (confirmed, suggested, unknown), shown when LB.faces is on; ignored ones are never drawn. */
export const faceRegions = (d) => [...d.faces.map((f) => ({ ...f, kind: 'confirmed' })), ...(d.faces_other || [])].filter((f) => f.kind !== 'ignored');
/** Redraw the face and object overlays on the picture from the toggles in LB.faces / LB.objects. */
export function renderFaces() {
  const { frame } = LB.el, d = LB.detail;
  frame.querySelectorAll('.facebox').forEach((x) => x.remove());
  if (!d || d.video) return;
  for (const b of LB.objects ? d.boxes || [] : []) {
    frame.append(h('div', { class: 'facebox objbox', style: `left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%` }, h('span', null, b.cls)));
  }
  const pct = (f) => `left:${(f.x / d.w) * 100}%;top:${(f.y / d.h) * 100}%;width:${(f.w / d.w) * 100}%;height:${(f.h / d.h) * 100}%`;
  for (const f of LB.faces ? faceRegions(d) : []) {
    const label = f.kind === 'confirmed' ? f.name : f.kind === 'unknown' ? 'Unknown' : f.kind === 'suggested' ? `${f.name}?` : 'Ignored';
    frame.append(h('div', { class: 'facebox face-' + f.kind, style: pct(f), title: label + (S.CAN_MANAGE ? ' · click for options' : ''),
      onclick: (e) => { e.stopPropagation(); S.CAN_MANAGE ? faceMenu(e.currentTarget, f, d) : (f.kind === 'confirmed' && filterFromLightbox({ tag: f.tag })); } }, h('span', null, label)));
  }
}
export function faceMenu(box, f, d) {
  $('.facemenu')?.remove();
  const rect = () => [f.x, f.y, f.w, f.h];
  const item = (label, fn, danger) => h('div', { class: 'fmi' + (danger ? ' danger' : ''), onclick: (e) => { e.stopPropagation(); menu.remove(); fn(); } }, label);
  const nameIt = () => personPicker((t) => faceOp({ op: 'name', image: d.id, rect: rect(), tag: t.id }), d.id);
  const items = f.kind === 'confirmed' ? [
    item(`Show all photos of ${f.name}`, () => filterFromLightbox({ tag: f.tag })),
    d.id !== S.tagById.get(f.tag)?.icon ? item(`Use as ${f.name}’s badge picture`, () => setTagIcon(S.tagById.get(f.tag) || { id: f.tag, name: f.name, person: 1 }, d.id)) : null,
    item('This is someone else…', nameIt),
    item('Forget the name (keep as unknown face)', () => faceOp({ op: 'unknown', image: d.id, rect: rect() })),
    item('Not a face to name (ignore)', () => faceOp({ op: 'ignore', image: d.id, rect: rect() })),
    item('Adjust the box (move or resize)…', () => adjustFace(f, d)),
    item('Remove this face region', () => removeFace(f, d), true),
  ] : f.kind === 'suggested' ? [
    item(`Yes, this is ${f.name}`, () => faceOp({ op: 'confirm', image: d.id, rect: rect() })),
    item('No, this is someone else…', nameIt),
    item('Not a face to name (ignore)', () => faceOp({ op: 'ignore', image: d.id, rect: rect() })),
    item('Adjust the box (move or resize)…', () => adjustFace(f, d)),
    item('Remove this region', () => removeFace(f, d), true),
  ] : [
    item('Who is this?…', nameIt),
    f.kind !== 'ignored' ? item('Not a face to name (ignore)', () => faceOp({ op: 'ignore', image: d.id, rect: rect() })) : item('Keep as unknown face', () => faceOp({ op: 'unknown', image: d.id, rect: rect() })),
    item('Adjust the box (move or resize)…', () => adjustFace(f, d)),
    item('Remove this region', () => removeFace(f, d), true),
  ];
  const menu = h('div', { class: 'facemenu' }, h('div', { class: 'fmh' }, f.kind === 'confirmed' ? f.name : f.kind === 'suggested' ? `Suggestion: ${f.name}` : f.kind === 'unknown' ? 'Unknown face' : 'Ignored face'), items);
  const inPanel = !!box.closest('.lb-info');
  // a menu opened from the info panel is fixed to the viewport, but it has to live inside #lightbox
  // (its own stacking context) or the lightbox paints over it
  const stage = inPanel ? $('#lightbox') : LB.el.stage, sr = inPanel ? { left: 0, top: 0, width: innerWidth, height: innerHeight } : stage.getBoundingClientRect(), br = box.getBoundingClientRect();
  if (inPanel) menu.style.position = 'fixed';
  stage.append(menu);
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = br.left - sr.left, top = br.bottom - sr.top + 4;
  if (left + mw > sr.width - 8) left = Math.max(8, sr.width - mw - 8);
  if (top + mh > sr.height - 8) top = Math.max(8, br.top - sr.top - mh - 4);
  menu.style.left = left + 'px'; menu.style.top = top + 'px';
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
  setTimeout(() => document.addEventListener('pointerdown', close, true), 0);
}
/** A face change shows at once: the photo's details are changed here first, the request follows in the
 *  background (one at a time per photo, in order), and only a failure brings the server's version back. */
const FACE_Q = new Map();  // photo id -> promise of the last change sent for it
/** A person tagged on the photo without a face box: show their photos, mark their face, or take them off the photo. */
export function tagOnlyMenu(anchor, f, d) {
  popMenu(anchor, [
    `${f.name} · no face box`,
    ['Show all photos of ' + f.name, () => filterFromLightbox({ tag: f.tag })],
    !d.video ? [`Mark ${f.name}’s face…`, () => startDrawFace()] : null,
    [`Not in this photo: take ${f.name} off`, () => faceOp({ op: 'untag', image: d.id, tag: f.tag }, () => faceOp({ op: 'tagonly', image: d.id, tag: f.tag }, null, `${f.name} put back.`)), 'danger'],
  ]);
}
export const hasPeople = (d) => !!(d.faces?.length || d.tags?.some((t) => t.person));
function applyFaceLocally(d, op) {
  const same = (f) => f.x === op.rect?.[0] && f.y === op.rect?.[1] && f.w === op.rect?.[2] && f.h === op.rect?.[3];
  const t = op.tag ? S.tagById.get(op.tag) : null;
  const confirmed = (d.faces || []).find(same), other = (d.faces_other || []).find(same);
  const was = confirmed ? { ...confirmed, kind: 'confirmed' } : other;
  d.faces = (d.faces || []).filter((f) => !same(f));
  d.faces_other = (d.faces_other || []).filter((f) => !same(f));
  const at = (r) => ({ x: r[0], y: r[1], w: r[2], h: r[3] });
  const keepTag = (id, name) => { if (id && !d.tags.some((x) => x.id === id)) { const tt = S.tagById.get(id); d.tags.push({ id, name: name || tt?.name, full: tt?.full || name, person: 1 }); } };
  const dropTag = (id) => { if (id && !d.faces.some((f) => f.tag === id)) d.tags = d.tags.filter((x) => x.id !== id); };
  if (op.op === 'name' || op.op === 'add') { d.faces.push({ tag: op.tag, name: t?.name || '…', ...at(op.rect) }); keepTag(op.tag, t?.name); if (was && was.tag !== op.tag && was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'confirm' && was) { d.faces.push({ tag: was.tag, name: was.name, ...at(op.rect) }); keepTag(was.tag, was.name); }
  else if (op.op === 'ignore' && was) { d.faces_other.push({ ...at(op.rect), kind: 'ignored', tag: null, name: null }); if (was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'unknown' && was) { d.faces_other.push({ ...at(op.rect), kind: 'unknown', tag: null, name: null }); if (was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'remove' && was) { if (was.kind === 'confirmed') dropTag(was.tag); }
  else if (op.op === 'detected') d.faces_other.push({ ...at(op.rect), kind: 'unknown', tag: null, name: null });
  else if (op.op === 'suggest') d.faces_other.push({ ...at(op.rect), kind: 'suggested', tag: op.tag, name: t?.name || '…' });
  else if (op.op === 'untag') d.tags = d.tags.filter((x) => x.id !== op.tag);
  else if (op.op === 'tagonly') keepTag(op.tag);
  else if (op.op === 'move' && was) { const moved = { ...was, ...at(op.new_rect) }; if (was.kind === 'confirmed') { delete moved.kind; d.faces.push(moved); } else d.faces_other.push(moved); }
}
function redrawPhoto(id) {
  if (LB.open && LB.src.id[LB.i] === id && LB.detail) { renderFaces(); if (LB.info) renderInfo(); }
}
/** Remove a face region at once (no question); the message offers Undo, which puts the same region back. */
function removeFace(f, d) {
  const rect = [f.x, f.y, f.w, f.h];
  const back = f.kind === 'confirmed' ? { op: 'add', tag: f.tag } : f.kind === 'suggested' ? { op: 'suggest', tag: f.tag } : f.kind === 'unknown' ? { op: 'detected' } : null;
  faceOp({ op: 'remove', image: d.id, rect }, back ? () => faceOp({ ...back, image: d.id, rect }) : null);
}
/** Every unnamed (unknown) face region on the photo, removed in one go; Undo puts them all back. */
export function removeUnnamedFaces(d) {
  const rects = faceRegions(d).filter((f) => f.kind === 'unknown').map((f) => [f.x, f.y, f.w, f.h]);
  if (!rects.length) { toast('No unnamed face regions on this photo.'); return; }
  const ops = (op) => rects.map((rect) => ({ op, image: d.id, rect }));
  faceOp(ops('remove'), () => faceOp(ops('detected'), null, `${plural(rects.length, 'unnamed face')} put back.`), `${plural(rects.length, 'unnamed face region')} removed.`);
}
/** One face change, or several on the same photo (an array, sent as one batch): shown at once, sent in order, undone from the message. */
export function faceOp(op, undo, said) {  // exported for test_ui.py, which calls it through import()
  const ops = Array.isArray(op) ? op : [op];
  op = ops[0];
  const id = op.image;
  const recs = ops.map((o) => ({ op: o, at: Date.now() }));
  RECENT_FACE_OPS.set(id, [...(RECENT_FACE_OPS.get(id) || []), ...recs]);
  const shown = LB.open && LB.src.id[LB.i] === id ? LB.detail : null, cached = details.get(id);  // the cache holds promises
  if (shown) ops.forEach((o) => applyFaceLocally(shown, o));
  if (cached) cached.then((cd) => { if (cd && cd !== shown && cd.faces) ops.forEach((o) => applyFaceLocally(cd, o)); }).catch(() => {});  // a copy the viewer is not showing (a still pending one gets the change from reapplyRecent)
  redrawPhoto(id);
  // the face badge on its thumbnails follows at once: a named face, or a person tagged without a box
  if (shown) setBadges([id], { faces: hasPeople(shown) });
  else if (cached) cached.then((cd) => { if (cd?.faces) setBadges([id], { faces: hasPeople(cd) }); }).catch(() => {});
  else if (ops.some((o) => ['name', 'add', 'confirm', 'tagonly'].includes(o.op))) setBadges([id], { faces: true });
  else getDetail(id).then((cd) => setBadges([id], { faces: hasPeople(cd) })).catch(() => {});  // a removal: does anyone stay? (the fetched copy gets this change re-applied)
  const name = op.tag ? S.tagById.get(op.tag)?.name : null;
  said = said || { name: `Named ${name}.`, confirm: 'Confirmed.', ignore: 'Face ignored.', unknown: 'Name removed; the face stays as unknown.', remove: 'Face region removed.', add: `Added ${name}.`, move: 'Region moved.', detected: 'Face put back.', suggest: 'Suggestion put back.', untag: `${name} taken off this photo.`, tagonly: `Added ${name}.` }[op.op];
  if (said) undo ? toast(said, 'Undo', undo, 7000) : toast(said);
  const prev = FACE_Q.get(id) || Promise.resolve();
  const send = () => ops.length === 1 ? post('/api/faces/op', op) : post('/api/faces/batch', { ops }).then((r) => { if (r.failed?.length) throw new Error(r.failed[0].error); });
  const mine = prev.then(send).then(() => true, (e) => {
    toast(`That face change did not go through: ${e.message}`, null, null, 9000);
    RECENT_FACE_OPS.set(id, (RECENT_FACE_OPS.get(id) || []).filter((x) => !recs.includes(x)));
    details.delete(id);
    getDetail(id).then((fresh) => { setBadges([id], { faces: hasPeople(fresh) }); if (LB.open && LB.src.id[LB.i] === id) { LB.detail = fresh; redrawPhoto(id); } }).catch(() => {});
    return false;
  });
  FACE_Q.set(id, mine);
  // once the index has it, take the server's version (unless another change for this photo is still on its way)
  mine.then(async (ok) => {
    if (!ok) return;
    await awaitReindex(() => true, 30, 500);
    if (FACE_Q.get(id) !== mine) return;
    FACE_Q.delete(id);
    details.delete(id);
    if (LB.open && LB.src.id[LB.i] === id) getDetail(id).then((fresh) => { if (LB.src.id[LB.i] === id && !FACE_Q.has(id)) { LB.detail = fresh; redrawPhoto(id); } }).catch(() => {});
  });
}
export function personPicker(onPick, photoId) {
  // the "People" root carries digiKam's person property too, but it is not someone a face can be named after
  // people: person tags, and single tags filed under People that digiKam has not marked as persons yet (naming a face
  // with one marks it), so an existing "People/Belcufine/Nathan Belcufine" is offered instead of a new person being made
  const people = S.META.tags.filter((t) => t.full !== 'People' && t.full.split('/')[0] === 'People' && (t.person || (!t.hasKids && t.full.split('/').length > 2)));
  const families = S.META.tags.filter((t) => !t.person && t.full.startsWith('People/') && t.full.split('/').length === 2);
  // the people of the nearby photos in the same folder, nearest in time first: most likely the same people
  let near = [], picker;
  if (photoId) api(`/api/faces/nearby?photo=${photoId}`).then((r) => { near = r.people.map((x) => x.tag).filter((id) => S.tagById.has(id)); picker.draw(); }).catch(() => {});
  const rows = (f, pick) => {
    const rank = (t) => { const k = near.indexOf(t.id); return k < 0 ? 1e9 : k; };
    const hits = people.filter((t) => !f || t._n.includes(f)).sort((a, b) => (rank(a) - rank(b)) || (b.nAll - a.nAll)).slice(0, 30);
    const nearCount = hits.filter((t) => near.includes(t.id)).length;
    return [nearCount ? h('div', { class: 'muted pphead' }, 'In nearby photos of this folder') : null,
      hits.map((t, k) => [k === nearCount && nearCount ? h('div', { class: 'muted pphead' }, 'Everyone') : null,
        h('div', { class: 'frow' + (k < nearCount ? ' near' : ''), onclick: () => pick(t) }, h('img', { src: faceUrl(t.id), loading: 'lazy', alt: '' }), h('span', { class: 'nm' }, t.name, h('span', { hidden: true }, '  ' + t.full.split('/').slice(1, -1).join(' › '))), h('span', { class: 'ct' }, nf.format(t.nAll)))]),
      f && !hits.some((t) => t._n === f) ? h('div', { class: 'frow', onclick: () => createPerson(picker.inp.value.trim()) }, icon('add'), h('span', { class: 'nm' }, `New person “${picker.inp.value.trim()}”…`)) : null];
  };
  const createPerson = (name) => {
    const fam = h('select', null, h('option', { value: 0 }, 'directly under People'), families.map((t) => h('option', { value: t.id }, t.name)));
    const sub = modal(`New person “${name}”`, h('div', { class: 'erow' }, h('span', { class: 'el' }, 'Family'), fam),
      h('p', { class: 'muted small' }, S.NO_DK ? 'Created as a person in the library.' : 'Created in digiKam’s database as a person tag.'),
      { buttons: ['Cancel', ['Create', async () => {
        let r;
        try { r = await post('/api/faces/person', { name, parent: +fam.value || (S.META.tags.find((t) => t.full === 'People')?.id || 0) }); } catch (e) { toastError(e); return; }
        sub.remove(); picker.back.remove();
        await awaitReindex((m) => m.tags.some((t) => t.id === r.id), 50, 300);
        const t = S.tagById.get(r.id); if (t) onPick(t); else toast('Person created; it will appear shortly.');
      }, 'on']] });
  };
  picker = listPicker('Who is this?', rows, onPick, { placeholder: 'Type a name…', maxHeight: '40vh', onEnter: (f, pick) => { const t = people.find((x) => x._n === f); if (t) pick(t); } });
}
/** P in the lightbox: name the first face digiKam has not named yet (its picker opens), or draw one when there is none. */
export function nameNextFace() {
  const d = LB.detail;
  if (!d || d.video) return;
  const f = faceRegions(d).find((x) => x.kind === 'unknown' || x.kind === 'suggested');
  if (!f) { startDrawFace(); return; }
  if (!LB.faces) toggleFaces();
  personPicker((t) => faceOp({ op: 'name', image: d.id, rect: [f.x, f.y, f.w, f.h], tag: t.id }), d.id);
}
export function startDrawFace() {
  const { stage, frame } = LB.el, d = LB.detail;
  if (!d || d.video) return;
  if (!LB.faces) toggleFaces();
  toast('Drag a rectangle around the face.', null, null, 4000);
  const box = h('div', { class: 'facebox face-draw' });
  let start = null;
  const toImg = (e) => { const r = frame.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * d.w, Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) * d.h]; };
  const down = (e) => { e.preventDefault(); e.stopPropagation(); start = toImg(e); frame.append(box); stage.setPointerCapture(e.pointerId); };
  const move = (e) => { if (!start) return; const p = toImg(e); const x = Math.min(start[0], p[0]), y = Math.min(start[1], p[1]), w = Math.abs(p[0] - start[0]), hh = Math.abs(p[1] - start[1]); box.style.cssText = `left:${x / d.w * 100}%;top:${y / d.h * 100}%;width:${w / d.w * 100}%;height:${hh / d.h * 100}%`; };
  const up = (e) => {
    if (!start) return;
    const p = toImg(e); const x = Math.round(Math.min(start[0], p[0])), y = Math.round(Math.min(start[1], p[1])), w = Math.round(Math.abs(p[0] - start[0])), hh = Math.round(Math.abs(p[1] - start[1]));
    start = null; stop();
    if (w < 8 || hh < 8) { box.remove(); return; }
    personPicker((t) => { box.remove(); faceOp({ op: 'add', image: d.id, rect: [x, y, w, hh], tag: t.id }); }, d.id);
  };
  const stop = () => { stage.removeEventListener('pointerdown', down, true); stage.removeEventListener('pointermove', move, true); stage.removeEventListener('pointerup', up, true); stage.style.cursor = ''; box.remove(); if (LB.mode?.stop === stop) LB.mode = null; };
  setMode(stop);
  stage.style.cursor = 'crosshair';
  stage.addEventListener('pointerdown', down, true); stage.addEventListener('pointermove', move, true); stage.addEventListener('pointerup', up, true);
}
/** Move or resize a face box: drag inside it to move, drag a corner to resize; it stays inside the photo.
 *  Saved as a move of the region (digiKam's database and the sidecar), with undo in the edit log. */
function adjustFace(f, d) {
  const { frame } = LB.el;
  if (!d || d.video) return;
  const W = d.w, H = d.h;
  let r = { x: Math.max(0, f.x), y: Math.max(0, f.y), w: Math.min(f.w, W), h: Math.min(f.h, H) };  // starts fitted inside the photo
  r.w = Math.min(r.w, W - r.x); r.h = Math.min(r.h, H - r.y);
  frame.querySelectorAll('.facebox').forEach((x) => { x.style.visibility = 'hidden'; });
  const box = h('div', { class: 'facebox face-adjust' }, ...['nw', 'ne', 'sw', 'se'].map((c) => h('i', { class: 'fh fh-' + c, 'data-c': c })));
  const place = () => { box.style.cssText = `left:${r.x / W * 100}%;top:${r.y / H * 100}%;width:${r.w / W * 100}%;height:${r.h / H * 100}%`; };
  place(); frame.append(box);
  const toImg = (e) => { const b = frame.getBoundingClientRect(); return [(e.clientX - b.left) / b.width * W, (e.clientY - b.top) / b.height * H]; };
  let drag = null;
  box.addEventListener('pointerdown', (e) => { e.preventDefault(); e.stopPropagation(); drag = { c: e.target.dataset.c || 'move', p: toImg(e), r: { ...r } }; box.setPointerCapture(e.pointerId); });
  box.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const [px, py] = toImg(e), dx = px - drag.p[0], dy = py - drag.p[1], o = drag.r, min = 16;
    let { x, y, w, h: hh } = o;
    if (drag.c === 'move') { x = Math.min(Math.max(0, o.x + dx), W - o.w); y = Math.min(Math.max(0, o.y + dy), H - o.h); }
    else {
      if (drag.c.includes('w')) { x = Math.min(Math.max(0, o.x + dx), o.x + o.w - min); w = o.x + o.w - x; }
      if (drag.c.includes('e')) w = Math.min(Math.max(min, o.w + dx), W - o.x);
      if (drag.c.includes('n')) { y = Math.min(Math.max(0, o.y + dy), o.y + o.h - min); hh = o.y + o.h - y; }
      if (drag.c.includes('s')) hh = Math.min(Math.max(min, o.h + dy), H - o.y);
    }
    r = { x, y, w, h: hh }; place();
  });
  box.addEventListener('pointerup', () => { drag = null; });
  const done = () => { bar.remove(); box.remove(); frame.querySelectorAll('.facebox').forEach((x) => { x.style.visibility = ''; }); document.removeEventListener('keydown', esc, true); if (LB.mode?.stop === done) LB.mode = null; };
  setMode(done);
  const esc = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(); } };
  document.addEventListener('keydown', esc, true);
  const bar = h('div', { class: 'adjustbar' }, h('span', null, `Adjusting ${f.name || 'this face'}: drag to move, drag a corner to resize.`),
    h('button', { class: 'pillbtn', onclick: done }, 'Cancel'),
    h('button', { class: 'pillbtn on', onclick: () => {
      const nr = [Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)];
      done();
      if (nr.join() === [f.x, f.y, f.w, f.h].join()) return;
      faceOp({ op: 'move', image: d.id, rect: [f.x, f.y, f.w, f.h], new_rect: nr });
    } }, 'Save'));
  LB.el.stage.append(bar);
}
export async function setTagIcon(t, photoId) {
  await tagOp({ op: 'icon', id: t.id, photo: photoId }, t);
  if (LB.open) { details.delete(LB.src.id[LB.i]); showItem(); }
}
