/** Settings and Maintenance: one loader (settingsPage) drawing the tabs of either page; the Users tab is users.js. */
import { $, PREF, UNITS, act, api, dfDayLocal, errorBox, fill, fmtBytes, fmtWhen, h, icon, loadingBox, main, nf, plural, post, regionUsesMiles, store } from './core.js';
import { S, THEMES, applyBadgePref, applyTheme, hashFor, infoPrefsEditor, pollSoon, toast, toastError } from './state.js';
import { confirmDialog, logDialog, modal } from './actions.js';
import { dbDialog, templatesSection } from './editing.js';
import { pageTitle } from './pages.js';
import { setupDialog, usersSection } from './users.js';
import { placeTagsDialog } from './admin.js';
import { runUpdate } from './main.js';


/** Maintenance › AI analysis and models: one table, a row per feature, with the model it runs on (download,
 *  update check) and the background job that fills it (progress, run again). Models without a job (places)
 *  and an installed search model that is not in use get rows of their own. */
function aiSection(view) {
  const body = h('div');
  let timer = 0;
  const checkBtn = h('button', { class: 'pillbtn', onclick: async () => { checkBtn.disabled = true; checkBtn.textContent = 'Checking…'; await act(() => post('/api/models/check', {})); draw(); } }, icon('spark'), 'Check models for updates');
  const wrap = h('div', null, h('h2', null, 'AI analysis and models'),
    h('div', { class: 'muted', style: 'max-width:860px' }, `Everything runs locally and is stored in ${S.APP_NAME}’s data folder; nothing is written to digiKam. New and changed photos are picked up automatically, about 20 seconds after digiKam saves them. Where a feature can run on more than one model, the menu in the Model column picks it: the default is the small, fast one, the others are more accurate and slower, each downloaded once. Models never update on their own; “Check models for updates” asks each source whether the exact files used have changed.`),
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin:14px 0' },
      h('button', { class: 'pillbtn on', onclick: async () => { await act(() => post('/api/analysis/run', { job: 'all' }), { toast: 'Checking for new and changed photos…' }); draw(); } }, icon('spark'), 'Check new and changed photos'), checkBtn),
    body);
  const fmtDate = (t) => (t ? dfDayLocal.format(new Date(t * 1000)) : '');  // when the model was installed: a real moment, in this browser's zone
  // a job's models: fixed ones (vision: RapidOCR; photo quality: the aesthetic and eye models; ages: MiVOLO) or the one
  // chosen for the feature (Maintenance offers the alternatives in a menu)
  const JOB_MODEL = { vision: ['ocr'], looks: ['aesthetic', 'eyes'], ages: ['mivolo'], jerseys: ['ocr'] };
  const JOB_CHOICE = { embeddings: 'search_model', objects: 'object_model', faces: 'face_vectors', facedetect: 'face_detector', captions: 'caption_model', clothing: 'search_model', rotation: 'search_model', videos: 'speech_model' };
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
    if (S.route.view !== view || !body.isConnected) return;  // the page was drawn again meanwhile: this copy of the section is gone
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
          if (!await confirmDialog(`${m.label} is not installed. Download it (${m.size})? ${S.APP_NAME} keeps working meanwhile; choose it here again once it has arrived.`)) return;
          await act(() => post('/api/models/download', { key }), { toast: `Downloading ${m.label}…` });
          draw(); return;
        }
        const note = group === 'search_model' ? ' The library is indexed again with it in the background; search stays on the current model until that finishes.'
          : group === 'face_vectors' ? ' Every face is embedded again with it (the groups and suggestions rebuild in the background).'
          : group === 'face_detector' ? ' Used by the next face detection run.'
          : ' New photos use it from now on; “Run again” redoes the whole library with it.';
        
        try { await post('/api/settings', { [group]: key }); toast(`${c.label}: now ${m.label}.${note}`, null, null, 9000); } catch (err) { e.target.value = c.chosen; toastError(err); }
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
              ' ', h('a', { class: 'lnk', href: o.page, target: '_blank', rel: 'noopener' }, 'About')),
            h('div', { class: 'muted' }, o.what)); })));
    };
    const modelCell = (ms, group) => {
      ms = ms.filter(Boolean);
      if (!ms.length) return h('td', { class: 'muted' }, 'built in');
      if (ms.length > 1) return h('td', null, ms.map((m) => { const td = modelCell([m]); return h('div', { class: 'mmulti' }, ...td.childNodes); }));
      const m = ms[0];
      const [st, cls] = modelState(m, p);
      return h('td', null, h('div', null, group ? chooser(group) : h('a', { class: 'lnk', href: m.page, target: '_blank', rel: 'noopener', title: m.what }, m.label), h('span', { class: 'muted' }, ` · ${m.size}`)),
        h('div', { class: 'mst ' + cls, title: m.revision ? `revision ${m.revision}` : '' }, st, group ? [' · ', h('a', { class: 'lnk', href: m.page, target: '_blank', rel: 'noopener', title: m.what }, 'About')] : null),
        group ? h('div', { class: 'muted mwhat' }, m.what) : null,
        group ? compare(group) : null);
    };
    const modelBtn = (m) => {
      if (Array.isArray(m)) return m.map(modelBtn);
      if (!m) return null;
      const c = m.check, upd = c && (c.status === 'update' || c.status === 'newer' || c.status === 'missing');
      if (m.installed && !upd) return null;  // installed and current: nothing to offer
      return h('button', { class: 'pillbtn small' + (!m.installed || upd ? ' on' : ''), disabled: !!p.running, title: m.installed ? `Download ${m.label} again (${m.size})` : `Download ${m.label} (${m.size})`,
        onclick: async () => {
          if (!await confirmDialog(`Download ${m.label} (${m.size})${m.installed ? ' again, replacing the installed files' : ''}? ${S.APP_NAME} keeps working meanwhile.` + (m.kind === 'pip' ? `\n\nA package update takes effect after a restart of ${S.APP_NAME}.` : ''))) return;
          await act(() => post('/api/models/download', { key: m.key }), { toast: `Downloading ${m.label}…` });
          draw();
        } }, m.installed ? 'Update' : 'Download');
    };
    const progressCell = (j, m, extra) => {
      if (!j) return h('td', { class: 'muted' }, '—');
      const state = !j.enabled ? (m.some((x) => x && !x.installed) ? 'needs its model' : 'off') : j.running ? 'running…' : j.queued ? 'queued' : j.done >= j.total ? 'up to date' : j.key === 'facedetect' ? 'on request' : 'waiting';
      const now = Date.now() / 1000, idle = j.running && j.since ? (j.last ? now - j.last : now - j.since) : 0, stuck = idle > 300;
      return h('td', { class: 'mprog' },
        h('div', { class: 'ahead' }, h('span', { class: j.running ? 'mst on' : j.done >= j.total && j.enabled ? 'mst ok' : 'mst' }, state), h('span', { class: 'sp' }), h('span', { class: 'muted' }, `${nf.format(Math.min(j.done, j.total))} / ${nf.format(j.total)}`)),
        h('div', { class: 'bar' }, h('i', { style: `width:${j.total ? Math.min(100, 100 * j.done / j.total).toFixed(1) : 0}%` })),
        j.running && j.since ? h('div', { class: stuck ? 'warn' : 'muted', style: 'font-size:11.5px;margin-top:3px' }, `${Math.round((now - j.since) / 60)} min · last progress ${idle < 90 ? Math.round(idle) + ' s' : Math.round(idle / 60) + ' min'} ago`,
          j.current && j.current.length ? ` · ${j.current.map((x) => x.split('/').pop()).join(', ')}` : '', stuck ? ` · probably stuck on that file: see the server log, or restart ${S.APP_NAME}` : '') : null,
        extra);
    };
    // face detection writes to digiKam's database, so it never starts by itself: a Run link for the photos not looked at yet
    const runNow = (j) => j && j.key === 'facedetect' && j.enabled && !j.running && j.done < j.total ? h('button', { type: 'button', class: 'lnk mrerun', style: 'margin-right:12px', title: 'Look for faces in every photo not looked at yet', onclick: async () => {
      if (!await confirmDialog(`Look for faces in the ${nf.format(j.total - j.done)} photos not checked yet? New faces are added to digiKam’s database as unnamed faces; faces already marked are kept as they are.`)) return;
      try { const r = await post('/api/analysis/run', { job: 'facedetect' }); toast(`Looking for faces in ${nf.format(r.queued)} photos.`); } catch (e) { toastError(e); }
      draw();
    } }, 'Run') : null;
    const rerun = (j) => j && j.rerun && j.enabled ? h('button', { type: 'button', class: 'lnk mrerun', title: `Discard the results of “${j.label}” and compute them again for every photo`, onclick: async () => {
      if (!await confirmDialog(`Run “${j.label}” again from scratch for all ${nf.format(j.total)} photos? The current results for it are discarded first. This can take from several minutes to a few hours.`)) return;
      await act(() => post('/api/analysis/run', { job: j.key, reset: true }), { toast: `Re-running “${j.label}”.` });
      draw();
    } }, 'Run again') : null;
    const row = (label, detail, j, m, group) => h('tr', { 'data-job': j?.key || '', 'data-model': m.filter(Boolean).map((x) => x.key).join(' ') },
      h('td', { class: 'mfeat' }, h('b', null, label), detail ? h('div', { class: 'muted mdet', title: detail }, detail) : null),
      modelCell(m, group), progressCell(j, m, [runNow(j), rerun(j)]), h('td', { class: 'macts' }, modelBtn(m)));
    const rows = a.jobs.map((j) => {
      const group = JOB_CHOICE[j.key];
      const m = group ? [models[choices[group]?.chosen]] : (JOB_MODEL[j.key] || []).map((k) => models[k]);
      return row(j.label, j.detail, j, m, group && choices[group] ? group : null);
    });
    // a model with a job of its own row is shown there; the rest (place names) get a row each
    const inRows = new Set([...Object.values(choices).flatMap((c) => c.options), ...Object.values(JOB_MODEL).flat()]);
    for (const m of md.models) {
      if (inRows.has(m.key)) continue;
      const label = m.key === 'geonames' ? 'Places from GPS positions' : m.label;
      const detail = m.key === 'geonames' ? 'Town, region and country for each GPS position, offline; looked up when the search index is built.' : m.what;
      rows.push(row(label, detail, null, [m]));
    }
    fill(body,
      h('div', { class: 'muted', style: 'font-size:13px;margin-bottom:8px' }, h('b', { style: 'color:var(--fg)' }, 'Background work: '), q.running ? `running ${(a.jobs.find((j) => (j.key === 'quality' && q.running === 'pixels') || j.key === q.running) || {}).label || q.running}` : 'idle',
        `. Jobs run one at a time, in the order below, each with up to ${q.threads} of ${q.cores || '?'} cores (change that in Settings).`, q.queued?.length ? ` Waiting: ${q.queued.length}.` : ''),
      h('div', { class: 'mwrap' }, h('table', { class: 'mtable' },
        h('thead', null, h('tr', null, ['Feature', 'Model', 'Progress', ''].map((c) => h('th', null, c)))), h('tbody', null, rows))));
    checkBtn.disabled = false; fill(checkBtn, icon('spark'), 'Check models for updates');
    clearTimeout(timer);
    if (a.jobs.some((j) => j.running) || p.running) timer = setTimeout(() => { if (S.route.view === view && body.isConnected) drawSafe(); }, 4000);
  };
  const drawRaw = draw;
  const drawSafe = async () => {  // a menu the user has open is not replaced under their pointer; try again shortly
    if (body.contains(document.activeElement) && document.activeElement.matches('select')) { clearTimeout(timer); timer = setTimeout(drawSafe, 1500); return; }
    return drawRaw();
  };
  drawSafe();
  return wrap;
}
function analysisSection(view, part = 'jobs') {
  // part 'jobs': the job list with progress (Maintenance); 'prefs': the search model and the core budget (Settings)
  const body = h('div');
  const wrap = h('div', null,
    ...(part === 'jobs' ? [h('h2', null, 'AI analysis'),
    h('div', { class: 'muted intro' }, `Everything here runs locally, wherever ${S.APP_NAME} is running, and is stored in its own data folder. New and changed photos are picked up automatically, about 20 seconds after digiKam saves them; “Check for new photos now” does the same on demand.`),
    h('div', { class: 'my14',}, h('button', { class: 'pillbtn on', onclick: async () => { await act(() => post('/api/analysis/run', { job: 'all' }), { toast: 'Checking for new and changed photos…' }); draw(); } }, icon('spark'), 'Check for new photos now')),] : [h('h2', null, 'Search and background work')]),
    body);
  let timer = 0;  // one poll per section while a job runs; a section drawn again stops polling (body.isConnected)
  const draw = async () => {
    let d;
    try { d = await api('/api/analysis'); } catch (e) { if (body.isConnected) fill(body, errorBox(e)); return; }  // say so, never an empty tab
    if (S.route.view !== view || !body.isConnected) return;
    const q = d.queue || {};
    const sel = h('select', { onchange: async (e) => { await act(() => post('/api/settings', { bg_threads: +e.target.value }), { toast: 'Applies when the next job starts.' }); } },
      Array.from({ length: Math.max(1, (q.cores || 4) - 1) }, (_, i) => h('option', { value: i + 1, selected: i + 1 === q.threads }, String(i + 1))));
    fill(body, h('div', { class: 'ajob', style: 'border-top:0;padding-top:0' },
      h('div', { class: 'ahead' }, h('b', null, 'Background work'), h('span', { class: 'muted' }, q.running ? `running: ${(d.jobs.find((j) => (j.key === 'quality' && q.running === 'pixels') || j.key === q.running) || {}).label || q.running}` : 'idle')),
      h('div', { class: 'muted note mt6' }, ...(part === 'prefs'
        ? [`Jobs run one at a time, in a fixed order, so ${S.APP_NAME} and other apps stay responsive. Each job may use up to `, sel, ` of ${q.cores || '?'} cores.`]
        : [`Jobs run one at a time, in the order below. Each may use up to ${q.threads} of ${q.cores || '?'} cores (change that in Settings).`, q.queued?.length ? ` Waiting: ${q.queued.length}.` : '']))),
      part === 'jobs' ? d.jobs.map((j) => h('div', { class: 'ajob' },
      h('div', { class: 'ahead' }, h('b', null, j.label), h('span', { class: 'muted' }, !j.enabled ? 'not installed' : j.running ? 'running…' : j.queued ? 'queued' : j.done >= j.total ? 'up to date' : 'waiting'),
        h('span', { class: 'sp' }), h('span', null, `${nf.format(Math.min(j.done, j.total))} / ${nf.format(j.total)}`)),
      h('div', { class: 'bar' }, h('i', { style: `width:${j.total ? Math.min(100, 100 * j.done / j.total).toFixed(1) : 0}%` })),
      j.running && j.since ? (() => { const now = Date.now() / 1000, idle = j.last ? now - j.last : now - j.since, stuck = idle > 300;
        return h('div', { class: stuck ? 'warn' : 'muted', style: 'font-size:12px;margin-top:4px' }, `running for ${Math.round((now - j.since) / 60)} min · last progress ${idle < 90 ? Math.round(idle) + ' s' : Math.round(idle / 60) + ' min'} ago`,
          j.current && j.current.length ? ` · working on ${j.current.map((p) => p.split('/').pop()).join(', ')}` : '',
          stuck ? ` · no progress for over 5 minutes: probably stuck on that file; see the server log (journalctl -u iopix, docker logs, or data/server.log) and consider restarting ${S.APP_NAME} from Maintenance` : ''); })() : null,
      h('div', { class: 'muted note mt6' }, j.detail, ' ',
        j.rerun && j.enabled ? h('button', { type: 'button', class: 'lnk', onclick: async () => {
          if (!await confirmDialog(`Run “${j.label}” again from scratch for all ${nf.format(j.total)} photos? The current results for it are discarded first. This can take from several minutes to a few hours.`)) return;
          await act(() => post('/api/analysis/run', { job: j.key, reset: true }), { toast: `Re-running “${j.label}”.` });
          draw();
        } }, 'Run again from scratch') : null))) : null);
    const others = (d.models || []).filter((m) => m.installed && !m.chosen);
    if (others.length && part === 'prefs') body.append(h('div', { class: 'ajob' },
      h('div', { class: 'ahead' }, h('b', null, 'Search model'), h('span', { class: 'muted' }, `using ${d.models.find((m) => m.active)?.label || 'none'}`)),
      h('div', { class: 'muted note mt6' },
        others.map((other) => h('div', { style: 'margin-bottom:4px' },
          other.key === 'siglip2'
            ? 'SigLIP 2 base is also installed: a newer model, generally more accurate than CLIP, though which one finds a given subject better varies; it indexes about 4 times slower. '
            : other.key === 'jinaclip' ? 'Jina CLIP v1 is also installed: a larger CLIP that understands longer, more natural queries; it indexes about 3 times slower than CLIP. '
            : `${other.label} is also installed. `,
          'Switching re-indexes the whole library in the background when that model has no index yet; search stays on the current model until it finishes, and you can switch back at any time. ',
          h('button', { type: 'button', class: 'lnk', onclick: async () => {
            if (!await confirmDialog(`Switch content search to ${other.label}?`)) return;
            await act(() => post('/api/settings', { search_model: other.key }), { toast: `Switching to ${other.label}.` });
            draw();
          } }, `Switch to ${other.label}`))))));
    clearTimeout(timer);
    if (part === 'jobs' && d.jobs.some((j) => j.running)) timer = setTimeout(() => { if (S.route.view === view && body.isConnected) draw(); }, 4000);
  };
  draw();
  return wrap;
}
export async function showSettings() { return settingsPage('settings'); }
export async function showMaintenance() { if (!S.IS_ADMIN) { main.replaceChildren(h('div', { class: 'empty' }, 'Maintenance is for admins. Sign in as an admin to see it.')); return; } return settingsPage('maintenance'); }
// Settings: preferences. Maintenance: library actions, job progress, models, storage. One loader, two layouts.
/** Maintenance › File scanner: on or off, how often, Scan now, and what the last pass found. */
function scannerRow(c) {
  const { m, row, btn, again, switchField } = c;
  const sc = m.scanner || {};
  const last = sc.last;
  const said = last ? `Last pass ${fmtWhen(last.finished)}: ${[last.added && plural(last.added, 'new file'), last.modified && plural(last.modified, 'changed file'), last.removed && plural(last.removed, 'file') + ' gone', last.folders && plural(last.folders, 'new folder')].filter(Boolean).join(', ') || 'nothing new'}.${last.skipped?.length ? ' ' + last.skipped.join(' ') : ''}${last.error ? ' Error: ' + last.error : ''}` : 'No pass yet.';
  const on = switchField('file_scanner', !!sc.enabled, { on: 'File scanner on: a first pass starts now.', off: 'File scanner off.', cls: 'chk' });
  const every = h('select', { class: 'sharepick', onchange: async (e) => { await act(() => post('/api/settings', { scan_interval: +e.target.value })); } },
    [[5, 'every 5 minutes'], [15, 'every 15 minutes'], [60, 'every hour'], [360, 'every 6 hours'], [1440, 'once a day']].map(([v, l]) => h('option', { value: v, selected: v === sc.interval }, l)));
  return row('File scanner',
    h('span', null, `Looks in the album folders for photos and videos added, changed or removed outside ${S.APP_NAME} (Finder, a sync tool, a phone backup), as digiKam’s scanner does, and records them the same way: camera, place, keywords, faces, digiKam’s file fingerprint. ${S.NO_DK ? 'With no digiKam it is how photos copied into the folder get into the library, so it is on unless you turn it off.' : 'Leave it off if everything arrives through Upload or Import; those are always read in full.'} A disk that looks unmounted is left alone. `, h('span', { class: 'muted' }, sc.running ? 'Scanning now…' : said)),
    h('span', { class: 'toolbar',}, on, every,
      btn('Scan now', async () => { try { await post('/api/scan', {}); toast('Scanning the album folders…'); setTimeout(again, 4000); } catch (e) { toastError(e); } })));
}
const OPEN_COMPARE = new Set();  // model groups whose "Compare the models" fold is open on the Maintenance page
// Settings and Maintenance are split into tabs: each stab() marks where one begins; what comes before the first is always shown
const STABS = {
  settings: [['display', 'Display'], ['server', 'digiKam and server'], ['editing', 'Editing'], ['ai', 'AI and search'], ['contacts', 'ioContacts'], ['users', 'Users and sign-in']],
  maintenance: [['library', 'Library'], ['models', 'AI analysis and models'], ['storage', 'Storage and backups'], ['updates', 'Updates and logs']],
};
const stab = (k) => h('span', { class: 'stab-mark', 'data-tab': k, hidden: true });
function settingsTabs(page, view) {
  if (!page) return;
  const kids = [...page.children], groups = new Map();
  let cur = null;
  for (const el of kids) {
    if (el.classList.contains('stab-mark')) { cur = el.dataset.tab; groups.set(cur, []); continue; }
    if (cur) groups.get(cur).push(el);
  }
  const tabs = STABS[view].filter(([k]) => groups.has(k));
  if (tabs.length < 2) return;
  let pick = S.route.params.get('tab');  // #/settings?tab=users links straight to a tab; else the last one opened
  if (!groups.has(pick)) pick = store.local.get(PREF[`${view}Tab`], null);
  if (!groups.has(pick)) pick = tabs[0][0];
  const bar = h('div', { class: 'stabs', role: 'tablist' });
  const show = (k) => {
    if (!groups.has(k)) return;
    for (const [g, els] of groups) els.forEach((el) => { el.style.display = g === k ? '' : 'none'; });
    [...bar.children].forEach((b) => { b.classList.toggle('on', b.dataset.tab === k); b.setAttribute('aria-selected', b.dataset.tab === k); });
    store.local.set(PREF[`${view}Tab`], k);
    if (S.route.params.get('tab') !== k) { S.route.params.set('tab', k); history.replaceState(null, '', hashFor(view, S.route.params)); }
  };
  tabs.forEach(([k, label]) => bar.append(h('button', { class: 'pillbtn stab', role: 'tab', 'data-tab': k, onclick: () => show(k) }, typeof label === 'function' ? label() : label)));
  bar.show = show;
  page.querySelector('.stab-mark').before(bar);
  show(pick);
}
/** What every settings row is built from: the maintenance data `m`, the page being drawn and the shared helpers
 * (row and button builders, the maintenance action runner `maint`, and the two field kinds that save one setting each). */
