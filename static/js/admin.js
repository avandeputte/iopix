/** Settings, maintenance, the model table, consistency, face quality, the history page and faces. */
import { $, api, date, dfDay, dfMonth, fill, fmtBytes, fmtWhen, h, icon, main, nf, norm, plural, post, store } from './core.js';
import { S, THEMES, applyBadgePref, applyTheme, faceUrl, filterParams, go, hashFor, infoPrefsEditor, onRoute, photosWith, pollSoon, toast } from './state.js';
import { crumbs, suggestions } from './search.js';
import { logDialog, modal } from './actions.js';
import { dbDialog, templatesSection } from './editing.js';
import { card, pageTitle, people, setupDialog, usersSection } from './pages.js';
import { details, openLightbox, personPicker, syncLightbox } from './lightbox.js';
import { awaitReindex, indexMeta, start } from './main.js';

/** Maintenance › AI analysis and models: one table, a row per feature, with the model it runs on (download,
 *  update check) and the background job that fills it (progress, run again). Models without a job (places)
 *  and an installed search model that is not in use get rows of their own. */
export function aiSection(view) {
  const body = h('div');
  let timer = 0;
  const checkBtn = h('button', { class: 'pillbtn', onclick: async () => { checkBtn.disabled = true; checkBtn.textContent = 'Checking…'; try { await post('/api/models/check', {}); } catch (e) { toast(e.message); } draw(); } }, icon('spark'), 'Check models for updates');
  const wrap = h('div', null, h('h2', null, 'AI analysis and models'),
    h('div', { class: 'muted', style: 'max-width:860px' }, 'Everything runs locally and is stored in the viewer’s data folder; nothing is written to digiKam. New and changed photos are picked up automatically, about 20 seconds after digiKam saves them. Where a feature can run on more than one model, the menu in the Model column picks it: the default is the small, fast one, the others are more accurate and slower, each downloaded once. Models never update on their own; “Check models for updates” asks each source whether the exact files used have changed.'),
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin:14px 0' },
      h('button', { class: 'pillbtn on', onclick: async () => { try { await post('/api/analysis/run', { job: 'all' }); toast('Checking for new and changed photos…'); } catch (e) { toast(e.message); } draw(); } }, icon('spark'), 'Check new and changed photos'), checkBtn),
    body);
  const fmtDate = (t) => (t ? dfDay.format(new Date(t * 1000)) : '');
  // a job's model: fixed (vision: RapidOCR) or the one chosen for the feature (Maintenance offers the alternatives in a menu)
  const JOB_MODEL = { vision: 'ocr' };
  const JOB_CHOICE = { embeddings: 'search_model', objects: 'object_model', faces: 'face_vectors', facedetect: 'face_detector', captions: 'caption_model' };
  const modelState = (m, p) => {
    const c = m.check;
    return p.running === m.key ? ['downloading…', 'on']
      : !m.installed ? ['not installed', 'warn']
      : !c ? [`installed ${fmtDate(m.at)}`, '']
      : c.status === 'current' ? ['up to date', 'ok']
      : c.status === 'update' ? [`update available (${c.remote})`, 'on']
      : c.status === 'newer' ? ['newer data available', 'on']
      : c.status === 'error' ? ['could not check', 'warn'] : [`installed ${m.revision || ''}`, ''];
  };
  const draw = async () => {
    let a, md;
    try { [a, md] = await Promise.all([api('/api/analysis'), api('/api/models')]); } catch { return; }
    if (S.route.view !== view) return;
    const q = a.queue || {}, p = md.progress || {};
    if (p.finished && !p.running && p.error) toast(`Download of ${p.finished} failed: ${p.error}`, null, null, 9000);
    const models = Object.fromEntries(md.models.map((m) => [m.key, m]));
    const choices = md.choices || {};
    // the menu of models a feature can run on; picking one that is not installed offers its download first
    const chooser = (group) => {
      const c = choices[group];
      if (!c) return null;
      const sel = h('select', { class: 'mpick', title: `Which model ${c.label.toLowerCase()} runs on`, onchange: async (e) => {
        const key = e.target.value, m = models[key];
        if (!m) return;
        if (!m.installed) {
          e.target.value = c.chosen;
          if (!confirm(`${m.label} is not installed. Download it (${m.size})? The viewer keeps working meanwhile; choose it here again once it has arrived.`)) return;
          try { await post('/api/models/download', { key }); toast(`Downloading ${m.label}…`); } catch (err) { toast(err.message); }
          draw(); return;
        }
        const note = group === 'search_model' ? ' The library is indexed again with it in the background; search stays on the current model until that finishes.'
          : group === 'face_vectors' ? ' Every face is embedded again with it (the groups and suggestions rebuild in the background).'
          : group === 'face_detector' ? ' Used by the next face detection run.'
          : ' New photos use it from now on; “Run again” redoes the whole library with it.';
        if (!confirm(`Switch ${c.label.toLowerCase()} to ${m.label}?${note}`)) { e.target.value = c.chosen; return; }
        try { await post('/api/settings', { [group]: key }); toast(`${c.label}: now ${m.label}.`); } catch (err) { e.target.value = c.chosen; toast(err.message); }
        draw();
      } }, c.options.map((k) => h('option', { value: k, selected: k === c.chosen }, models[k] ? models[k].label + (models[k].installed ? '' : ' (not installed)') : k)));
      return sel;
    };
    // what each choice is: size, speed, accuracy (the registry's own words), the chosen one marked
    const compare = (group) => {
      const c = choices[group];
      if (!c || c.options.length < 2) return null;
      // the table is redrawn while jobs run; a fold the user opened stays open until they close it
      return h('details', { class: 'mcompare', open: OPEN_COMPARE.has(group), ontoggle: (e) => { if (e.target.open) OPEN_COMPARE.add(group); else OPEN_COMPARE.delete(group); } }, h('summary', null, `Compare the ${c.options.length} models`),
        h('div', { class: 'mopts' }, c.options.map((k) => { const o = models[k]; if (!o) return null;
          return h('div', { class: 'mopt' + (k === c.chosen ? ' on' : '') },
            h('div', null, h('b', null, o.label), h('span', { class: 'muted' }, ` · ${o.size}${k === c.chosen ? ' · in use' : o.installed ? ' · installed' : ' · not installed'}`),
              ' ', h('a', { class: 'lnk', href: o.page, target: '_blank', rel: 'noopener' }, 'about')),
            h('div', { class: 'muted' }, o.what)); })));
    };
    const modelCell = (m, group) => {
      if (!m) return h('td', { class: 'muted' }, 'built in');
      const [st, cls] = modelState(m, p);
      return h('td', null, h('div', null, group ? chooser(group) : h('a', { class: 'lnk', href: m.page, target: '_blank', rel: 'noopener', title: m.what }, m.label), h('span', { class: 'muted' }, ` · ${m.size}`)),
        h('div', { class: 'mst ' + cls, title: m.revision ? `revision ${m.revision}` : '' }, st, group ? [' · ', h('a', { class: 'lnk', href: m.page, target: '_blank', rel: 'noopener', title: m.what }, 'about')] : null),
        group ? h('div', { class: 'muted mwhat' }, m.what) : null,
        group ? compare(group) : null);
    };
    const modelBtn = (m) => {
      if (!m) return null;
      const c = m.check, upd = c && (c.status === 'update' || c.status === 'newer' || c.status === 'missing');
      if (m.installed && !upd) return null;  // installed and current: nothing to offer
      return h('button', { class: 'pillbtn small' + (!m.installed || upd ? ' on' : ''), disabled: !!p.running, title: m.installed ? `Download ${m.label} again (${m.size})` : `Download ${m.label} (${m.size})`,
        onclick: async () => {
          if (!confirm(`Download ${m.label} (${m.size})${m.installed ? ' again, replacing the installed files' : ''}? The viewer keeps working meanwhile.` + (m.kind === 'pip' ? '\n\nA package update takes effect after a restart of the viewer.' : ''))) return;
          try { await post('/api/models/download', { key: m.key }); toast(`Downloading ${m.label}…`); } catch (e) { toast(e.message); }
          draw();
        } }, m.installed ? 'Update' : 'Download');
    };
    const progressCell = (j, m, extra) => {
      if (!j) return h('td', { class: 'muted' }, '—');
      const state = !j.enabled ? (m && !m.installed ? 'needs its model' : 'off') : j.running ? 'running…' : j.queued ? 'queued' : j.done >= j.total ? 'up to date' : j.key === 'facedetect' ? 'on request' : 'waiting';
      const now = Date.now() / 1000, idle = j.running && j.since ? (j.last ? now - j.last : now - j.since) : 0, stuck = idle > 300;
      return h('td', { class: 'mprog' },
        h('div', { class: 'ahead' }, h('span', { class: j.running ? 'mst on' : j.done >= j.total && j.enabled ? 'mst ok' : 'mst' }, state), h('span', { class: 'sp' }), h('span', { class: 'muted' }, `${nf.format(Math.min(j.done, j.total))} / ${nf.format(j.total)}`)),
        h('div', { class: 'bar' }, h('i', { style: `width:${j.total ? Math.min(100, 100 * j.done / j.total).toFixed(1) : 0}%` })),
        j.running && j.since ? h('div', { class: stuck ? 'warn' : 'muted', style: 'font-size:11.5px;margin-top:3px' }, `${Math.round((now - j.since) / 60)} min · last progress ${idle < 90 ? Math.round(idle) + ' s' : Math.round(idle / 60) + ' min'} ago`,
          j.current && j.current.length ? ` · ${j.current.map((x) => x.split('/').pop()).join(', ')}` : '', stuck ? ' · probably stuck on that file: see the server log, or restart the viewer' : '') : null,
        extra);
    };
    // face detection writes to digiKam's database, so it never starts by itself: a Run link for the photos not looked at yet
    const runNow = (j) => j && j.key === 'facedetect' && j.enabled && !j.running && j.done < j.total ? h('a', { class: 'lnk mrerun', style: 'margin-right:12px', title: 'Look for faces in every photo not looked at yet', onclick: async () => {
      if (!confirm(`Look for faces in the ${nf.format(j.total - j.done)} photos not checked yet? New faces are added to digiKam’s database as unnamed faces; faces already marked are kept as they are.`)) return;
      try { const r = await post('/api/analysis/run', { job: 'facedetect' }); toast(`Looking for faces in ${nf.format(r.queued)} photos.`); } catch (e) { toast(e.message); }
      draw();
    } }, 'Run') : null;
    const rerun = (j) => j && j.rerun && j.enabled ? h('a', { class: 'lnk mrerun', title: `Discard the results of “${j.label}” and compute them again for every photo`, onclick: async () => {
      if (!confirm(`Run “${j.label}” again from scratch for all ${nf.format(j.total)} photos? The current results for it are discarded first. This can take from several minutes to a few hours.`)) return;
      try { await post('/api/analysis/run', { job: j.key, reset: true }); toast(`Re-running “${j.label}”.`); } catch (e) { toast(e.message); }
      draw();
    } }, 'Run again') : null;
    const row = (label, detail, j, m, group) => h('tr', { 'data-job': j?.key || '', 'data-model': m?.key || '' },
      h('td', { class: 'mfeat' }, h('b', null, label), detail ? h('div', { class: 'muted mdet', title: detail }, detail) : null),
      modelCell(m, group), progressCell(j, m, [runNow(j), rerun(j)]), h('td', { class: 'macts' }, modelBtn(m)));
    const rows = a.jobs.map((j) => {
      const group = JOB_CHOICE[j.key];
      const m = group ? models[choices[group]?.chosen] : models[JOB_MODEL[j.key]];
      return row(j.label, j.detail, j, m, group && choices[group] ? group : null);
    });
    const inMenus = new Set(Object.values(choices).flatMap((c) => c.options));
    for (const m of md.models) {
      if (inMenus.has(m.key) || m.key === 'ocr') continue;
      const label = m.key === 'geonames' ? 'Places from GPS positions' : m.label;
      const detail = m.key === 'geonames' ? 'Town, region and country for each GPS position, offline; looked up when the search index is built.' : m.what;
      rows.push(row(label, detail, null, m));
    }
    fill(body,
      h('div', { class: 'muted', style: 'font-size:13px;margin-bottom:8px' }, h('b', { style: 'color:var(--fg)' }, 'Background work: '), q.running ? `running ${(a.jobs.find((j) => (j.key === 'quality' && q.running === 'pixels') || j.key === q.running) || {}).label || q.running}` : 'idle',
        `. Jobs run one at a time, in the order below, each with up to ${q.threads} of ${q.cores || '?'} cores (change that in Settings).`, q.queued?.length ? ` Waiting: ${q.queued.length}.` : ''),
      h('div', { class: 'mwrap' }, h('table', { class: 'mtable' },
        h('thead', null, h('tr', null, ['Feature', 'Model', 'Progress', ''].map((c) => h('th', null, c)))), h('tbody', null, rows))));
    checkBtn.disabled = false; fill(checkBtn, icon('spark'), 'Check models for updates');
    clearTimeout(timer);
    if (a.jobs.some((j) => j.running) || p.running) timer = setTimeout(() => { if (S.route.view === view) drawSafe(); }, 4000);
  };
  const drawRaw = draw;
  const drawSafe = async () => {  // a menu the user has open is not replaced under their pointer; try again shortly
    if (body.contains(document.activeElement) && document.activeElement.matches('select')) { clearTimeout(timer); timer = setTimeout(drawSafe, 1500); return; }
    return drawRaw();
  };
  drawSafe();
  return wrap;
}
export function analysisSection(view, part = 'jobs') {
  // part 'jobs': the job list with progress (Maintenance); 'prefs': the search model and the core budget (Settings)
  const body = h('div');
  const wrap = h('div', null,
    ...(part === 'jobs' ? [h('h2', null, 'AI analysis'),
    h('div', { class: 'muted', style: 'max-width:820px' }, 'Everything here runs locally, wherever the viewer is running, and is stored in its own data folder. New and changed photos are picked up automatically, about 20 seconds after digiKam saves them; “Check for new photos now” does the same on demand.'),
    h('div', { style: 'margin:14px 0' }, h('button', { class: 'pillbtn on', onclick: async () => { try { await post('/api/analysis/run', { job: 'all' }); toast('Checking for new and changed photos…'); } catch (e) { toast(e.message); } draw(); } }, icon('spark'), 'Check for new photos now')),] : [h('h2', null, 'Search and background work')]),
    body);
  const draw = async () => {
    let d;
    try { d = await api('/api/analysis'); } catch { return; }
    if (S.route.view !== view) return;
    const q = d.queue || {};
    const sel = h('select', { onchange: async (e) => { try { await post('/api/settings', { bg_threads: +e.target.value }); toast('Applies when the next job starts.'); } catch (err) { toast(err.message); } } },
      Array.from({ length: Math.max(1, (q.cores || 4) - 1) }, (_, i) => h('option', { value: i + 1, selected: i + 1 === q.threads }, String(i + 1))));
    fill(body, h('div', { class: 'ajob', style: 'border-top:0;padding-top:0' },
      h('div', { class: 'ahead' }, h('b', null, 'Background work'), h('span', { class: 'muted' }, q.running ? `running: ${(d.jobs.find((j) => (j.key === 'quality' && q.running === 'pixels') || j.key === q.running) || {}).label || q.running}` : 'idle')),
      h('div', { class: 'muted', style: 'font-size:13px;margin-top:6px' }, ...(part === 'prefs'
        ? ['Jobs run one at a time, in a fixed order, so the viewer and other apps stay responsive. Each job may use up to ', sel, ` of ${q.cores || '?'} cores.`]
        : [`Jobs run one at a time, in the order below. Each may use up to ${q.threads} of ${q.cores || '?'} cores (change that in Settings).`, q.queued?.length ? ` Waiting: ${q.queued.length}.` : '']))),
      part === 'jobs' ? d.jobs.map((j) => h('div', { class: 'ajob' },
      h('div', { class: 'ahead' }, h('b', null, j.label), h('span', { class: 'muted' }, !j.enabled ? 'not installed' : j.running ? 'running…' : j.queued ? 'queued' : j.done >= j.total ? 'up to date' : 'waiting'),
        h('span', { class: 'sp' }), h('span', null, `${nf.format(Math.min(j.done, j.total))} / ${nf.format(j.total)}`)),
      h('div', { class: 'bar' }, h('i', { style: `width:${j.total ? Math.min(100, 100 * j.done / j.total).toFixed(1) : 0}%` })),
      j.running && j.since ? (() => { const now = Date.now() / 1000, idle = j.last ? now - j.last : now - j.since, stuck = idle > 300;
        return h('div', { class: stuck ? 'warn' : 'muted', style: 'font-size:12px;margin-top:4px' }, `running for ${Math.round((now - j.since) / 60)} min · last progress ${idle < 90 ? Math.round(idle) + ' s' : Math.round(idle / 60) + ' min'} ago`,
          j.current && j.current.length ? ` · working on ${j.current.map((p) => p.split('/').pop()).join(', ')}` : '',
          stuck ? ' · no progress for over 5 minutes: probably stuck on that file; see the server log (journalctl -u iopix, docker logs, or data/server.log) and consider restarting the viewer from Maintenance' : ''); })() : null,
      h('div', { class: 'muted', style: 'font-size:13px;margin-top:6px' }, j.detail, ' ',
        j.rerun && j.enabled ? h('a', { class: 'lnk', onclick: async () => {
          if (!confirm(`Run “${j.label}” again from scratch for all ${nf.format(j.total)} photos? The current results for it are discarded first. This can take from several minutes to a few hours.`)) return;
          try { await post('/api/analysis/run', { job: j.key, reset: true }); toast(`Re-running “${j.label}”.`); } catch (e) { toast(e.message); }
          draw();
        } }, 'Run again from scratch') : null))) : null);
    const others = (d.models || []).filter((m) => m.installed && !m.chosen);
    if (others.length && part === 'prefs') body.append(h('div', { class: 'ajob' },
      h('div', { class: 'ahead' }, h('b', null, 'Search model'), h('span', { class: 'muted' }, `using ${d.models.find((m) => m.active)?.label || 'none'}`)),
      h('div', { class: 'muted', style: 'font-size:13px;margin-top:6px' },
        others.map((other) => h('div', { style: 'margin-bottom:4px' },
          other.key === 'siglip2'
            ? 'SigLIP 2 base is also installed. Measured on 2,160 photos against their tags it was slightly better on average than CLIP (better for animals, zoo, costumes; worse for weddings, bowling, food), and it indexes about 4 times slower. '
            : other.key === 'jinaclip' ? 'Jina CLIP v1 is also installed: a larger CLIP that understands longer, more natural queries; it indexes about 3 times slower than CLIP. '
            : `${other.label} is also installed. `,
          'Switching re-indexes the whole library in the background when that model has no index yet; search stays on the current model until it finishes, and you can switch back at any time. ',
          h('a', { class: 'lnk', onclick: async () => {
            if (!confirm(`Switch content search to ${other.label}?`)) return;
            try { await post('/api/settings', { search_model: other.key }); toast(`Switching to ${other.label}.`); } catch (e) { toast(e.message); }
            draw();
          } }, `Switch to ${other.label}`))))));
    if (part === 'jobs' && d.jobs.some((j) => j.running)) setTimeout(() => { if (S.route.view === view) draw(); }, 4000);
  };
  draw();
  return wrap;
}

export const SRC_LABEL = { db: 'Database', sidecar: 'Sidecar', file: 'Image file' };
export const FIELD_LABEL = { rating: 'Rating', title: 'Title', caption: 'Caption', tags: 'Tags', taken: 'Date', gps: 'Location' };
export async function showConsistency() {
  let field = '', offset = 0, selected = new Set(), d, wasRunning = false, pollT = 0;
  const head = h('div'), list = h('div'), more = h('button', { class: 'pillbtn', style: 'margin:16px 0', onclick: () => load(true) }, 'Show more');
  const fmtv = (f, v, na) => na ? h('i', { class: 'muted', title: 'This file type cannot hold metadata; the sidecar carries it' }, 'n/a') : v == null ? h('i', { class: 'muted' }, 'absent') : f === 'tags' ? (v.length ? h('span', null, v.map((t) => h('span', { class: 'ichip', style: 'height:24px;font-size:12px;margin:1px' }, t.split('/').pop()))) : h('i', { class: 'muted' }, 'none'))
    : f === 'rating' ? (v ? '★'.repeat(v) : h('i', { class: 'muted' }, 'none')) : (v || h('i', { class: 'muted' }, 'empty'));
  const item = (r) => {
    const cb = h('input', { type: 'checkbox', checked: selected.has(r.id), onchange: (e) => { e.target.checked ? selected.add(r.id) : selected.delete(r.id); bar(); } });
    if (field === 'nosidecar') return h('div', { class: 'crec slim' + (selected.has(r.id) ? ' sel' : '') },
      h('div', { class: 'chead' }, S.EDITING ? cb : null, h('img', { src: `/thumb/${r.id}`, loading: 'lazy', alt: '', onclick: () => openLightbox({ id: [r.id] }, 0, true) }),
        h('div', { style: 'flex:1;min-width:0' }, h('b', null, r.name), h('div', { class: 'muted', style: 'font-size:12px' }, r.path.replace(/^.*\/photos\//, ''))),
        S.EDITING ? h('button', { class: 'pillbtn', style: 'height:30px', onclick: () => makeSidecars([r.id]) }, 'Create sidecar') : null));
    return h('div', { class: 'crec' + (selected.has(r.id) ? ' sel' : '') },
      h('div', { class: 'chead' }, S.EDITING ? cb : null, h('img', { src: `/thumb/${r.id}`, loading: 'lazy', alt: '', onclick: () => openLightbox({ id: [r.id] }, 0, true) }),
        h('div', null, h('b', null, r.name), h('div', { class: 'muted', style: 'font-size:12px' }, r.path.replace(/^.*\/photos\//, ''), !r.has_sidecar ? ' · no sidecar' : '', !r.file_xmp ? (r.file_editable ? ' · no metadata in the file' : ' · file type cannot hold metadata') : ''))),
      h('table', { class: 'ctable' }, h('tr', null, h('th'), ...['db', 'sidecar', 'file'].map((s) => h('th', null, SRC_LABEL[s]))),
        ...r.mismatch.map((f) => h('tr', null, h('td', null, FIELD_LABEL[f] || f), ...['db', 'sidecar', 'file'].map((s) => h('td', null, fmtv(f, r.fields[f][s], s === 'file' && !r.file_xmp && !r.file_editable)))))),
      S.EDITING ? h('div', { class: 'cfix' }, h('span', { class: 'muted' }, 'Fix this photo: take the values from the'),
        ...['db', 'sidecar', 'file'].filter((s) => r.mismatch.some((f) => r.fields[f][s] != null)).map((s) => h('button', { class: 'pillbtn', style: 'height:30px', onclick: () => fixOne(r, s) }, SRC_LABEL[s].toLowerCase()))) : null);
  };
  // photos without a sidecar: write one from digiKam's database (rating, title, caption, tags), as digiKam would
  const makeSidecars = async (ids) => {
    if (ids.length > 1 && !confirm(`Create an XMP sidecar for ${nf.format(ids.length)} photos, with the rating, title, caption and tags from digiKam’s database? Nothing else changes.`)) return;
    toast(`Creating ${plural(ids.length, 'sidecar')}…`, null, null, 60000);
    let done = 0, skipped = [];
    for (let i = 0; i < ids.length; i += 200) {
      try { const r = await post('/api/consistency/sync', { ids: ids.slice(i, i + 200), source: 'db', targets: ['sidecar'], fields: Object.keys(FIELD_LABEL) }); done += r.synced; skipped = skipped.concat(r.skipped); }
      catch (e) { toast(e.message, null, null, 9000); break; }
    }
    toast(`Created ${plural(done, 'sidecar')}${skipped.length ? `; ${nf.format(skipped.length)} skipped (${skipped[0].reason})` : ''}.`, null, null, 9000);
    selected.clear(); bar(); setTimeout(load, 2500);
  };
  // one photo: copy the chosen source's values to the other places that can hold them
  const fixOne = async (r, src) => {
    const targets = ['db', 'sidecar', 'file'].filter((t) => t !== src && (t !== 'file' || r.file_editable));
    if (!confirm(`Copy ${r.mismatch.map((f) => (FIELD_LABEL[f] || f).toLowerCase()).join(', ')} from the ${SRC_LABEL[src].toLowerCase()} to the ${targets.map((t) => SRC_LABEL[t].toLowerCase()).join(' and ')} for ${r.name}?` + (targets.includes('db') ? '\n\nThis writes into digiKam’s database.' : ''))) return;
    try { const res = await post('/api/consistency/sync', { ids: [r.id], source: src, targets, fields: r.mismatch }); toast(res.synced ? `Fixed ${r.name}.` : `Not fixed: ${res.skipped[0]?.reason || 'nothing to copy'}`, null, null, 8000); } catch (e) { toast(e.message, null, null, 9000); return; }
    load();
  };
  // the bulk bar: fixed at the bottom of the page, wraps on narrow screens. One click per source ("take the values
  // from"), applied to the other places that can hold them; the detailed choice (fields, targets) is one click away.
  const syncBar = h('div', { class: 'cbulk', id: 'cbulk', hidden: true });
  let advanced = false;
  const runSync = async (src, targets, fields) => {
    const n = selected.size;
    if (!targets.length) { toast('Pick at least one place to copy to, other than the source.'); return; }
    if (!confirm(`Copy ${fields.map((f) => (FIELD_LABEL[f] || f).toLowerCase()).join(', ')} from the ${SRC_LABEL[src].toLowerCase()} to the ${targets.map((t) => SRC_LABEL[t].toLowerCase()).join(' and ')} for ${nf.format(n)} ${n === 1 ? 'photo' : 'photos'}?` +
      (targets.includes('db') ? '\n\nThis writes into digiKam’s database.' : '') + (targets.includes('file') ? '\n\nImage files are rewritten in place (metadata only; pixels untouched); the original of each is kept in the viewer’s file-backups folder. Files that cannot hold metadata are skipped.' : '') + '\n\nSidecars are backed up before they change.')) return;
    fill(syncBar, h('span', { class: 'n' }, `Syncing ${nf.format(n)} photos…`));
    try {
      const r = await post('/api/consistency/sync', { ids: [...selected], source: src, targets, fields });
      toast(`Synced ${nf.format(r.synced)} ${r.synced === 1 ? 'photo' : 'photos'}${r.skipped.length ? `; ${nf.format(r.skipped.length)} skipped (${r.skipped[0].reason})` : ''}.`, null, null, 9000);
    } catch (e) { toast(e.message, null, null, 9000); bar(); return; }
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
        h('a', { class: 'lnk', onclick: () => { advanced = !advanced; bar(); } }, advanced ? 'Fewer options' : 'Choose fields and places…')),
      advanced ? h('div', { class: 'cbrow' },
        h('span', null, 'Copy from ', src, ' to '), h('span', { class: 'cbchecks' }, tg), h('span', { class: 'muted' }, 'fields:'), h('span', { class: 'cbchecks' }, fl),
        h('button', { class: 'pillbtn on', onclick: () => runSync(src.value, tg.map((l) => l.firstChild).filter((c) => c.checked && c.value !== src.value).map((c) => c.value),
          fl.map((l) => l.firstChild).filter((c) => c.checked).map((c) => c.value)) }, icon('spark'), 'Sync')) : null);
  };
  const load = async (append) => {
    if (!append) offset = 0;
    try { d = await api(`/api/consistency?field=${field}&offset=${offset}&limit=60`); } catch (e) { list.replaceChildren(h('div', { class: 'empty' }, e.message)); return; }
    if (S.route.view !== 'consistency') return;
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
        h('span', { class: 'muted', style: 'font-size:13px;font-weight:400;margin-left:10px' }, nf.format(d.total)),
        h('span', { class: 'sp' }),
        S.EDITING && d.total ? h('a', { class: 'lnk', style: 'font-size:13px;font-weight:400', onclick: async () => {  // every photo listed (on this filter), not only the ones shown
          try { const all = await api(`/api/consistency?field=${field}&offset=0&limit=5000`); all.items.forEach((r) => selected.add(r.id)); } catch (e) { toast(e.message); return; }
          bar(); load();
        } }, `Select all ${nf.format(d.total)}`) : null) : null,
      field === 'nosidecar' && d.total ? h('p', { class: 'muted', style: 'margin-top:0' }, 'These photos keep their metadata only in digiKam’s database (and the file, when it can hold it). A sidecar is not required, but digiKam and other programs read it, and the viewer writes edits there. Creating one copies the rating, title, caption and tags from digiKam’s database.') : null,
      p.error && !p.failed ? h('p', { class: 'warn' }, 'Last error: ' + p.error) : null);
    // a scan that just finished: say so once
    if (wasRunning && !p.running) toast(p.failed ? `Check finished with errors: ${nf.format(p.failed)} photos could not be compared.` : `Check finished: ${nf.format(s.scanned)} photos compared, ${nf.format(s.mismatched)} disagree.`, null, null, 8000);
    wasRunning = p.running;
    if (!append) list.replaceChildren();
    if (!d.items.length && !offset) list.append(h('div', { class: 'empty' }, !s.scanned ? '' : field === 'nosidecar' ? 'Every photo has a sidecar.' : 'Everything that was checked agrees.'));
    list.append(...d.items.map(item));
    offset += d.items.length;
    more.hidden = offset >= d.total;
    clearTimeout(pollT);
    if (p.running) pollT = setTimeout(() => { if (S.route.view === 'consistency') load(); }, 3000);  // refresh the progress (and the list, from the top) until it is done
  };
  const scan = async (subset, rescanAll) => {
    const q = subset ? filterParams() : new URLSearchParams();
    q.set('rescan_all', rescanAll ? '1' : '0');
    try {
      const res = await fetch('/api/consistency/scan', { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: q.toString() });
      const r = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(r.detail || `${res.status}`);
      toast(`Checking ${nf.format(r.queued)} photos in the background.`);
    } catch (e) { toast('Could not start the check: ' + e.message, null, null, 8000); return; }
    wasRunning = true;
    load();
  };
  main.replaceChildren(h('div', { class: 'page spage cpage' }, crumbs([['Maintenance', '#/maintenance'], ['Metadata consistency', null]]),
    pageTitle('check', 'teal', 'Metadata consistency'),
    h('p', { class: 'muted', style: 'margin:0' }, 'The rating, title, caption and tags of each photo are read from digiKam’s database, its XMP sidecar and the image file, and compared. Fix a photo with its buttons, or tick several and choose the right copy in the bar at the bottom; the other places are made to match. Files that cannot hold metadata (most videos, RAW) show n/a, which is not a disagreement.'),
    h('div', { class: 'ctools' },
      h('button', { class: 'pillbtn on', onclick: () => scan(false, false) }, icon('spark'), 'Check new and changed photos'),
      h('button', { class: 'pillbtn', onclick: () => { if (confirm('Re-check every photo in the library? This reads every sidecar and image file and takes a while.')) scan(false, true); } }, 'Re-check whole library'),
      [...filterParams().keys()].length ? h('button', { class: 'pillbtn', onclick: () => scan(true, true) }, 'Check the current search only') : null),
    head, list, more), syncBar);
  load();
}

export async function placeTagsDialog(ids) {
  let p;
  try { p = await api('/api/placetags' + (ids ? '?ids=' + ids.join(',') : '')); } catch (e) { toast(e.message); return; }
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
    h('p', { class: 'muted', style: 'font-size:12.5px' }, 'Town names come from the offline place lookup and can be a neighbouring town. Tags are assigned through the normal edit path, so they reach digiKam’s database with “Apply edits to digiKam database”.'),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close'),
      h('button', { class: 'pillbtn on', disabled: !p.to_tag || p.progress.running || !!(p.missing_tags.length && p.digikamRunning), onclick: async () => {
        try { const r = await post('/api/placetags', { ids: ids || [] }); toast(`Tagging ${nf.format(r.to_tag)} photos${r.creating ? `, creating ${r.creating} tags` : ''} in the background.`); } catch (e) { toast(e.message, null, null, 9000); return; }
        const poll = async () => { try { p = await api('/api/placetags' + (ids ? '?ids=' + ids.join(',') : '')); } catch { return; } draw(); if (p.progress.running && back.isConnected) setTimeout(poll, 3000); else if (back.isConnected) { indexMeta(await api('/api/meta')); } };
        setTimeout(poll, 1500);
      } }, icon('place'), 'Create and assign')));
}

/** Faces page: the decided faces leave the page at once; the batch is written in the background, and a
 *  failure puts them back. DECIDED hides them from the queue until the index has caught up. */
export const DECIDED = new Map();  // "photo:x:y:w:h" -> when it was decided
export const faceKey = (o) => `${o.image ?? o.photo}:${(o.rect || [o.x, o.y, o.w, o.h]).join(':')}`;
export async function facesBatch(ops, msg) {
  const now = Date.now();
  ops.forEach((o) => DECIDED.set(faceKey(o), now));
  toast(`${msg}: ${plural(ops.length, 'face')}.`, null, null, 4000);
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
export function onRouteFaces() { S.viewKey = null; onRoute(); }
export async function showFaces(vp) {
  const tab = (vp && vp.get('tab')) || 'suggested';
  const page = h('div', { class: 'page' });
  const tabs = h('div', { class: 'eseg', style: 'margin:10px 0' }, [['suggested', 'Suggestions'], ['unknown', 'Unnamed faces'], ['ignored', 'Ignored']].map(([k, l]) => h('button', { class: k === tab ? 'on' : '', onclick: () => go('faces', photosWith({ tab: k })) }, l)));
  const body = h('div');
  main.replaceChildren(page);
  page.append(crumbs([['People', '#/people'], ['Faces', null]]), h('h1', null, 'Faces'), tabs, body);
  let d;
  try { d = await api(`/api/faces/queue?kind=${tab}&limit=500`); } catch (e) { body.replaceChildren(h('div', { class: 'empty' }, e.message)); return; }
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
    body.append(h('div', { class: 'muted', style: 'max-width:820px' }, `${nf.format(d.total)} faces digiKam thinks it recognised, grouped by person, like digiKam’s People view. Confirm a whole group at once, or decide face by face. Everything is written to digiKam’s database (digiKam closed; backup first).`));
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
          h('button', { class: 'pillbtn on', onclick: async () => { const chosen = faces.filter((f, i) => sel.has(i)); if (!chosen.length) { toast('Nothing selected in this group.'); return; } if (!confirm(`Confirm ${chosen.length} faces as ${name}?`)) return; if (await facesBatch(chosen.map((f) => op(f, 'confirm')), `Confirmed ${name}`)) refresh(); } }, icon('check'), 'Confirm selected'),
          h('button', { class: 'pillbtn', onclick: async () => { const chosen = faces.filter((f, i) => sel.has(i)); if (!chosen.length) return; if (!confirm(`Reject ${chosen.length} suggestions of ${name}? The faces stay as unknown.`)) return; if (await facesBatch(chosen.map((f) => op(f, 'unknown')), 'Rejected')) refresh(); } }, 'Reject selected'),
          h('button', { class: 'pillbtn', onclick: () => { sel.size === faces.length ? sel.clear() : faces.forEach((f, i) => sel.add(i)); draw(); } }, 'Toggle all')),
        grid));
    }
    return;
  }
  // unnamed faces: look-alikes grouped (SFace embeddings), each group named or ignored in one go; the viewer never proposes a name
  if (tab === 'unknown' && store.get('facegroups', '1') !== '0') {
    let g = null;
    try { g = await api('/api/faces/groups?kind=unknown'); } catch { g = null; }
    if (g && DECIDED.size) {  // decided here a moment ago: gone from the groups too
      const keep = (f) => !DECIDED.has(faceKey(f));
      g.groups = g.groups.map((grp) => ({ ...grp, faces: grp.faces.filter(keep) })).map((grp) => ({ ...grp, n: grp.faces.length })).filter((grp) => grp.faces.length);
      g.ungrouped = g.ungrouped.filter(keep);
    }
    if (S.route.view !== 'faces') return;
    if (g && (g.groups.length || g.missing.length || g.progress.running)) {
      const head = h('div', { class: 'muted', style: 'max-width:820px' }, `${nf.format(d.total)} faces digiKam found but could not name. Look-alikes are shown together so one decision covers the group, with the viewer’s own “looks like” guess when a named person matches; untick a face to leave it out. `,
        h('a', { class: 'lnk', onclick: () => { store.set('facegroups', '0'); refresh(); } }, 'Show as a plain grid'));
      const status = g.progress.running ? h('div', { class: 'enote' }, `Grouping… ${nf.format(g.progress.done)} / ${nf.format(g.progress.total)} faces compared.`)
        : g.missing.length ? h('div', { class: 'enote' }, `${nf.format(g.missing.length)} faces have not been compared yet (the Face grouping job in Maintenance runs in the background).`) : null;
      body.append(head, status);
      const nameAs = async (faces, s) => { if (!faces.length) return; if (!confirm(`Name ${faces.length} ${faces.length === 1 ? 'face' : 'faces'} as ${s.name}?`)) return; if (await facesBatch(faces.map((f) => op(f, 'name', { tag: s.tag })), `Named ${s.name}`)) refresh(); };
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
          S.CAN_MANAGE ? [h('button', { class: 'pillbtn' + (suggest ? '' : ' on'), onclick: () => personPicker(async (t) => { const c = chosen(); if (!c.length) return; if (!confirm(`Name ${c.length} faces as ${t.name}?`)) return; if (await facesBatch(c.map((f) => op(f, 'name', { tag: t.id })), `Named ${t.name}`)) refresh(); }) }, icon('person'), grouped ? 'Name these…' : 'Name selected…'),
            h('button', { class: 'pillbtn', onclick: async () => { const c = chosen(); if (!c.length) return; if (!confirm(`Ignore ${c.length} faces? They stay, marked as not a person to name.`)) return; if (await facesBatch(c.map((f) => op(f, 'ignore')), 'Ignored')) refresh(); } }, 'Ignore'),
            h('button', { class: 'pillbtn', onclick: () => { sel.size === faces.length ? sel.clear() : faces.forEach((f, i) => sel.add(i)); draw(); } }, 'Toggle all')] : null), grid);
      };
      if (g.people === 0 && !g.progress.running) body.append(h('div', { class: 'muted', style: 'font-size:13px' }, 'Suggestions appear once the named people have been compared too (the Face grouping job does that first).'));
      g.groups.forEach((grp, k) => body.append(groupBlock(grp.faces, `Look-alikes ${k + 1} · ${grp.n} faces`, true, grp.suggest)));
      if (g.ungrouped.length) body.append(groupBlock(g.ungrouped, `${nf.format(g.ungrouped.length)} faces without a look-alike`, false));
      if (g.missing.length) body.append(groupBlock(g.missing, `${nf.format(g.missing.length)} faces not compared yet`, false));
      if (g.progress.running) setTimeout(() => { if (S.route.view === 'faces') refresh(); }, 5000);
      return;
    }
  }
  // unnamed or ignored faces: a selectable grid, name or ignore many at once
  const sel = new Set();
  const grid = h('div', { class: 'fgrid' });
  if (tab === 'unknown' && store.get('facegroups', '1') === '0') body.append(h('div', { class: 'muted', style: 'font-size:13px' }, h('a', { class: 'lnk', onclick: () => { store.set('facegroups', '1'); refresh(); } }, 'Group look-alikes'), ' (needs the SFace model from Maintenance › AI analysis and models).'));
  const bar = h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0' });
  const chosen = () => d.faces.filter((f, i) => sel.has(i));
  const drawBar = () => S.CAN_MANAGE && fill(bar,
    h('span', null, `${nf.format(sel.size)} selected of ${nf.format(d.faces.length)}`),
    h('button', { class: 'pillbtn', onclick: () => { sel.size === d.faces.length ? sel.clear() : d.faces.forEach((f, i) => sel.add(i)); draw(); } }, sel.size === d.faces.length ? 'Select none' : 'Select all'),
    h('button', { class: 'pillbtn on', disabled: !sel.size, onclick: () => personPicker(async (t) => { const c = chosen(); if (!confirm(`Name ${c.length} faces as ${t.name}?`)) return; if (await facesBatch(c.map((f) => op(f, 'name', { tag: t.id })), `Named ${t.name}`)) refresh(); }) }, icon('person'), 'Name selected…'),
    tab === 'unknown' ? h('button', { class: 'pillbtn', disabled: !sel.size, onclick: async () => { const c = chosen(); if (!confirm(`Ignore ${c.length} faces? They stay, marked as not a person to name.`)) return; if (await facesBatch(c.map((f) => op(f, 'ignore')), 'Ignored')) refresh(); } }, 'Ignore selected') : null,
    tab === 'ignored' ? h('button', { class: 'pillbtn', disabled: !sel.size, onclick: async () => { const c = chosen(); if (await facesBatch(c.map((f) => op(f, 'unknown')), 'Back to unknown')) refresh(); } }, 'Back to unnamed') : null,
    h('button', { class: 'pillbtn', disabled: !sel.size, onclick: async () => { const c = chosen(); if (await facesBatch(c.map((f) => op(f, 'remove')), 'Removed')) refresh(); } }, 'Remove selected'));
  const draw = () => { fill(grid, d.faces.map((f, i) => h('div', { class: 'fcell' + (sel.has(i) ? ' sel' : ''), onclick: (e) => { if (e.target.tagName === 'IMG' && !e.shiftKey && !sel.size) return; if (!S.CAN_MANAGE) return; e.preventDefault(); sel.has(i) ? sel.delete(i) : sel.add(i); draw(); } },
    S.CAN_MANAGE ? h('label', { class: 'fchk' }, h('input', { type: 'checkbox', checked: sel.has(i), onclick: (e) => e.stopPropagation(), onchange: (e) => { e.target.checked ? sel.add(i) : sel.delete(i); draw(); } })) : null, crop(f),
    h('div', { class: 'muted', style: 'font-size:11px;text-align:center' }, dfMonth.format(date(f.ts)))))); drawBar(); };
  body.append(h('div', { class: 'muted', style: 'max-width:820px' }, tab === 'unknown' ? `${nf.format(d.total)} faces digiKam found but could not name. Tick the ones that are the same person and name them together; tick ones that are not faces to name and ignore them. Click a face to open its photo.` : `${nf.format(d.total)} faces marked as not to be named.`), bar, grid);
  draw();
  if (d.total > d.faces.length) body.append(h('p', { class: 'muted' }, `Showing the first ${nf.format(d.faces.length)}; decide these and the next ones appear.`));
}