function settingsContext(m, view) {
  const again = () => { if (S.route.view === view) settingsPage(view); };
  const when = (t) => (t ? fmtWhen(t) : 'never');
  const maint = async (action, msg, confirmText) => {
    if (confirmText && !await confirmDialog(confirmText)) return;
    await act(() => post('/api/maintenance', { action }), { toast: msg });
    setTimeout(again, 1500);
  };
  const row = (label, detail, btn) => h('div', { class: 'srow' }, h('div', null, h('b', null, label), h('div', { class: 'muted note' }, detail)), btn);
  const btn = (label, fn, primary) => h('button', { class: 'pillbtn' + (primary ? ' on' : ''), onclick: fn }, label);
  /** A checkbox that saves one on/off setting the moment it is clicked, says what it did, and springs back if the save failed. */
  const switchField = (key, checked, { label = 'On', on, off, disabled = false, after, cls } = {}) => h('label', { class: cls, style: cls ? null : 'display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap' },
    h('input', { type: 'checkbox', checked, disabled, onchange: async (e) => {
      try { await post('/api/settings', { [key]: e.target.checked }); toast(e.target.checked ? on : off); if (after) after(); } catch (err) { e.target.checked = !e.target.checked; toastError(err); } } }), label);
  /** A Turn on / Turn off button for one setting; the page is drawn again with the new value. */
  const toggleButton = (key, on, after) => btn(on ? 'Turn off' : 'Turn on', async () => { try { await post('/api/settings', { [key]: !on }); if (after) await after(); } catch (e) { toastError(e); } settingsPage(view); });
  return { m, view, again, when, maint, row, btn, switchField, toggleButton };
}
/** Settings › Display: the name, appearance, units, badges, the info panel sections and the map provider. */
function displayTab(c) {
  const { m, view, row, btn } = c;
  return [
    stab('display'),
    h('h2', null, 'Display'),
    S.IS_ADMIN ? (() => {
      const inp = h('input', { class: 'einput', placeholder: 'ioPix', value: m.appNameSet || '', maxlength: 40, style: 'width:220px', onkeydown: (e) => { if (e.key === 'Enter') e.target.nextSibling.click(); } });
      return row('Name', `What ${S.APP_NAME} calls itself, for everyone: the top bar, the browser tab and the installed app. Leave it empty for iopix.`,
        h('span', { style: 'display:flex;gap:6px;align-items:center' }, inp, btn('Save', async () => { try { await post('/api/settings', { app_name: inp.value }); toast('Name saved.'); pollSoon(300); } catch (e) { toastError(e); } })));
    })() : null,
    row('Appearance', `Light (the default) or dark colors, or follow the device’s setting. ${S.USER ? 'Saved with your account, so it follows you to other devices.' : 'Remembered in this browser; sign in to keep it with an account.'}`,
      h('div', { class: 'eseg', id: 'themeseg' }, THEMES.map(([k, label]) => h('button', { class: store.get(PREF.theme, 'light') === k ? 'on' : '', 'data-theme': k, onclick: (e) => {
        store.set(PREF.theme, k); applyTheme(); for (const b of e.currentTarget.parentNode.children) b.classList.toggle('on', b === e.currentTarget); } }, label)))),
    row('Units', `How distances and heights read: the size of an approximate location, a day’s trail, altitude. Automatic follows this browser’s language and region, so miles and feet in the US, kilometres and metres in most other places. ${S.USER ? 'Saved with your account, so it follows you to other devices.' : 'Remembered in this browser; sign in to keep it with an account.'}`,
      h('div', { class: 'eseg', id: 'unitseg' }, UNITS.map(([k, label]) => h('button', { class: store.get(PREF.units, 'auto') === k ? 'on' : '', 'data-units': k, onclick: (e) => {
        store.set(PREF.units, k); for (const b of e.currentTarget.parentNode.children) b.classList.toggle('on', b === e.currentTarget); } }, k === 'auto' ? `${label} (${regionUsesMiles(navigator.language) ? 'miles' : 'kilometres'} here)` : label)))),
    row('Thumbnail badges', 'The small marks on thumbnails: face tags, GPS coordinates and favorites. This is remembered per browser.',
      h('label', { style: 'display:flex;gap:8px;align-items:center;cursor:pointer' }, h('input', { type: 'checkbox', checked: store.get(PREF.badges, '1') !== '0',
        onchange: (e) => { store.set(PREF.badges, e.target.checked ? '1' : '0'); applyBadgePref(); } }), 'Show badges')),
    row('Info panel', `The sections the photo info panel shows; the gear at the top of the panel opens the same choices. ${S.USER ? 'Saved with your account, so it follows you to other devices.' : 'Remembered in this browser; sign in to keep it with an account.'}`, infoPrefsEditor()),
    S.IS_ADMIN ? (() => {
      const sel = h('select', null, h('option', { value: 'osm', selected: m.mapProvider !== 'google' }, 'OpenStreetMap (no account needed)'), h('option', { value: 'google', selected: m.mapProvider === 'google' }, 'Google Maps (your API key)'));
      const keyIn = h('input', { class: 'einput', type: 'password', placeholder: m.googleMapsKeySet ? 'key is set; paste a new one to replace it' : 'Google Maps API key', autocomplete: 'off', style: 'width:260px' });
      const srvIn = h('input', { class: 'einput', type: 'password', placeholder: m.googleServerKeySet ? 'server key is set; paste a new one to replace it' : 'Server key (optional)', title: 'A second key, restricted by API and by this server’s address, for geocoding, places, elevation and Vision; without one the first key is used', autocomplete: 'off', style: 'width:260px' });
      return row('Map provider', `Maps and place search use ${m.mapProvider === 'google' && m.googleMapsKeySet ? 'Google Maps' : 'OpenStreetMap'}. Google needs an API key from the Google Cloud console with the Map Tiles API and Geocoding API enabled and billing on; restrict that key by HTTP referrer to ${S.APP_NAME}'s address, since browsers see it. An optional second key, restricted by API and by this server's address, is used for what the server calls (geocoding, places, elevation, Vision). Keys are stored in ${S.APP_NAME}'s settings and never shown again.`,
        h('span', { class: 'toolbar tight' }, sel, keyIn, srvIn, btn('Save', async () => {
          const body = { map_provider: sel.value }; if (keyIn.value.trim()) body.google_maps_key = keyIn.value.trim(); if (srvIn.value.trim()) body.google_server_key = srvIn.value.trim();
          if (sel.value === 'google' && !keyIn.value.trim() && !m.googleMapsKeySet) { toast('Enter the Google Maps API key first.'); return; }
          try { await post('/api/settings', body); S.mapsLoaded = false; toast('Map provider saved.'); } catch (e) { toastError(e); }
          settingsPage(view);
        })));
    })() : null,
  ];
}