export async function showSettings() { return settingsPage('settings'); }
export async function showMaintenance() { if (!S.IS_ADMIN) { main.replaceChildren(h('div', { class: 'empty' }, 'Maintenance is for admins. Sign in as an admin to see it.')); return; } return settingsPage('maintenance'); }
// Settings: preferences. Maintenance: library actions, job progress, models, storage. One loader, two layouts.
/** Maintenance › File scanner: on or off, how often, Scan now, and what the last pass found. */
function scannerRow(m, row, btn, again) {
  const sc = m.scanner || {};
  const last = sc.last;
  const said = last ? `Last pass ${fmtWhen(last.finished)}: ${[last.added && plural(last.added, 'new file'), last.modified && plural(last.modified, 'changed file'), last.removed && plural(last.removed, 'file') + ' gone', last.folders && plural(last.folders, 'new folder')].filter(Boolean).join(', ') || 'nothing new'}.${last.skipped?.length ? ' ' + last.skipped.join(' ') : ''}${last.error ? ' Error: ' + last.error : ''}` : 'No pass yet.';
  const on = h('input', { type: 'checkbox', checked: !!sc.enabled, onchange: async (e) => { try { await post('/api/settings', { file_scanner: e.target.checked }); toast(e.target.checked ? 'File scanner on: a first pass starts now.' : 'File scanner off.'); } catch (err) { e.target.checked = !e.target.checked; toast(err.message); } } });
  const every = h('select', { class: 'sharepick', onchange: async (e) => { try { await post('/api/settings', { scan_interval: +e.target.value }); } catch (err) { toast(err.message); } } },
    [[5, 'every 5 minutes'], [15, 'every 15 minutes'], [60, 'every hour'], [360, 'every 6 hours'], [1440, 'once a day']].map(([v, l]) => h('option', { value: v, selected: v === sc.interval }, l)));
  return row('File scanner',
    h('span', null, 'Looks in the album folders for photos and videos added, changed or removed outside the viewer (Finder, a sync tool, a phone backup), as digiKam’s scanner does, and records them the same way: camera, place, keywords, faces, digiKam’s file fingerprint. Leave it off if everything arrives through Upload or Import; those are always read in full. A disk that looks unmounted is left alone. ', h('span', { class: 'muted' }, sc.running ? 'Scanning now…' : said)),
    h('span', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' }, h('label', { style: 'display:flex;gap:6px;align-items:center;cursor:pointer' }, on, 'On'), every,
      btn('Scan now', async () => { try { await post('/api/scan', {}); toast('Scanning the album folders…'); setTimeout(again, 4000); } catch (e) { toast(e.message); } })));
}
export const OPEN_COMPARE = new Set();  // model groups whose "Compare the models" fold is open on the Maintenance page
export async function settingsPage(view) {
  let m;
  try { m = await api('/api/maintenance'); } catch (e) { if (S.route.view === view) main.replaceChildren(h('div', { class: 'empty' }, 'Could not load settings: ' + e.message)); return; }
  if (S.route.view !== view) return;
  const when = (t) => (t ? fmtWhen(t) : 'never');
  const act = async (action, msg, confirmText) => {
    if (confirmText && !confirm(confirmText)) return;
    try { await post('/api/maintenance', { action }); toast(msg); } catch (e) { toast(e.message); }
    setTimeout(() => { if (S.route.view === view) settingsPage(view); }, 1500);
  };
  const row = (label, detail, btn) => h('div', { class: 'srow' }, h('div', null, h('b', null, label), h('div', { class: 'muted', style: 'font-size:13px' }, detail)), btn);
  const btn = (label, fn, primary) => h('button', { class: 'pillbtn' + (primary ? ' on' : ''), onclick: fn }, label);
  const st = m.storage;
  main.replaceChildren(h('div', { class: 'page spage' },
    h('h1', null, view === 'settings' ? 'Settings' : 'Maintenance', h('span', { class: 'muted', style: 'font-size:13px;font-weight:400;margin-left:12px' }, `${S.APP_NAME} v${S.APP_VERSION || '?'}${S.APP_COMMIT && S.APP_COMMIT !== 'unknown' ? ' · ' + S.APP_COMMIT : ''}`)),
    h('div', { class: 'muted', style: 'max-width:820px' }, m.paths ? `Library: ${m.paths.library} (${nf.format(m.photos)} items). digiKam database: ${m.paths.digikamDb}. Everything the viewer writes lives in ${m.paths.data}.` : `${nf.format(m.photos)} items in the library.`),
    !S.USERS_EXIST ? row('No users yet', 'The viewer is open to anyone who can reach it. Create the admin account to switch on sign-in: visitors then browse read-only, and only editors and admins can change the library.', btn('Create the admin account', setupDialog, true)) : null,
    ...(view === 'settings' ? [
    h('h2', null, 'Display'),
    S.IS_ADMIN ? (() => {
      const inp = h('input', { class: 'einput', placeholder: 'iopix', value: m.appNameSet || '', maxlength: 40, style: 'margin:0;width:220px', onkeydown: (e) => { if (e.key === 'Enter') e.target.nextSibling.click(); } });
      return row('Name', 'What the viewer calls itself, for everyone: the top bar, the browser tab and the installed app. Leave it empty for iopix.',
        h('span', { style: 'display:flex;gap:6px;align-items:center' }, inp, btn('Save', async () => { try { await post('/api/settings', { app_name: inp.value }); toast('Name saved.'); pollSoon(300); } catch (e) { toast(e.message); } })));
    })() : null,
    row('Appearance', `Light (the default) or dark colours, or follow the device’s setting. ${S.USER ? 'Saved with your account, so it follows you to other devices.' : 'Remembered in this browser; sign in to keep it with an account.'}`,
      h('div', { class: 'eseg', id: 'themeseg' }, THEMES.map(([k, label]) => h('button', { class: store.get('theme', 'light') === k ? 'on' : '', 'data-theme': k, onclick: (e) => {
        store.set('theme', k); applyTheme(); for (const b of e.currentTarget.parentNode.children) b.classList.toggle('on', b === e.currentTarget); } }, label)))),
    row('Thumbnail badges', 'The small marks on thumbnails: face tags, GPS coordinates and favorites. This is remembered per browser.',
      h('label', { style: 'display:flex;gap:8px;align-items:center;cursor:pointer' }, h('input', { type: 'checkbox', checked: store.get('badges', '1') !== '0',
        onchange: (e) => { store.set('badges', e.target.checked ? '1' : '0'); applyBadgePref(); } }), 'Show badges')),
    row('Info panel', `The sections the photo info panel shows; the gear at the top of the panel opens the same choices. ${S.USER ? 'Saved with your account, so it follows you to other devices.' : 'Remembered in this browser; sign in to keep it with an account.'}`, infoPrefsEditor()),
    S.IS_ADMIN ? (() => {
      const sel = h('select', null, h('option', { value: 'osm', selected: m.mapProvider !== 'google' }, 'OpenStreetMap (no account needed)'), h('option', { value: 'google', selected: m.mapProvider === 'google' }, 'Google Maps (your API key)'));
      const keyIn = h('input', { class: 'einput', type: 'password', placeholder: m.googleMapsKeySet ? 'key is set; paste a new one to replace it' : 'Google Maps API key', autocomplete: 'off', style: 'margin:0;width:260px' });
      return row('Map provider', `Maps and place search use ${m.mapProvider === 'google' && m.googleMapsKeySet ? 'Google Maps' : 'OpenStreetMap'}. Google needs an API key from the Google Cloud console with the Map Tiles API and Geocoding API enabled and billing on; restrict the key to this viewer's address. The key is stored in the viewer's settings and never shown again.`,
        h('span', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' }, sel, keyIn, btn('Save', async () => {
          const body = { map_provider: sel.value }; if (keyIn.value.trim()) body.google_maps_key = keyIn.value.trim();
          if (sel.value === 'google' && !keyIn.value.trim() && !m.googleMapsKeySet) { toast('Enter the Google Maps API key first.'); return; }
          try { await post('/api/settings', body); S.mapsLoaded = false; toast('Map provider saved.'); } catch (e) { toast(e.message); }
          settingsPage(view);
        })));
    })() : null,
    S.IS_ADMIN ? (() => {
      const d = m.database || {};
      const inp = (k, ph, type = 'text', w = 160) => h('input', { class: 'einput', type, placeholder: ph, value: type === 'password' ? '' : (d[k] ?? ''), autocomplete: 'off', style: `margin:0;width:${w}px`, onkeydown: (e) => e.stopPropagation() });
      const type = h('select', null, h('option', { value: 'sqlite', selected: d.type !== 'mysql' }, 'SQLite (digikam4.db file)'), h('option', { value: 'mysql', selected: d.type === 'mysql' }, 'MySQL / MariaDB'));
      const f = { sqlite_dir: inp('sqlite_dir', 'folder with digikam4.db', 'text', 320), host: inp('host', 'host'), port: inp('port', 'port', 'text', 70), database: inp('database', 'database'), user: inp('user', 'user'),
        password: inp('password', d.password_set ? 'password (stored; leave empty to keep)' : 'password', 'password', 220) };
      const backup = h('select', null, [['auto', 'full backup only before deleting photos or a tag, or a merge'], ['skip', 'never (the server is backed up elsewhere)']].map(([v, l]) => h('option', { value: v, selected: (d.backup === 'skip' ? 'skip' : 'auto') === v }, l)));
      const sq = h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;margin-top:6px' }, f.sqlite_dir), my = h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;margin-top:6px' }, f.host, f.port, f.database, f.user, f.password);
      const show = () => { sq.hidden = type.value !== 'sqlite'; my.hidden = type.value !== 'mysql'; }; type.onchange = show; show();
      const payload = () => ({ type: type.value, sqlite_dir: f.sqlite_dir.value, host: f.host.value, port: f.port.value, database: f.database.value, user: f.user.value, password: f.password.value, backup: backup.value });
      const result = h('div', { class: 'muted', style: 'font-size:12.5px;margin-top:6px' });
      return row('digiKam database', `Where digiKam keeps its database. Currently ${d.type === 'mysql' ? `MySQL ${d.user}@${d.host}:${d.port}/${d.database}` : `SQLite in ${d.sqlite_dir}`}. Changing it rebuilds the index from the other database within seconds; the viewer's own data (favorites, albums, users, analysis) stays. Test first.`,
        h('div', null, h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' }, type, backup), sq, my,
          h('div', { style: 'display:flex;gap:6px;margin-top:8px' },
            btn('Test connection', async () => { fill(result, 'Testing…'); try { const r = await post('/api/db/test', payload()); fill(result, `OK: ${nf.format(r.images)} photos, ${nf.format(r.tags)} tags, DBVersion ${r.dbVersion}, album root ${r.roots.join(', ')}`); } catch (e) { fill(result, e.message); } }),
            btn('Save and switch', async () => { if (!confirm('Switch the viewer to this digiKam database now?')) return; try { await post('/api/settings', { database: payload() }); toast('Database settings saved; the index is rebuilding.'); } catch (e) { toast(e.message); return; } setTimeout(() => settingsPage(view), 2500); }, true)),
          result));
    })() : null,
    S.IS_ADMIN ? (() => {
      const body = h('div', { class: 'muted', style: 'font-size:12.5px' }, 'Loading…');
      const draw = async () => {
        let d; try { d = await api('/api/roots'); } catch (e) { fill(body, e.message); return; }
        const inputs = new Map();
        fill(body, h('div', { style: 'display:grid;gap:8px' }, d.roots.map((r) => {
          const inp = h('input', { class: 'einput', placeholder: r.localPath, value: r.overridden ? r.localPath : '', style: 'margin:0;width:340px', onkeydown: (e) => e.stopPropagation() });
          inputs.set(r.digikamPath, inp);
          return h('div', null, h('div', null, h('b', null, r.label || 'Album root'), ' ', h('span', { class: 'muted' }, `digiKam: ${r.digikamPath}`)),
            h('div', { style: 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:4px' }, inp,
              h('span', { class: 'muted', style: 'font-size:12px' }, r.exists ? `found: ${r.localPath} (${r.sample >= 2000 ? '2,000+' : nf.format(r.sample)} files)` : `not found: ${r.localPath}`)));
        }), h('div', { style: 'display:flex;gap:6px' },
          btn('Check', draw),
          btn('Save', async () => { const m = {}; inputs.forEach((inp, k) => { m[k] = inp.value.trim(); }); try { await post('/api/settings', { album_roots: m }); toast('Album roots saved; the index is rebuilding.'); } catch (e) { toast(e.message); return; } setTimeout(draw, 2500); }, true))));
      };
      draw();
      return row('Album roots', `Where each album root digiKam knows is mounted here. Leave a field empty to use the default (PHOTOS_ROOT for a single-root library, else the path digiKam recorded); enter a path when the folder is mounted elsewhere, e.g. /photos in a container. Changing it rebuilds the index.`, body);
    })() : null,
    S.IS_ADMIN ? (() => {  // the viewer's own database: SQLite file (default) or MySQL/MariaDB, with a one-time copy of the data
      const body = h('div', { class: 'muted', style: 'font-size:13px' }, 'Loading…');
      const wrap = row('Viewer database', 'Where the viewer keeps its own data: users, albums, favorites, settings, pending edits and the AI results. SQLite in the data folder by default, or a MySQL/MariaDB database of its own (not digiKam’s). Switching copies every row across and restarts the viewer; the configuration is kept in data/viewer-db.json, outside the database.', body);
      (async () => {
        let d; try { d = await api('/api/viewerdb'); } catch (e) { fill(body, e.message); return; }
        const inp = (k, ph, type = 'text', w = 160) => h('input', { class: 'einput', type, placeholder: ph, value: type === 'password' ? '' : (d[k] ?? ''), autocomplete: 'off', style: `margin:0;width:${w}px`, onkeydown: (e) => e.stopPropagation() });
        const type = h('select', null, h('option', { value: 'sqlite', selected: d.type !== 'mysql' }, 'SQLite file (viewer.db in the data folder)'), h('option', { value: 'mysql', selected: d.type === 'mysql' }, 'MySQL / MariaDB'));
        const f = { host: inp('host', 'host'), port: inp('port', 'port', 'text', 70), database: inp('database', 'database'), user: inp('user', 'user'), password: inp('password', d.password_set ? 'password (stored; leave empty to keep)' : 'password', 'password', 220) };
        const my = h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;margin-top:6px' }, f.host, f.port, f.database, f.user, f.password);
        const copy = h('input', { type: 'checkbox', checked: true }), drop = h('input', { type: 'checkbox', checked: true });
        const opts = h('div', { style: 'display:grid;gap:4px;margin-top:8px;font-size:13px' },
          h('label', { style: 'display:flex;gap:6px;align-items:center;cursor:pointer' }, copy, 'Copy all the data from the current database to the new one'),
          h('label', { style: 'display:flex;gap:6px;align-items:center;cursor:pointer' }, drop, 'Drop and recreate the viewer’s tables at the target first (a clean slate; anything already there is lost)'));
        const show = () => { my.hidden = type.value !== 'mysql'; }; type.onchange = show; show();
        const payload = () => ({ type: type.value, host: f.host.value, port: f.port.value, database: f.database.value, user: f.user.value, password: f.password.value });
        const result = h('div', { class: 'muted', style: 'font-size:12.5px;margin-top:6px' });
        fill(body, h('div', null, `Currently: ${d.type === 'mysql' ? `MySQL ${d.user}@${d.host}:${d.port}/${d.database}` : `SQLite file ${d.sqlite_file}`}`, d.error ? h('span', { class: 'warn' }, ` · the configured MySQL database could not be used (${d.error}); the SQLite file is in use meanwhile`) : null),
          h('div', { style: 'margin-top:8px' }, type), my, opts,
          h('div', { style: 'display:flex;gap:6px;margin-top:8px' },
            btn('Test connection', async () => { fill(result, 'Testing…'); try { const r = await post('/api/viewerdb/test', payload()); const t = Object.entries(r.tables); fill(result, `OK: ${r.version}. ${t.length ? 'Viewer tables already there: ' + t.map(([k, n]) => `${k} (${nf.format(n)})`).join(', ') : 'No viewer tables there yet.'}`); } catch (e) { fill(result, e.message); } }),
            btn('Switch', async () => {
              const p = payload();
              if (!confirm(`Switch the viewer to ${p.type === 'mysql' ? `MySQL ${p.user}@${p.host}:${p.port}/${p.database}` : 'the SQLite file'}?` + (copy.checked ? '\n\nEvery row is copied there first' + (drop.checked ? ', after dropping and recreating the viewer’s tables at the target.' : ' (existing rows with the same ids are replaced).') : '\n\nThe data is NOT copied; the viewer starts empty there unless the tables already hold data.') + '\n\nThe viewer restarts when the copy is done; the page reloads by itself.')) return;
              fill(result, 'Copying… this can take a minute on a large library.');
              try { const r = await post('/api/viewerdb/switch', { ...p, copy: copy.checked, drop: drop.checked }); fill(result, `Switched to ${r.database}: ` + Object.entries(r.copied).map(([k, n]) => `${k} ${nf.format(n)}`).join(', ') + '. Restarting…'); setTimeout(() => location.reload(), 6000); } catch (e) { fill(result, e.message); }
            }, true)), result);
      })();
      return wrap;
    })() : null,
    S.IS_ADMIN ? row('Check for updates daily', m.updateCheckLocked ? 'Switched off by UPDATE_CHECK=0 in the environment: the viewer never contacts GitHub on its own.'
      : 'Once a day the viewer asks GitHub whether a newer version of the viewer exists and, if so, shows admins a banner. Only the public list of changes is fetched; nothing about this install or its photos is sent. Off: it never contacts GitHub on its own (Maintenance › Viewer can still check on request).',
      h('label', { style: 'display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap' }, h('input', { type: 'checkbox', checked: !!m.updateCheck, disabled: !!m.updateCheckLocked, onchange: async (e) => {
        try { await post('/api/settings', { update_check: e.target.checked }); toast(e.target.checked ? 'The viewer will check for updates once a day.' : 'The viewer will not contact GitHub on its own.'); pollSoon(2000); } catch (err) { e.target.checked = !e.target.checked; toast(err.message); } } }), 'Check daily')) : null,
    S.IS_ADMIN ? row('Read-only mode', m.readOnlyLocked ? 'Locked on by READ_ONLY=1 in the environment: nothing in the library can be changed from the viewer.'
      : `${m.readOnly ? 'On' : 'Off'}. When on, the viewer only browses: editing, tagging, face naming, place tags, GPS tracks, metadata sync and database updates are switched off and refused by the server. Favorites and viewer albums keep working; they never touch the library.`,
      m.readOnlyLocked ? null : btn(m.readOnly ? 'Turn off' : 'Turn on', async () => { try { await post('/api/settings', { read_only: !m.readOnly }); const s = await api('/api/status'); S.EDITING = s.editing; S.CAN_MANAGE = s.editing && s.dbWrite; } catch (e) { toast(e.message); } settingsPage(view); })) : null,
    S.IS_ADMIN ? (() => {
      const d = m.digikam || {}, copy = d.mirror ? `${nf.format(d.mirror.photos)} photos as of ${fmtWhen(d.mirror.taken)}` : 'not taken yet';
      if (d.conflict) {
        const planEl = h('div', { class: 'muted', style: 'font-size:13px;margin-top:6px' }, 'Working out what changed…');
        api('/api/library/merge').then((p) => fill(planEl, p.total ? 'To merge: ' + p.summary.join(', ') + '.' : 'Nothing to merge.')).catch((e) => fill(planEl, e.message));
        const wrap = row('Sync with digiKam', `On, but ${plural(d.localChanges, 'change')} made while it was off ${d.localChanges === 1 ? 'is' : 'are'} not in digiKam’s database yet, so the viewer keeps using its own library. Merge writes those changes into digiKam (new tags, folders and uploads get digiKam ids; renames, assignments, faces, ratings, captions and locations are applied; deleted photos are removed), after a backup of digiKam’s database. Replace drops them and copies the library from digiKam again. Sidecar edits are kept either way.`,
          h('span', { style: 'display:flex;gap:6px;flex-wrap:wrap' },
            btn('Merge into digiKam', async () => { if (!confirm('Write the changes made while sync was off into digiKam’s database? digiKam must be closed; its database is backed up first.')) return; try { const r = await post('/api/library/merge', {}); toast(`Merged: ${Object.entries(r.applied).map(([k, v]) => `${v} ${k.replace('_', ' ')}`).join(', ') || 'nothing to do'}${r.conflicts ? `; ${r.conflicts} rows digiKam had also changed (the viewer’s values won)` : ''}.`, null, null, 12000); } catch (e) { toast(e.message, null, null, 10000); } setTimeout(() => settingsPage(view), 2500); }, true),
            btn('Replace with digiKam’s', async () => { if (!confirm(`Drop the ${plural(d.localChanges, 'change')} made while sync was off and copy the library from digiKam again? Edits in sidecars are kept.`)) return; try { await post('/api/settings', { library_replace: true }); toast('Copying the library from digiKam…'); } catch (e) { toast(e.message); } setTimeout(() => settingsPage(view), 2500); }),
            btn('Turn sync off', async () => { try { await post('/api/settings', { digikam_sync: false }); } catch (e) { toast(e.message); } settingsPage(view); })));
        wrap.firstChild.append(planEl);
        return wrap;
      }
      return row('Sync with digiKam', `Currently ${d.sync ? 'on' : 'off'}. On: the viewer re-reads digiKam’s database whenever digiKam saves, and writes the edits made here into it whenever digiKam is closed (edits always go to the sidecars first). Off: the viewer never opens digiKam’s database; the library lives in the viewer’s own tables (${copy}) and everything works there: edits, tags, folders, uploads, faces and place tags. Turning sync on again does not overwrite what was changed meanwhile; you choose what happens.${d.sync && !d.reachable ? ' digiKam’s database is unreachable right now; the copy is in use.' : ''}`,
        btn(d.sync ? 'Turn off' : 'Turn on', async () => { try { await post('/api/settings', { digikam_sync: !d.sync }); } catch (e) { toast(e.message); } settingsPage(view); }));
    })() : null,
    S.IS_ADMIN ? analysisSection(view, 'prefs') : null,
    S.IS_ADMIN && S.USERS_EXIST ? usersSection() : null,
    S.IS_ADMIN && S.EDITING ? [
    h('h2', null, 'Editing'),
    row('Write metadata into image files', `Off: edits go to XMP sidecars only. On: also into JPEG, PNG, TIFF and WebP files, like digiKam’s “sidecar and file” mode. Currently ${m.writeFiles ? 'on' : 'off'}.`,
      btn(m.writeFiles ? 'Turn off' : 'Turn on', async () => { try { await post('/api/settings', { write_files: !m.writeFiles }); } catch (e) { toast(e.message); } settingsPage(view); })),
    h('h2', null, 'Place tags'),
    row('Tags from GPS positions', 'Turns each geotagged photo’s town into real digiKam tags under “Places/Country/Region/Town”, so places are searchable in digiKam as well. Missing tags are created in digiKam’s database (digiKam closed); assignments go to sidecars like any edit.',
      h('button', { class: 'pillbtn', onclick: placeTagsDialog }, icon('place'), 'Review and run')),
    templatesSection(m),
    ] : null,
    ] : [
    // 1. Library: everything about digiKam's data as the viewer sees it
    h('h2', null, 'Library'),
    row('Import', 'Photos and videos from a phone, a card or a computer, into folders by date, duplicates left out, a sidecar for each. The installed app is a share target on Android.', h('a', { class: 'pillbtn', href: '#/import' }, 'Open Import')),
    scannerRow(m, row, btn, () => { if (S.route.view === view) settingsPage(view); }),
    row('Lossless JPEG edits', S.LOSSLESS?.jpegtran ? 'jpegtran (libjpeg-turbo) is installed: JPEGs are rotated, flipped and cropped without re-encoding; other files by their orientation flag.'
      : 'jpegtran (libjpeg-turbo) is not installed on the server, so JPEGs are turned by their orientation flag only and cannot be cropped. Install it there, as root: apt-get install -y libjpeg-turbo-progs (Debian, Ubuntu, Proxmox LXC); dnf install libjpeg-turbo-utils; brew install jpeg-turbo. Nothing needs restarting.',
      h('span', { class: 'mst ' + (S.LOSSLESS?.jpegtran ? 'ok' : 'warn') }, S.LOSSLESS?.jpegtran ? 'installed' : S.LOSSLESS ? 'missing' : 'editing off')),
    row('Trash', 'Photos moved to the trash here or in digiKam (each album root’s .dtrash folder), with their sidecars, until restored or deleted for good.', h('a', { class: 'pillbtn', href: '#/trash' }, 'Open the trash')),
    row('Library copy', (() => { const d = m.digikam || {}; return d.mirror ? `The viewer’s own copy of the library’s metadata: ${nf.format(d.mirror.photos)} photos as of ${fmtWhen(d.mirror.taken)}, refreshed at every read of digiKam and included in the app backup. The index is built from it whenever digiKam’s database is unreachable or Sync with digiKam is off${d.source === 'mirror' ? ' (as it is now)' : ''}.` : 'Not taken yet; it is taken at the next read of digiKam’s database.'; })(), null),
    row('Rescan the library now', `The index is rebuilt from digiKam’s database whenever digiKam saves (last saved ${when(m.digikamSaved)}; index built ${when(m.indexBuilt)}). Force it here if something looks stale.`,
      btn('Rescan now', () => act('reindex', 'Rescanning the library…'), true)),
    (() => {  // metadata consistency, summarised in one line
      const line = h('span', null, 'Loading…');
      api('/api/consistency?limit=1').then((c) => {
        const s = c.summary, p = c.progress;
        fill(line, p.running ? `Checking… ${nf.format(p.done)} / ${nf.format(p.total)} photos.`
          : !s.scanned ? 'Not checked yet.'
          : `${s.mismatched ? plural(s.mismatched, 'photo disagrees', 'photos disagree') : 'Everything agrees'} across digiKam’s database, the sidecars and the files${s.scanned_at ? ` (last checked ${fmtWhen(s.scanned_at)})` : ''}.`);
      }).catch(() => fill(line, ''));
      return row('Metadata consistency', h('span', null, line, ' Rating, title, caption and tags are compared in the three places they live; disagreements can be synced in either direction.'),
        h('a', { class: 'pillbtn on', href: '#/consistency' }, 'Open'));
    })(),
    (() => {  // face quality, summarised in one line; the page has the details and the fixes
      const line = h('span', null, 'Checking…');
      api('/api/quality').then((q) => fill(line, (q.near.extra ? `${plural(q.near.extra, 'face is', 'faces are')} stored twice. ` : 'No face is stored twice. ')
        + (q.separate.count ? `${plural(q.separate.count, 'photo has', 'photos have')} the same person in two places. ` : '')
        + (q.unnamed.count ? `${plural(q.unnamed.count, 'photo has', 'photos have')} faces but no names. ` : '')
        + (q.untagged?.count ? `${plural(q.untagged.count, 'photo shows', 'photos show')} people but nobody is tagged. ` : '')
        + (q.invalid.count ? `${plural(q.invalid.count, 'box is', 'boxes are')} outside the photo or empty.` : ''))).catch(() => fill(line, ''));
      return row('Metadata quality', h('span', null, line, ' Photos without a location, with an implausible date or in a folder for another date; duplicate faces, the same person twice, and boxes outside the photo.'), h('a', { class: 'pillbtn on', href: '#/quality' }, 'Open'));
    })(),
    S.EDITING ? row('Edits waiting for digiKam', m.pendingEdits ? `${plural(m.pendingEdits, 'photo has', 'photos have')} edits saved in sidecars that digiKam’s database does not have yet. digiKam is ${m.digikamRunning ? 'running, so they cannot be applied right now' : 'closed, so they can be applied now'}.` : 'None. digiKam’s database and the sidecars agree.',
      m.pendingEdits ? btn('Apply to digiKam database…', dbDialog, !m.digikamRunning) : null) : null,
    row('Tag cover photos', 'The photo shown for each tag is chosen by the content model. Pick them again after big tagging changes.', btn('Pick again', () => act('covers', 'Choosing tag covers in the background.'))),
    // 2. AI analysis and models
    aiSection(view),
    // 3. Storage and backups: each item's size with what can be done about it on the same row
    h('h2', null, 'Storage and backups', h('span', { class: 'muted' }, `${fmtBytes(st.free)} free on the disk`)),
    (() => {
      const item = (label, size, detail, action) => h('tr', null, h('td', null, label, detail ? h('div', { class: 'muted', style: 'font-size:11.5px;white-space:normal' }, detail) : null),
        h('td', { class: 'num' }, fmtBytes(size) || '0 B'), h('td', { class: 'act' }, action || null));
      const small = (label, fn) => h('button', { class: 'pillbtn small', onclick: fn }, label);
      return h('table', { class: 'stable wide' }, h('tbody', null,
        item('Thumbnails', st.thumbnails, 'grid previews, rebuilt in the background (several minutes)', small('Rebuild', () => act('thumbs', 'Rebuilding thumbnails in the background.', `Delete all grid thumbnails (${fmtBytes(st.thumbnails)}) and rebuild them?`))),
        item('Previews', st.previews, 'large images for the viewer, remade on demand', small('Clear', () => act('previews', 'Previews cleared; they are remade as you open photos.'))),
        item('Face crops', st.faces, 'remade on demand', small('Clear', () => act('faces', 'Face crops cleared; they are remade as needed.'))),
        item('Search index', st.index, 'rebuilt from digiKam’s database in seconds'),
        item('Viewer database', st.viewerDb, st.viewerDbError ? h('span', { class: 'warn' }, `MySQL/MariaDB is configured but could not be used when the viewer started (${st.viewerDbError}), so the SQLite file is in use since then: anything saved now is not in the MySQL database. Fix the cause (Settings › Viewer database) and restart the viewer.`) : 'users, albums, favorites, settings, AI results'),
        item('Content embeddings', st.embeddings, 'the content-search index'),
        item('AI models', st.models),
        item('Sidecar backups', st.backups, 'each sidecar as it was before the viewer first changed it'),
        item('Image file backups', st.fileBackups, 'the original of each file whose metadata was rewritten'),
        item('Database backups', st.dbBackups, 'digiKam’s database before each write (the last 10)'),
        item('Deleted photos', st.deletedPhotos, 'copies of deleted photos and their sidecars, kept until you remove them (data/deleted-photos)')));
    })(),
    h('div', { class: 'muted', style: 'font-size:12.5px;margin-top:8px;max-width:820px' }, 'Backups are never deleted automatically (apart from keeping the last ten database copies); remove them by hand once you are happy with the corresponding changes.'),
    row('Download a backup', 'digiKam’s database as it is right now (a consistent copy of digikam4.db, or a mysqldump for MariaDB), and the viewer’s data as a zip (its database plus the content-search index). Keep them somewhere else.',
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, h('a', { class: 'pillbtn', href: '/api/backup/digikam', download: '' }, icon('download'), h('span', null, 'digiKam database')),
        h('a', { class: 'pillbtn', href: '/api/backup/app', download: '' }, icon('download'), h('span', null, 'Viewer data (zip)')))),
    // 4. The viewer itself
    h('h2', null, 'Viewer'),
    row('Edit log', 'Every change made in the viewer, for every photo, kept for good: who, when, what it was before and after, with Undo and restore of the sidecar or the original file from the backups.', h('a', { class: 'pillbtn', href: '#/history' }, 'Open the edit log')),
    row('Server log', 'What the server has been doing: edits, database writes, updates, background jobs and any errors, from the viewer’s own log file in the data folder.', btn('Show the log', logDialog)),
    (() => {
      const body = h('div', { class: 'muted', style: 'font-size:13px' }, 'Loading…');
      const draw = async () => {
        let u; try { u = await api('/api/update'); } catch (e) { fill(body, e.message); return; }
        const c = u.check, p = u.progress, last = u.lastUpdate;
        const outcome = !p.running && !p.error && last ? h('div', { class: last.ok ? 'muted' : 'warn', style: 'font-size:12px;margin-top:2px' }, `last update: ${last.text}`) : null;
        const state = p.running ? 'updating… the viewer restarts when it is done' : p.error ? `last update failed: ${p.error}` : !c ? `v${u.version} · commit ${u.installed}` : c.status === 'current' ? `v${u.version} (${u.installed}) is the latest` : c.status === 'update' ? `newer version on GitHub: ${c.remote} from ${c.remoteDate} (${c.remoteMessage}); installed ${u.installed}` : `could not check: ${c.error}`;
        fill(body, state, outcome, h('div', { style: 'display:flex;gap:6px;margin-top:6px' },
          btn('Check for updates', async () => { fill(body, 'Checking…'); try { await post('/api/update', { action: 'check' }); } catch (e) { toast(e.message); } draw(); }),
          btn('MCP update token…', async () => { try { const t = await post('/api/update', { action: 'token' }); const back = modal('MCP update token', h('p', { class: 'muted' }, 'An MCP client (e.g. Claude) can run the update with the update_viewer tool and this token; the endpoint itself has no sign-in, so treat it like a password.'), h('div', { class: 'einput', style: 'font-family:monospace;user-select:all;word-break:break-all' }, t.token), t.fromEnv ? h('p', { class: 'muted' }, 'Set by MCP_ADMIN_TOKEN in the environment.') : null,
            h('div', { class: 'modal-btns' }, t.fromEnv ? null : h('button', { class: 'pillbtn', onclick: async () => { if (!confirm('Generate a new token? The old one stops working.')) return; try { await post('/api/update', { action: 'regenerate_token' }); } catch (e) { toast(e.message); } back.remove(); } }, 'Regenerate'), h('button', { class: 'pillbtn on', onclick: () => back.remove() }, 'Close'))); } catch (e) { toast(e.message); } }),
          u.docker ? h('span', { class: 'muted' }, 'Docker: update by pulling the image (docker compose pull && docker compose up -d).')
            : u.canRun ? btn('Update now', async () => { if (!confirm('Fetch the latest version, refresh the packages and restart the viewer?')) return; try { await post('/api/update', { action: 'run' }); toast('Updating; the viewer restarts when it is done.'); } catch (e) { toast(e.message); } setTimeout(draw, 3000); }, c && c.status === 'update') : null));
        if (p.running) setTimeout(() => { if (S.route.view === view) draw(); }, 5000);
      };
      draw();
      return row('Viewer version', 'The code running here, and whether the GitHub main branch has something newer. "Update now" runs update.sh (git pull or a fresh tarball), keeps your settings and data, and restarts the service.', body);
    })(),
    row('Restart the viewer', 'Stops the server; launchd, Docker or systemd starts it again within seconds. Needed after an update of the RapidOCR package (offered in the models table above when “Check models for updates” finds a newer one), and handy when a background job looks stuck.',
      btn('Restart', async () => { if (!confirm('Restart the viewer now?')) return; try { await post('/api/restart', {}); toast('Restarting…'); } catch (e) { toast(e.message); } })),
    ])));
}