/** Settings › ioContacts: the optional link to the suite's address book, for the birthdays of the people named here
 * (iocontacts.py). The mirror of ioContacts' own Settings › ioPix: the server reads the contacts, never the browser. */
function contactsTab(c) {
  const { row, btn } = c;
  const box = h('div', null, loadingBox('Loading…'));
  const draw = (s, test) => {
    const fixed = new Set(s.fromEnv);
    const inp = (k, ph, type = 'text', w = 220) => h('input', { class: 'einput', type, placeholder: ph, value: type === 'password' ? '' : ({ url: s.url, user: s.user, cf_client_id: s.cfId }[k] || ''),
      disabled: fixed.has(k), autocomplete: 'off', style: `margin:0;width:${w}px`, onkeydown: (e) => e.stopPropagation() });
    const f = { url: inp('url', 'https://contacts.example.com', 'text', 320), user: inp('user', 'ioContacts user'),
      password: inp('password', s.hasPassword ? 'app password (kept; type to replace)' : 'app password', 'password'),
      cf_client_id: inp('cf_client_id', 'Cloudflare client ID (optional)'),
      cf_client_secret: inp('cf_client_secret', s.hasCfSecret ? 'client secret (kept; type to replace)' : 'Cloudflare client secret', 'password') };
    const state = test ? (test.ok ? `Connected: ${nf.format(test.contacts)} contacts, birthdays for ${nf.format(test.matched)} ${test.matched === 1 ? 'person' : 'people'} here.` : `Saved, but ioContacts did not answer: ${test.error}`)
      : !s.enabled ? 'Off: no birthdays from ioContacts.'
      : s.error ? `The last try failed: ${s.error}`
      : s.fetched ? `Connected: birthdays for ${nf.format(s.matched)} ${s.matched === 1 ? 'person' : 'people'} here, from ${nf.format(s.contacts)} contacts.` : 'Not fetched yet.';
    const save = async (body) => {
      try { const r = await post('/api/iocontacts/settings', body); draw(r, r.test); } catch (e) { toastError(e); }
    };
    fill(box, row('ioContacts', `Birthdays from ioContacts, the ioThing address book. With it, the People page lists the birthdays coming up, the photo info shows how old each person was, and date suggestions use the birth years. It uses the people you linked to contacts in ioContacts (a contact’s ioPix person); link more there. ${S.APP_NAME} reads the contacts from the server; your browser never needs to reach ioContacts. Use an ioContacts account and an app password made for it (in ioContacts: Settings › Apps and devices). Behind Cloudflare Access, add a service token (Zero Trust › Access › Service auth) and allow it in the ioContacts application’s policy. Leave the address empty to turn this off.${fixed.size ? ' Some fields are set in iopix.env and can only be changed there.' : ''}`,
      h('div', null,
        h('div', { class: 'toolbar tight' }, f.url, f.user, f.password),
        h('div', { class: 'toolbar tight mt6' }, f.cf_client_id, f.cf_client_secret),
        h('div', { style: 'display:flex;gap:6px;margin-top:8px' },
          btn('Save and test', () => {
            const body = { url: f.url.value, user: f.user.value, cf_client_id: f.cf_client_id.value };
            if (f.password.value) body.password = f.password.value;
            if (f.cf_client_secret.value) body.cf_client_secret = f.cf_client_secret.value;
            for (const k of fixed) delete body[k];
            save(body);
          }, true),
          s.enabled && !fixed.has('url') ? btn('Turn off', () => save({ url: '' })) : null),
        h('div', { class: 'muted', style: `font-size:12.5px;margin-top:6px${(s.error && !test) || (test && !test.ok) ? ';color:var(--warn)' : ''}` }, state))));
  };
  api('/api/iocontacts/settings').then((s) => draw(s)).catch((e) => fill(box, e.message));
  return [stab('contacts'), h('h2', null, 'ioContacts'), box];
}

/** Settings › digiKam and server: the two databases, album roots, the update check, the MCP server, read-only mode and sync. */
function serverTab(c) {
  const { m, view, row, btn, again, switchField, toggleButton } = c;
  return [
    S.IS_ADMIN ? stab('server') : null,
    S.IS_ADMIN ? (() => {
      const d = m.database || {};
      const inp = (k, ph, type = 'text', w = 160) => h('input', { class: 'einput', type, placeholder: ph, value: type === 'password' ? '' : (d[k] ?? ''), autocomplete: 'off', style: `margin:0;width:${w}px`, onkeydown: (e) => e.stopPropagation() });
      const type = h('select', null, h('option', { value: 'sqlite', selected: d.type === 'sqlite' }, 'SQLite (digikam4.db file)'), h('option', { value: 'mysql', selected: d.type === 'mysql' }, 'MySQL / MariaDB'), h('option', { value: 'none', selected: d.type === 'none' }, 'None: I don’t use digiKam'));
      const f = { sqlite_dir: inp('sqlite_dir', 'folder with digikam4.db', 'text', 320), host: inp('host', 'host'), port: inp('port', 'port', 'text', 70), database: inp('database', 'database'), user: inp('user', 'user'),
        password: inp('password', d.password_set ? 'password (stored; leave empty to keep)' : 'password', 'password', 220) };
      const backup = h('select', null, [['auto', 'full backup only before deleting photos or a tag, or a merge'], ['skip', 'never (the server is backed up elsewhere)']].map(([v, l]) => h('option', { value: v, selected: (d.backup === 'skip' ? 'skip' : 'auto') === v }, l)));
      const sq = h('div', { class: 'toolbar tight mt6',}, f.sqlite_dir), my = h('div', { class: 'toolbar tight mt6',}, f.host, f.port, f.database, f.user, f.password);
      const show = () => { sq.hidden = type.value !== 'sqlite'; my.hidden = type.value !== 'mysql'; backup.hidden = test.hidden = type.value === 'none'; }; type.onchange = show;
      const payload = () => ({ type: type.value, sqlite_dir: f.sqlite_dir.value, host: f.host.value, port: f.port.value, database: f.database.value, user: f.user.value, password: f.password.value, backup: backup.value });
      const result = h('div', { class: 'muted', style: 'font-size:12.5px;margin-top:6px' });
      const test = btn('Test connection', async () => { fill(result, 'Testing…'); try { const r = await post('/api/db/test', payload()); fill(result, `OK: ${nf.format(r.images)} photos, ${nf.format(r.tags)} tags, DBVersion ${r.dbVersion}, album root ${r.roots.join(', ')}`); } catch (e) { fill(result, e.message); } });
      show();
      return row('digiKam database', d.type === 'none'
        ? `None: ${S.APP_NAME} keeps the library itself, in its own database, and the file scanner finds the photos in the photo folder. Tags, people and faces, captions, places and dates are saved there and in XMP sidecars beside the photos, which digiKam can read if you ever start using it. If you start using digiKam later, let it read the folder first (it picks up the sidecars), then choose its database here: ${S.APP_NAME} asks before it replaces its own library with digiKam’s.`
        : `Where digiKam keeps its database. Currently ${d.type === 'mysql' ? `MySQL ${d.user}@${d.host}:${d.port}/${d.database}` : `SQLite in ${d.sqlite_dir}`}. Changing it rebuilds the index from the other database within seconds; ${S.APP_NAME}'s own data (favorites, albums, users, analysis) stays. Test first.`,
        h('div', null, h('div', { class: 'toolbar tight',}, type, backup), sq, my,
          h('div', { style: 'display:flex;gap:6px;margin-top:8px' },
            test,
            btn('Save and switch', async () => { if (!await confirmDialog(type.value === 'none' ? `Stop using digiKam’s database? ${S.APP_NAME} keeps working from its own copy of the library.` : `Switch ${S.APP_NAME} to this digiKam database now?`)) return; try { await post('/api/settings', { database: payload() }); toast('Database settings saved; the index is rebuilding.'); } catch (e) { toastError(e); return; } setTimeout(again, 2500); }, true)),
          result));
    })() : null,
    S.IS_ADMIN ? (() => {
      const body = h('div', { class: 'muted small' }, 'Loading…');
      const draw = async () => {
        let d; try { d = await api('/api/roots'); } catch (e) { fill(body, e.message); return; }
        const inputs = new Map();
        fill(body, h('div', { class: 'stack',}, d.roots.map((r) => {
          const inp = h('input', { class: 'einput', placeholder: r.localPath, value: r.overridden ? r.localPath : '', style: 'width:340px', onkeydown: (e) => e.stopPropagation() });
          inputs.set(r.digikamPath, inp);
          return h('div', null, h('div', null, h('b', null, r.label || 'Album root'), ' ', h('span', { class: 'muted' }, `digiKam: ${r.digikamPath}`)),
            h('div', { style: 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:4px' }, inp,
              h('span', { class: 'muted smaller' }, r.exists ? `found: ${r.localPath} (${r.sample >= 2000 ? '2,000+' : nf.format(r.sample)} files)` : `not found: ${r.localPath}`)));
        }), h('div', { style: 'display:flex;gap:6px' },
          btn('Check', draw),
          btn('Save', async () => { const m = {}; inputs.forEach((inp, k) => { m[k] = inp.value.trim(); }); try { await post('/api/settings', { album_roots: m }); toast('Album roots saved; the index is rebuilding.'); } catch (e) { toastError(e); return; } setTimeout(draw, 2500); }, true))));
      };
      draw();
      return row('Album roots', `Where each album root digiKam knows is mounted here. Leave a field empty to use the default (PHOTOS_ROOT for a single-root library, else the path digiKam recorded); enter a path when the folder is mounted elsewhere, e.g. /photos in a container. Changing it rebuilds the index.`, body);
    })() : null,
    S.IS_ADMIN ? row(`${S.APP_NAME} database`, `Where ${S.APP_NAME} keeps its own data: users, albums, favorites, settings, pending edits, the AI results and its copy of the library: a MariaDB or MySQL database of its own (not digiKam’s). Moving to another one copies every row across and restarts ${S.APP_NAME}; the connection is kept in data/viewer-db.json, outside the database.`, viewerDbPanel()) : null,
    S.IS_ADMIN ? row('MCP server', m.mcpLocked ? 'Switched on by MCP=1 in the environment.'
      : `The endpoint at /mcp that lets an AI assistant (Claude, for example) search and read this library. Off: /mcp does not answer at all. On: every request needs the MCP update token (Maintenance › iopix version) as a bearer header, the read-only tools included.`,
      m.mcpLocked ? null : switchField('mcp_enabled', !!m.mcpEnabled, { on: 'The MCP server at /mcp is on.', off: 'The MCP server is off: /mcp does not answer.' })) : null,
    S.IS_ADMIN ? row('Read-only mode', m.readOnlyLocked ? `Locked on by READ_ONLY=1 in the environment: nothing in the library can be changed from ${S.APP_NAME}.`
      : `${m.readOnly ? 'On' : 'Off'}. When on, ${S.APP_NAME} only browses: editing, tagging, face naming, place tags, GPS tracks, metadata sync and database updates are switched off and refused by the server. Favorites and viewer albums keep working; they never touch the library.`,
      m.readOnlyLocked ? null : toggleButton('read_only', m.readOnly, async () => { const s = await api('/api/status'); S.EDITING = s.editing; S.CAN_MANAGE = s.editing && s.dbWrite; })) : null,
    S.IS_ADMIN ? (() => {
      const d = m.digikam || {};
      if (d.none) return null;  // no digiKam at all: nothing to sync with
      const copy = d.mirror ? `${nf.format(d.mirror.photos)} photos as of ${fmtWhen(d.mirror.taken)}` : 'not taken yet';
      if (d.conflict) {
        const planEl = h('div', { class: 'muted note mt6' }, 'Working out what changed…');
        api('/api/library/merge').then((p) => fill(planEl, p.total ? 'To merge: ' + p.summary.join(', ') + '.' : 'Nothing to merge.')).catch((e) => fill(planEl, e.message));
        const wrap = row('Sync with digiKam', `On, but ${plural(d.localChanges, 'change')} made while it was off ${d.localChanges === 1 ? 'is' : 'are'} not in digiKam’s database yet, so ${S.APP_NAME} keeps using its own library. Merge writes those changes into digiKam (new tags, folders and uploads get digiKam ids; renames, assignments, faces, ratings, captions and locations are applied; deleted photos are removed), after a backup of digiKam’s database. Replace drops them and copies the library from digiKam again. Sidecar edits are kept either way.`,
          h('span', { style: 'display:flex;gap:6px;flex-wrap:wrap' },
            btn('Merge into digiKam', async () => { if (!await confirmDialog('Write the changes made while sync was off into digiKam’s database? digiKam must be closed; its database is backed up first.')) return; try { const r = await post('/api/library/merge', {}); toast(`Merged: ${Object.entries(r.applied).map(([k, v]) => `${v} ${k.replace('_', ' ')}`).join(', ') || 'nothing to do'}${r.conflicts ? `; ${r.conflicts} rows digiKam had also changed (the viewer’s values won)` : ''}.`, null, null, 12000); } catch (e) { toastError(e, 10000); } setTimeout(again, 2500); }, true),
            btn('Replace with digiKam’s', async () => { if (!await confirmDialog(`Drop the ${plural(d.localChanges, 'change')} made while sync was off and copy the library from digiKam again? Edits in sidecars are kept.`)) return; await act(() => post('/api/settings', { library_replace: true }), { toast: 'Copying the library from digiKam…' }); setTimeout(again, 2500); }),
            btn('Turn sync off', async () => { await act(() => post('/api/settings', { digikam_sync: false })); settingsPage(view); })));
        wrap.firstChild.append(planEl);
        return wrap;
      }
      return row('Sync with digiKam', `Currently ${d.sync ? 'on' : 'off'}. On: ${S.APP_NAME} re-reads digiKam’s database whenever digiKam saves, and writes the edits made here into it whenever digiKam is closed (edits always go to the sidecars first). Off: ${S.APP_NAME} never opens digiKam’s database; the library lives in ${S.APP_NAME}’s own tables (${copy}) and everything works there: edits, tags, folders, uploads, faces and place tags. Turning sync on again does not overwrite what was changed meanwhile; you choose what happens.${d.sync && !d.reachable ? ' digiKam’s database is unreachable right now; the copy is in use.' : ''}`,
        toggleButton('digikam_sync', d.sync));
    })() : null,
  ];
}

/** Settings › Editing: where metadata is written, sidecar names, place tags and the metadata templates. */
function editingTab(c) {
  const { m, view, row, btn, toggleButton } = c;
  return [
    stab('editing'),
    h('h2', null, 'Editing'),
    row('Write metadata into image files', `Off: edits go to XMP sidecars only. On: also into JPEG, PNG, TIFF and WebP files, like digiKam’s “sidecar and file” mode. Currently ${m.writeFiles ? 'on' : 'off'}.`,
      toggleButton('write_files', m.writeFiles)),
    row('Sidecar file names', `New sidecars are written as ${m.sidecarCompat ? 'photo.xmp (digiKam’s “compatible file name” option)' : 'photo.jpg.xmp (digiKam’s default)'}. Existing sidecars are read either way; pick what your digiKam is set to.`,
      btn(m.sidecarCompat ? 'Use photo.jpg.xmp' : 'Use photo.xmp', async () => { await act(() => post('/api/settings', { sidecar_compat: !m.sidecarCompat })); settingsPage(view); })),
    h('h2', null, 'Place tags'),
    row('Tags from GPS positions', 'Turns each geotagged photo’s town into real digiKam tags under “Places/Country/Region/Town”, so places are searchable in digiKam as well. Missing tags are created in digiKam’s database (digiKam closed); assignments go to sidecars like any edit.',
      h('button', { class: 'pillbtn', onclick: placeTagsDialog }, icon('place'), 'Review and run')),
    templatesSection(m),
  ];
}

/** Maintenance › Library: import, the scanner, the trash, the library copy, rescans, and the checks summarised in one line each. */
function libraryTab(c) {
  const { m, row, btn, maint, when, again } = c;
  return [
    // 1. Library: everything about digiKam's data as the viewer sees it
    stab('library'),
    h('h2', null, 'Library'),
    row('Import', 'Photos and videos from a phone, a card or a computer, into folders by date, duplicates left out, a sidecar for each. The installed app is a share target on Android.', h('a', { class: 'pillbtn', href: '#/import' }, 'Open Import')),
    scannerRow(c),
    row('Lossless JPEG edits', S.LOSSLESS?.jpegtran ? 'jpegtran (libjpeg-turbo) is installed: JPEGs are rotated, flipped and cropped without re-encoding; other files by their orientation flag.'
      : 'jpegtran (libjpeg-turbo) is not installed on the server, so JPEGs are turned by their orientation flag only and cannot be cropped. Install it there, as root: apt-get install -y libjpeg-turbo-progs (Debian, Ubuntu, Proxmox LXC); dnf install libjpeg-turbo-utils; brew install jpeg-turbo. Nothing needs restarting.',
      h('span', { class: 'mst ' + (S.LOSSLESS?.jpegtran ? 'ok' : 'warn') }, S.LOSSLESS?.jpegtran ? 'installed' : S.LOSSLESS ? 'missing' : 'editing off')),
    row('Trash', 'Photos moved to the trash here or in digiKam (each album root’s .dtrash folder), with their sidecars, until restored or deleted for good.', h('a', { class: 'pillbtn', href: '#/trash' }, 'Open the trash')),
    row('Library copy', (() => { const d = m.digikam || {}; return d.mirror ? `${S.APP_NAME}’s own copy of the library’s metadata: ${nf.format(d.mirror.photos)} photos as of ${fmtWhen(d.mirror.taken)}, refreshed at every read of digiKam and included in the app backup. The index is built from it whenever digiKam’s database is unreachable or Sync with digiKam is off${d.source === 'mirror' ? ' (as it is now)' : ''}.` : 'Not taken yet; it is taken at the next read of digiKam’s database.'; })(), null),
    row('Rescan the library now', `The index is rebuilt from digiKam’s database whenever digiKam saves (last saved ${when(m.digikamSaved)}; index built ${when(m.indexBuilt)}). Force it here if something looks stale.`,
      btn('Rescan now', () => maint('reindex', 'Rescanning the library…'), true)),
    (() => {  // metadata consistency, summarized in one line
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
    (() => {  // face quality, summarized in one line; the page has the details and the fixes
      const line = h('span', null, 'Checking…');
      api('/api/quality').then((q) => fill(line, (q.near.extra ? `${plural(q.near.extra, 'face is', 'faces are')} stored twice. ` : 'No face is stored twice. ')
        + (q.separate.count ? `${plural(q.separate.count, 'photo has', 'photos have')} the same person in two places. ` : '')
        + (q.unnamed.count ? `${plural(q.unnamed.count, 'photo has', 'photos have')} faces but no names. ` : '')
        + (q.untagged?.count ? `${plural(q.untagged.count, 'photo shows', 'photos show')} people but nobody is tagged. ` : '')
        + (q.invalid.count ? `${plural(q.invalid.count, 'box is', 'boxes are')} outside the photo or empty.` : ''))).catch(() => fill(line, ''));
      return row('Metadata quality', h('span', null, line, ' Photos without a location, with an implausible date or in a folder for another date; duplicate faces, the same person twice, and boxes outside the photo.'), h('a', { class: 'pillbtn on', href: '#/quality' }, 'Open'));
    })(),
    (() => {  // duplicate files, summarised in one line; the page lists the sets
      const line = h('span', null, '');
      api('/api/duplicates').then((d) => fill(line, d.groups.length ? `${plural(d.groups.length, 'set')} of byte-identical files, ${fmtBytes(d.wasted)} that could be freed. ` : 'No byte-identical copies. ')).catch(() => fill(line, ''));
      return row('Duplicate files', h('span', null, line, `Copies of the same file, from digiKam’s own fingerprints. ${S.APP_NAME} never deletes anything; remove the extras in digiKam.`),
        h('a', { class: 'pillbtn on', href: '#/duplicates' }, 'Open'));
    })(),
    // the suggestion pages, each summarised in a line from a one-item read of its list (the total comes with it)
    ...[['datesuggest', 'Date suggestions', 'A year for each photo marked Date unknown, from the ages of the people in it, the photos they appear in, the camera and the look of the file; folder hints bound the guesses.', 'suggestion'],
      ['placesuggest', 'Place suggestions', 'A position for each photo without one, from where the photos around it in time were taken.', 'suggestion'],
      ['jerseys', 'Jersey numbers', 'Players read from shirt numbers where the face is not named, when one player clearly wears the number in the same folder or year.', 'suggestion'],
      ['clothing', 'Clothing matches', 'People recognised by what they wear in the same event, where their face is not named or not visible.', 'suggestion'],
      ['rotation', 'Wrongly rotated', 'Photos that read as on their side or upside down, each shown the way it would look after the turn.', 'photo']].map(([view, label, text, noun]) => {
      const line = h('span', null, '');
      api(`/api/${view}?limit=1`).then((d) => fill(line, d.ready === false ? 'Not analysed yet. ' : `${d.total ? plural(d.total, noun) : `No ${noun}s`}${d.coverage && d.coverage[1] ? ` (${nf.format(d.coverage[0])} / ${nf.format(d.coverage[1])} analysed)` : ''}. `)).catch(() => fill(line, ''));
      return row(label, h('span', null, line, text), h('a', { class: 'pillbtn on', href: `#/${view}` }, 'Open'));
    }),
    (() => {  // damaged files, summarized in one line; the page has the list and the repair
      const line = h('span', null, '');
      api('/api/filecheck').then((d) => fill(line, d.progress.running ? `Checking… ${nf.format(d.progress.done)} / ${nf.format(d.progress.total)} files. `
        : !d.checked ? 'Not checked yet. ' : d.found.length ? `${plural(d.found.length, 'damaged file')} found (${nf.format(d.found.filter((x) => x.fixable).length)} repairable). ` : 'Nothing damaged found. ')).catch(() => fill(line, ''));
      return row('Damaged files', h('span', null, line, 'Every file read and checked: JPEGs that stop early or lack their end marker, damaged PNGs, pictures that cannot be decoded; a safe repair where there is one.'),
        h('a', { class: 'pillbtn on', href: '#/damaged' }, 'Open'));
    })(),
    S.CAN_MANAGE ? (() => {  // digiKam's pick labels, which iopix does not use: counted, and cleared on request
      const line = h('span', null, '');
      const boxes = { 1: h('input', { type: 'checkbox', checked: true }), 2: h('input', { type: 'checkbox' }), 3: h('input', { type: 'checkbox' }) };
      const names = { 1: 'Rejected', 2: 'Pending', 3: 'Accepted' };
      const wrap = h('span', { class: 'toolbar' });
      api('/api/picklabels').then((d) => {
        const c = d.counts, p = d.progress;
        fill(line, p.running ? `Clearing… sidecars ${nf.format(p.done)} / ${nf.format(p.total)}. ` : `${['Rejected', 'Pending', 'Accepted'].map((n, i) => `${n}: ${nf.format(c[i + 1])}`).join(', ')}. `);
        fill(wrap, [1, 2, 3].map((k) => h('label', { class: 'chk' }, boxes[k], names[k])), btn('Clear…', async () => {
          const ks = [1, 2, 3].filter((k) => boxes[k].checked), n = ks.reduce((a, k) => a + c[k], 0);
          if (!ks.length || !n) { toast('No photo carries the chosen labels.'); return; }
          if (!await confirmDialog(`Take the ${ks.map((k) => names[k]).join(', ')} pick label off ${plural(n, 'photo')}? digiKam’s database is backed up first, sidecars that carry the label are cleaned too (each backed up), and every photo’s change is in the edit log with undo.`, { ok: 'Clear', danger: true })) return;
          try { const r = await post('/api/picklabels/clear', { levels: ks }); toast(`Pick label cleared from ${plural(r.cleared, 'photo')}.`); } catch (e) { toastError(e); }
          again();
        }, true));
      }).catch(() => fill(line, ''));
      return row('Pick labels', h('span', null, line, `digiKam’s pick labels. ${S.APP_NAME} does not use them and never sets them; clear the ones an older tool left behind.`), wrap);
    })() : null,
    S.EDITING ? row('Edits waiting for digiKam', m.pendingEdits ? `${plural(m.pendingEdits, 'photo has', 'photos have')} edits saved in sidecars that digiKam’s database does not have yet. digiKam is ${m.digikamRunning ? 'running, so they cannot be applied right now' : 'closed, so they can be applied now'}.` : 'None. digiKam’s database and the sidecars agree.',
      m.pendingEdits ? btn('Apply to digiKam database…', dbDialog, !m.digikamRunning) : null) : null,
    row('Tag cover photos', 'The photo shown for each tag is chosen by the content model. Pick them again after big tagging changes.', btn('Pick again', () => maint('covers', 'Choosing tag covers in the background.'))),
  ];
}

/** Maintenance › Storage and backups: what each cache and backup takes on disk, and the backup downloads. */
function storageTab(c) {
  const { m, row, maint } = c;
  const st = m.storage;
  return [
    // 3. Storage and backups: each item's size with what can be done about it on the same row
    stab('storage'),
    h('h2', null, 'Storage and backups', h('span', { class: 'muted' }, `${fmtBytes(st.free)} free on the disk`)),
    (() => {
      const item = (label, size, detail, action) => h('tr', null, h('td', null, label, detail ? h('div', { class: 'muted', style: 'font-size:11.5px;white-space:normal' }, detail) : null),
        h('td', { class: 'num' }, fmtBytes(size) || '0 B'), h('td', { class: 'act' }, action || null));
      const small = (label, fn) => h('button', { class: 'pillbtn small', onclick: fn }, label);
      return h('table', { class: 'stable wide' }, h('tbody', null,
        item('Thumbnails', st.thumbnails, 'grid previews, rebuilt in the background (several minutes)', small('Rebuild', () => maint('thumbs', 'Rebuilding thumbnails in the background.', `Delete all grid thumbnails (${fmtBytes(st.thumbnails)}) and rebuild them?`))),
        item('Previews', st.previews, `large images for ${S.APP_NAME}, remade on demand`, small('Clear', () => maint('previews', 'Previews cleared; they are remade as you open photos.'))),
        item('Face crops', st.faces, 'remade on demand', small('Clear', () => maint('faces', 'Face crops cleared; they are remade as needed.'))),
        item('Search index', st.index, 'rebuilt from digiKam’s database in seconds'),
        item(`${S.APP_NAME} database`, st.viewerDb, st.viewerDbError ? h('span', { class: 'warn' }, `MySQL/MariaDB is configured but could not be used when ${S.APP_NAME} started (${st.viewerDbError}), so the SQLite file is in use since then: anything saved now is not in the MySQL database. Fix the cause (Settings › ${S.APP_NAME} database) and restart ${S.APP_NAME}.`) : 'users, albums, favorites, settings, AI results'),
        item('Content embeddings', st.embeddings, 'the content-search index'),
        item('AI models', st.models),
        item('Sidecar backups', st.backups, `each sidecar as it was before ${S.APP_NAME} first changed it`),
        item('Image file backups', st.fileBackups, 'the original of each file whose metadata was rewritten'),
        item('Database backups', st.dbBackups, 'digiKam’s database before each write (the last 10)'),
        item('Deleted photos', st.deletedPhotos, 'copies of deleted photos and their sidecars, kept until you remove them (data/deleted-photos)')));
    })(),
    h('div', { class: 'muted', style: 'font-size:12.5px;margin-top:8px;max-width:820px' }, 'Backups are never deleted automatically (apart from keeping the last ten database copies); remove them by hand once you are happy with the corresponding changes.'),
    row('Download a backup', S.NO_DK ? `${S.APP_NAME}’s data as a zip: its database, which holds the whole library (folders, tags, people and faces, captions, places), plus the content-search index. Keep it somewhere else.` : `digiKam’s database as it is right now (a consistent copy of digikam4.db, or a mysqldump for MariaDB), and ${S.APP_NAME}’s data as a zip (its database plus the content-search index). Keep them somewhere else.`,
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, S.NO_DK ? null : h('a', { class: 'pillbtn', href: '/api/backup/digikam', download: '' }, icon('download'), h('span', null, 'digiKam database')),
        h('a', { class: 'pillbtn', href: '/api/backup/app', download: '' }, icon('download'), h('span', null, `${S.APP_NAME} data (zip)`)))),
  ];
}

/** Maintenance › the app itself: the edit log, the server log, the version and updates, and restart. */
/** Maintenance › Updates and logs: the version card, the daily check, the MCP admin token, the server log and Restart, from
 *  iosuite-shared's /api/server and /api/log, so the page is the same in every ioThing app. */
function viewerTab(c) {
  const { view, row, btn } = c;
  return [
    // 4. Updates and logs
    stab('updates'),
    h('h2', null, 'Updates and logs'),
    row('Edit log', `Every change made in ${S.APP_NAME}, for every photo, kept for good: who, when, what it was before and after, with Undo and restore of the sidecar or the original file from the backups.`, h('a', { class: 'pillbtn', href: '#/history' }, 'Open the edit log')),
    row('Server log', `What the server has been doing: edits, database writes, updates, background jobs and any errors, from ${S.APP_NAME}’s own log file in the data folder.`, btn('Show the log', logDialog)),
    (() => {
      const body = h('div', { class: 'muted note' }, 'Loading…');
      let timer = 0;
      const draw = async () => {
        let u; try { u = await api('/api/server'); } catch (e) { fill(body, e.message); return; }
        if (!body.isConnected) return;
        const c = u.check, p = u.progress, last = u.lastUpdate;
        const outcome = !p.running && !p.error && last ? h('div', { class: last.ok ? 'muted' : 'warn', style: 'font-size:12px;margin-top:2px' }, `last update: ${last.text}`) : null;
        const state = p.running ? `updating… ${S.APP_NAME} restarts when it is done` : p.error ? `last update failed: ${p.error}` : !c ? `v${u.version}` : c.status === 'current' ? `v${u.version} is the latest version` : c.status === 'update' ? `You have v${u.version}; ${c.remoteVersion ? `v${c.remoteVersion}` : 'a newer build'} is available${c.remoteDate ? ` (${c.remoteDate})` : ''}${c.remoteMessage ? `: ${c.remoteMessage.replace(/^v\d+\.\d+\.\d+:\s*/, '')}` : ''}` : `could not check: ${c.error}`;
        fill(body, state, outcome, h('div', { style: 'display:flex;gap:6px;margin-top:6px' },
          btn('Check for updates', async () => { fill(body, 'Checking…'); await act(() => post('/api/server', { action: 'check' })); draw(); }),
          btn('MCP update token…', async () => { try { const t = await post('/api/server', { action: 'token' }); const back = modal('MCP update token', u.mcpEnabled ? null : h('p', { class: 'warn' }, 'The MCP server is switched off (Settings › MCP server), so this token is not usable until it is turned on.'), h('p', { class: 'muted' }, 'An MCP client (e.g. Claude) can run the update with the update_viewer tool and this token; the endpoint itself has no sign-in, so treat it like a password.'), h('div', { class: 'einput', style: 'font-family:monospace;user-select:all;word-break:break-all' }, t.token), t.fromEnv ? h('p', { class: 'muted' }, 'Set by MCP_ADMIN_TOKEN in the environment.') : null,
            { buttons: [t.fromEnv ? null : ['Regenerate', async () => { if (!await confirmDialog('Generate a new token? The old one stops working.')) return; await act(() => post('/api/server', { action: 'regenerate_token' })); back.remove(); }], 'Close'] }); } catch (e) { toastError(e); } }),
          u.docker ? h('span', { class: 'muted' }, 'Docker: update by pulling the image (docker compose pull && docker compose up -d).')
            : u.canRun ? btn('Update now', async () => {
              if (!await confirmDialog(`Fetch the latest version, refresh the packages and restart ${S.APP_NAME}? The page reloads when the new version is up.`)) return;
              if (await runUpdate((text, failed) => { if (failed) toast(text, null, null, 15000); })) { toast(`Updating; ${S.APP_NAME} restarts when it is done, and the page reloads.`); setTimeout(draw, 3000); }
            }, c && c.status === 'update') : null,
          h('a', { class: 'pillbtn', href: `https://github.com/${u.repo}/commits/${u.branch}`, target: '_blank', rel: 'noopener' }, 'Change log')),
          h('label', { style: 'display:flex;gap:8px;align-items:center;cursor:pointer;margin-top:8px' }, h('input', { type: 'checkbox', checked: !!u.autoCheck, disabled: !!u.autoCheckLocked, onchange: async (e) => {
            try { await post('/api/server', { autoCheck: e.target.checked }); toast(e.target.checked ? `${S.APP_NAME} will check for updates once a day.` : `${S.APP_NAME} will not contact GitHub on its own.`); pollSoon(2000); } catch (err) { e.target.checked = !e.target.checked; toastError(err); } } }),
            u.autoCheckLocked ? 'Check daily (switched off by UPDATE_CHECK=0 in the environment)' : 'Check daily (only GitHub’s public list of changes is asked; nothing about this install is sent)'));
        clearTimeout(timer);
        if (p.running) timer = setTimeout(() => { if (S.route.view === view && body.isConnected) draw(); }, 5000);
      };
      draw();
      return row(`${S.APP_NAME} version`, `Which version of ${S.APP_NAME} is running here, and whether a newer one is out. “Update now” installs the newest version, keeps your settings and data, and restarts ${S.APP_NAME}; the page reloads when it is back.`, body);
    })(),
    row(`Restart ${S.APP_NAME}`, 'Stops the server; launchd, Docker or systemd starts it again within seconds. Needed after an update of the RapidOCR package (offered in the models table above when “Check models for updates” finds a newer one), and handy when a background job looks stuck.',
      btn('Restart', async () => { if (!await confirmDialog(`Restart ${S.APP_NAME} now?`)) return; await act(() => post('/api/server', { action: 'restart' }), { toast: 'Restarting…' }); })),
  ];
}
async function settingsPage(view) {
  let m;
  if (!main.querySelector('.spage')) main.replaceChildren(h('div', { class: 'page spage' }, loadingBox(view === 'settings' ? 'Loading the settings…' : 'Loading…')));
  try { m = await api('/api/maintenance'); } catch (e) { if (S.route.view === view) main.replaceChildren(h('div', { class: 'empty' }, 'Could not load settings: ' + e.message)); return; }
  if (S.route.view !== view) return;
  const c = settingsContext(m, view);
  const { row, btn } = c;
  main.replaceChildren(h('div', { class: 'page spage' },
    pageTitle(view === 'settings' ? 'gear' : 'wrench', 'slate', view === 'settings' ? 'Settings' : 'Maintenance', h('span', { class: 'muted' }, `${S.APP_NAME} v${S.APP_VERSION || '?'}`)),
    h('div', { class: 'muted intro' }, m.paths ? `Library: ${m.paths.library} (${nf.format(m.photos)} photos and videos). ${m.paths.digikamDb === 'none' ? `No digiKam: the library lives in ${S.APP_NAME}’s own database` : `digiKam database: ${m.paths.digikamDb}`}. Everything ${S.APP_NAME} writes lives in ${m.paths.data}.` : `${nf.format(m.photos)} photos in the library.`),
    !S.USERS_EXIST ? row('No users yet', `${S.APP_NAME} is open to anyone who can reach it. Create the admin account to switch on sign-in: visitors then browse read-only, and only editors and admins can change the library.`, btn('Create the admin account', setupDialog, true)) : null,
    ...(view === 'settings' ? [
    displayTab(c),
    S.IS_ADMIN ? serverTab(c) : null,
    S.IS_ADMIN ? stab('ai') : null,
    S.IS_ADMIN ? analysisSection(view, 'prefs') : null,
    S.IS_ADMIN ? contactsTab(c) : null,
    S.IS_ADMIN && S.USERS_EXIST ? stab('users') : null,
    S.IS_ADMIN && S.USERS_EXIST ? usersSection() : null,
    S.IS_ADMIN && S.EDITING ? editingTab(c) : null,
    ] : [
    libraryTab(c),
    // 2. AI analysis and models
    stab('models'),
    aiSection(view),
    storageTab(c),
    viewerTab(c),
    ])));
  settingsTabs(main.querySelector('.spage'), view);
}

/** The viewer's own database (MariaDB/MySQL): where it is, whether it answers, and the fields to test and move to
 *  another one. The same panel serves Settings and the set-up page the app shows while it has no database at all
 *  (main.js), where `setup` makes the button say so and the page reload once the viewer restarts. */
export function viewerDbPanel({ setup = false } = {}) {
  const body = h('div', { class: 'muted note' }, 'Loading…');
  const btn = (label, fn, primary) => h('button', { class: 'pillbtn' + (primary ? ' on' : ''), onclick: fn }, label);
  (async () => {
    let d; try { d = await api('/api/viewerdb'); } catch (e) { fill(body, e.message); return; }
    if (d.locked) {  // configured but not answering, and nobody signed in: only Retry (the connection is changed in data/viewer-db.json or VIEWER_DB_*)
      const result = h('div', { class: 'muted', style: 'font-size:12.5px;margin-top:6px' });
      fill(body, h('div', null, `${S.APP_NAME}’s database is configured but not answering: `, h('span', { class: 'warn' }, d.error || ''),
        h('div', { class: 'muted note mt6' }, 'Start the database server (or wait for it) and retry. To use another server, change data/viewer-db.json or the VIEWER_DB_* variables and restart.')),
        h('div', { class: 'btnrow', style: 'margin-top:8px' },
          btn('Retry the configured database', async () => { fill(result, 'Trying…'); try { await post('/api/viewerdb/retry', {}); fill(result, 'It answers now. Restarting…'); setTimeout(() => location.reload(), 4000); } catch (e) { fill(result, e.message); } }, true)), result);
      return;
    }
    const inp = (k, ph, type = 'text', w = 160) => h('input', { class: 'einput', type, placeholder: ph, value: type === 'password' ? '' : (d[k] ?? ''), autocomplete: 'off', style: `margin:0;width:${w}px`, onkeydown: (e) => e.stopPropagation() });
    const f = { host: inp('host', 'host'), port: inp('port', 'port', 'text', 70), database: inp('database', 'database'), user: inp('user', 'user'),
      password: inp('password', d.password_set ? 'password (stored; leave empty to keep)' : 'password', 'password') };
    const fields = h('div', { class: 'toolbar tight mt6' }, f.host, f.port, f.database, f.user, f.password);
    const hasData = d.configured && !d.error;
    const copy = h('input', { type: 'checkbox', checked: true }), drop = h('input', { type: 'checkbox' });  // dropping is opt-in: the target may be a database in use
    const copyLabel = hasData ? 'Copy all the data from the current database to the new one' : d.legacy_sqlite ? `Import everything from the ${S.APP_NAME} file of before (${d.legacy_sqlite}); the file is renamed afterwards` : null;
    const opts = h('div', { style: 'display:grid;gap:4px;margin-top:8px;font-size:13px' },
      copyLabel ? h('label', { class: 'chk' }, copy, copyLabel) : null,
      h('label', { class: 'chk' }, drop, `Drop and recreate ${S.APP_NAME}’s tables at the target first (a clean slate; anything already there is lost)`));
    const payload = () => ({ host: f.host.value, port: f.port.value, database: f.database.value, user: f.user.value, password: f.password.value });
    const result = h('div', { class: 'muted', style: 'font-size:12.5px;margin-top:6px' });
    const restartThen = (msg) => { fill(result, msg + ' Restarting…'); setTimeout(() => { if (setup) location.reload(); else pollSoon(2000); }, 4000); };
    const current = !d.configured ? `No database yet: ${S.APP_NAME} needs a MariaDB or MySQL database of its own. Create an empty one with a user that owns it, enter its details and press Use this database.`
      : `${d.error ? 'Configured' : 'Currently'}: MySQL ${d.user}@${d.host}:${d.port}/${d.database}`;
    fill(body, h('div', null, current, d.error && d.configured ? h('span', { class: 'warn' }, ` · not answering: ${d.error}`) : null),
      fields, opts,
      h('div', { class: 'btnrow', style: 'margin-top:8px' },
        btn('Test connection', async () => { fill(result, 'Testing…'); try { const r = await post('/api/viewerdb/test', payload()); const t = Object.entries(r.tables); fill(result, `OK: ${r.version}. ${t.length ? `${S.APP_NAME} tables already there: ` + t.map(([k, n]) => `${k} ${nf.format(n)}`).join(', ') : 'An empty database.'}`); } catch (e) { fill(result, e.message); } }),
        d.configured && d.error ? btn('Retry the configured database', async () => { fill(result, 'Trying…'); try { await post('/api/viewerdb/retry', {}); restartThen('It answers now.'); } catch (e) { fill(result, e.message); } }) : null,
        btn(d.configured ? 'Switch' : 'Use this database', async () => {
          const p = payload();
          if (!await confirmDialog(`${d.configured ? 'Switch' : 'Set up'} ${S.APP_NAME} on MySQL ${p.user}@${p.host}:${p.port}/${p.database}?` + (copyLabel && copy.checked ? `\n\nEvery row is copied there first${drop.checked ? ', after dropping and recreating the tables there' : ''}.` : drop.checked ? '\n\nThe tables there are dropped and created again.' : '') + `\n\n${S.APP_NAME} restarts on the new database.`, { ok: d.configured ? 'Switch' : 'Use it' })) return;
          fill(result, 'Copying… this can take a minute on a large library.');
          try { const r = await post('/api/viewerdb/switch', { ...p, copy: !!copyLabel && copy.checked, drop: drop.checked }); restartThen(`Set up on ${r.database}: ` + Object.entries(r.copied).map(([k, n]) => `${k} ${nf.format(n)}`).join(', ') + '.'); } catch (e) { fill(result, e.message); }
        }, true)), result);
  })();
  return body;
}