export async function showDuplicates() {
  main.replaceChildren(h('div', { class: 'empty' }, 'Looking for duplicates…'));
  let d;
  try { d = await api('/api/duplicates'); } catch { if (S.route.view === 'duplicates') main.replaceChildren(h('div', { class: 'empty' }, 'Could not load duplicates.')); return; }
  if (S.route.view !== 'duplicates') return;
  const src = { id: d.groups.flatMap((g) => g.items.map((x) => x.id)) };
  let k = 0;
  main.replaceChildren(h('div', { class: 'page' }, crumbs([['Explore', '#/explore'], ['Duplicate files', null]]), h('h1', null, 'Duplicate files'),
    h('div', { class: 'muted' }, d.groups.length ? `${nf.format(d.groups.length)} sets of byte-identical files, ${fmtBytes(d.wasted)} that could be freed. The viewer never deletes anything; remove the extras in digiKam.`
      : 'No byte-identical files found. digiKam’s file hashes say every item is unique.'),
    d.groups.map((g) => h('div', { class: 'dupgroup' }, h('div', { class: 'muted' }, `${g.items.length} copies · ${fmtBytes(g.size)} each`),
      h('div', { class: 'duprow' }, g.items.map((x) => { const i = k++; return h('div', { class: 'dupitem' },
        h('div', { class: 'tile', style: `width:${Math.round(150 * Math.min(2, x.w / (x.h || 1)))}px;height:150px`, onclick: () => openLightbox(src, i, true) }, h('img', { src: `/thumb/${x.id}`, loading: 'lazy', alt: '' })),
        h('div', { class: 'nm', title: x.name }, x.name), h('a', { class: 'sb lnk', href: hashFor('photos', photosWith({ album: albumIdByPath(x.album), sort: 'old' })) }, x.album.replace(/^\//, ''))); })))),
    nearDupsSection(src)));
  syncLightbox();
}
/** Near-duplicates: look-alike photos found with the content-search embeddings (resized, re-saved, burst neighbours). */
export function nearDupsSection(src) {
  const sec = h('div', { style: 'margin-top:36px' });
  const draw = async () => {
    let d; try { d = await api('/api/neardups'); } catch (e) { fill(sec, h('div', { class: 'muted' }, e.message)); return; }
    if (S.route.view !== 'duplicates') return;
    const p = d.progress;
    const level = h('select', null, Object.keys(d.levels).map((k) => h('option', { value: k, selected: k === d.level }, `${k} (${d.levels[k]})`)));
    const run = S.IS_ADMIN ? h('button', { class: 'pillbtn on', disabled: p.running || !d.enabled, onclick: async () => { try { await post('/api/neardups', { level: level.value }); toast('Looking for similar photos in the background.'); } catch (e) { toast(e.message, null, null, 8000); } setTimeout(draw, 1500); } }, icon('spark'), d.gen ? 'Find again' : 'Find similar photos') : null;
    const note = !d.enabled ? 'Needs content search (SigLIP 2 or CLIP) installed: the similarity comes from its embeddings.'
      : p.running ? `Comparing… ${nf.format(p.done)} / ${nf.format(p.total)} photos.`
      : p.error ? `Last run failed: ${p.error}`
      : !d.gen ? 'Not computed yet. Every photo is compared with every other using the content-search embeddings; a minute or two for a large library.'
      : d.current ? `${nf.format(d.groups.length)} sets of similar photos at the “${d.level}” level. Sharpest copy first in each set.` : `Computed for an older search index (${d.level}); run it again for current results.`;
    fill(sec, h('h2', { style: '--hue:var(--c-purple)' }, h('span', { class: 'hic' }, icon('spark')), 'Similar photos'),
      h('div', { class: 'muted', style: 'max-width:820px' }, 'Resized, re-saved or re-edited copies and burst neighbours, which the byte-identical check above cannot see. The viewer never deletes anything; it only shows the sets.'),
      h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0' }, S.IS_ADMIN ? h('span', null, 'Strictness: ', level) : null, run, h('span', { class: 'muted' }, note)),
      d.groups.map((g) => h('div', { class: 'dupgroup' }, h('div', { class: 'muted' }, `${g.n} similar · ${Math.round(g.score * 100)}% alike${g.identical ? ' · also byte-identical' : ''}`),
        h('div', { class: 'duprow' }, g.items.map((x) => { const i = src.id.push(x.id) - 1; return h('div', { class: 'dupitem' },
          h('div', { class: 'tile', style: `width:${Math.round(150 * Math.min(2, x.w / (x.h || 1)))}px;height:150px`, onclick: () => openLightbox(src, i, true) }, h('img', { src: `/thumb/${x.id}`, loading: 'lazy', alt: '' })),
          h('div', { class: 'nm', title: x.name }, x.name), h('div', { class: 'sb' }, `${fmtBytes(x.size || 0)}${x.sharpn != null ? ' · sharpness ' + Math.round(x.sharpn * 100) + '%' : ''}`),
          h('a', { class: 'sb lnk', href: hashFor('photos', photosWith({ album: albumIdByPath(x.album), sort: 'old' })) }, x.album.replace(/^\//, ''))); })))));
    if (p.running) setTimeout(() => { if (S.route.view === 'duplicates') draw(); }, 4000);
  };
  draw();
  return sec;
}
export const albumIdByPath = (path) => S.META.albums.find((a) => a.path === path)?.id ?? '';

export async function tagOp(op, t) {
  let r;
  try { r = await post('/api/tags/op', op); } catch (e) { toast(e.message, null, null, 9000); return; }
  toast(op.op === 'icon' ? `“${r.path}” now uses this photo.` : op.op === 'notperson' ? `“${r.path}” is a keyword tag again, no longer listed under People.` : op.op === 'merge' ? `Merged “${r.path}” into “${r.new_path}” (${nf.format(r.photos)} photos).` : op.op === 'add' ? `Created “${r.path}”.` : `${{ rename: 'Renamed', move: 'Moved', delete: 'Deleted' }[op.op]} “${r.path}”${r.new_path ? ` → “${r.new_path}”` : ''}.` +
    (r.sidecars ? ` Updating ${nf.format(r.photos)} photos’ sidecars in the background.` : ''), null, null, 8000);
  await awaitReindex();
  $('#taglist')?.remove();  // the tag datalist is rebuilt from the new tag list on next use
  details.clear();
  if (S.route.view === 'tags') showTags();
  pollSoon(500);
}
export const openTags = new Set();  // which tag nodes are expanded, kept across redraws
/** digiKam's own face buckets under People; not real people and not to be renamed, moved or deleted. */
export const isFaceBucket = (t) => ['Unknown', 'Ignored', 'Unconfirmed'].includes(t.name) && t.full.startsWith('People/');
/** parent id (0 = top level) -> child tags, optionally leaving some tags out. */
export function tagKids(skip = () => false) {
  const kidsOf = new Map();
  for (const t of S.META.tags) { if (skip(t)) continue; const k = t.pid ?? 0; if (!kidsOf.has(k)) kidsOf.set(k, []); kidsOf.get(k).push(t); }
  return kidsOf;
}
export function tagTreePicker(onPick, { exclude = () => false, allowTop = true } = {}) {
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
export function moveTagDialog(t) {
  const sub = (x) => x.id === t.id || x.full.startsWith(t.full + '/') || x.id === t.pid;
  const move = (parent) => { back.remove(); tagOp({ op: 'move', id: t.id, parent }, t); };
  const back = modal(`Move “${t.name}” into…`,
    h('p', null, `Now at “${t.full.replaceAll('/', ' › ')}”. Its sub-tags and photos move with it. Pick the new parent:`),
    tagTreePicker((dest) => { if (dest && sub(dest)) { toast('A tag cannot be moved into itself.'); return; } move(dest ? dest.id : 0); }, { exclude: sub, allowTop: t.pid != null }),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Cancel')));
}

export function showTags() {
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
    const act = (ic, title, fn) => h('button', { class: 'iconbtn tact', title, onclick: (e) => { e.stopPropagation(); fn(); } }, icon(ic));
    const trow = h('div', { class: 'trow', title: t.full, onclick: () => go('photos', photosWith({ tag: t.id })) }, tw, cover,
      h('span', { class: 'nm' }, flat ? t.full.replaceAll('/', ' › ') : t.name),
      can ? h('span', { class: 'tacts' },
        act('add', 'Add a tag inside this one', () => { const n = prompt(`New tag inside “${t.name}”`); if (n) tagOp({ op: 'add', parent: t.id, name: n }); }),
        act('edit', 'Rename', () => { const n = prompt('Rename tag', t.name); if (n && n !== t.name) tagOp({ op: 'rename', id: t.id, name: n }, t); }),
        h('button', { class: 'pillbtn tmove', title: 'Move this tag (with its sub-tags) under another tag, or to the top level', onclick: (e) => { e.stopPropagation(); moveTagDialog(t); } }, icon('folder'), 'Move'),
        t.person && !kids.length ? h('button', { class: 'pillbtn tmove', title: 'The same person entered twice: move everything on this one to another person, then remove this one', onclick: (e) => { e.stopPropagation();
          personPicker((into) => { if (into.id !== t.id && confirm(`Merge “${t.full.replaceAll('/', ' › ')}” into “${into.full.replaceAll('/', ' › ')}”?\n\nIts ${nf.format(t.nAll)} photos and face boxes move to ${into.name}, then “${t.name}” is removed. digiKam's database is backed up first.`)) tagOp({ op: 'merge', id: t.id, into: into.id }, t); });
        } }, 'Merge into…') : null,
        t.person && !t.full.startsWith('People/') ? h('button', { class: 'pillbtn tmove', title: 'digiKam marked this tag as a person (a face was once named with it): make it a keyword tag again, off the People page', onclick: (e) => { e.stopPropagation(); tagOp({ op: 'notperson', id: t.id }, t); } }, 'Not a person') : null,
        isPerson ? null : act('trash', 'Delete', () => { const kidsN = S.META.tags.filter((x) => x.full.startsWith(t.full + '/')).length;
          if (confirm(`Delete the tag “${t.full}”${kidsN ? ` and its ${kidsN} sub-tags` : ''}?\n\nIt will be removed from ${nf.format(t.nAll)} photos (the photos themselves are not touched).`)) tagOp({ op: 'delete', id: t.id }, t); })) : null,
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
    h('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap' },
      h('input', { class: 'pagefilter', placeholder: 'Find a tag…', oninput: (e) => draw(norm(e.target.value)) }),
      S.CAN_MANAGE ? h('button', { class: 'pillbtn' + (S.manageTags ? ' on' : ''), style: 'margin-top:6px', onclick: () => { S.manageTags = !S.manageTags; document.body.classList.toggle('managing', S.manageTags); showTags(); } }, icon('edit'), S.manageTags ? 'Done managing' : 'Manage tags') : null,
      S.manageTags ? h('button', { class: 'pillbtn', style: 'margin-top:6px', onclick: () => { const n = prompt('New top-level tag'); if (n) tagOp({ op: 'add', parent: 0, name: n }); } }, icon('add'), 'New top-level tag') : null),
    S.manageTags ? h('div', { class: 'enote', style: 'max-width:760px' }, 'To change the hierarchy, drag a tag onto the tag that should contain it, or use its Move button to pick a parent from the tree. Sub-tags and photos move with it. Changes go to digiKam’s database, so digiKam must be closed; a backup is saved first, and the sidecars of affected photos are updated in the background. People tags are managed in digiKam.') : null,
    h('div', { style: 'height:8px' }), topZone, tree));
  draw('');
}
