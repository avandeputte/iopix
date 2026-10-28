/** Zip downloads. */
import { $, api, fill, fmtBytes, h, icon, nf, send, store } from './core.js';
import { toast } from './state.js';
import { chipList } from './search.js';
import { modal } from './actions.js';
import { details } from './lightbox.js';

export async function downloadDialog(p, selected) {
  p = new URLSearchParams(p);
  p.delete('sort');
  const name = selected ? `${selected} selected photos` : (chipList(p).map((c) => c.label.replace(/[“”]/g, '')).join(' ') || 'photos').slice(0, 80);
  const side = h('input', { type: 'checkbox' });
  const size = h('select', null, [[1000, '1 GB'], [2000, '2 GB'], [4000, '4 GB'], [8000, '8 GB']].map(([v, l]) => h('option', { value: v, selected: v === 2000 }, l)));
  const pick = (key, opts, dflt) => h('select', { onchange: () => store.set(key, sel[key].value) }, opts.map(([v, l]) => h('option', { value: v, selected: String(v) === store.get(key, dflt) }, l)));
  const sel = {};
  sel['exp-size'] = pick('exp-size', [[0, 'Original size'], [4096, 'Large (4096 px)'], [2048, 'Medium (2048 px)'], [1280, 'Small (1280 px)']], '0');
  sel['exp-fmt'] = pick('exp-fmt', [['keep', 'Same format'], ['jpeg', 'JPEG (HEIC, PNG and others converted)']], 'keep');
  sel['exp-name'] = pick('exp-name', [['keep', 'Their own names'], ['date', 'Date and time (2026-10-17_143205)'], ['date-name', 'Date and name (2026-10-17_IMG_1234)'], ['date-seq', 'Date and number (2026-10-17_001)']], 'keep');
  const nogps = h('input', { type: 'checkbox', checked: store.get('exp-nogps', '0') === '1', onchange: () => store.set('exp-nogps', nogps.checked ? '1' : '0') });
  const flat = h('input', { type: 'checkbox', checked: store.get('exp-flat', '0') === '1', onchange: () => store.set('exp-flat', flat.checked ? '1' : '0') });
  const body = h('div', null, h('p', null, 'Working out the download…'));
  const back = modal('Download', body,
    h('div', { class: 'expopts' },
      h('label', null, h('span', { class: 'muted' }, 'Size'), sel['exp-size']), h('label', null, h('span', { class: 'muted' }, 'Format'), sel['exp-fmt']),
      h('label', null, h('span', { class: 'muted' }, 'File names'), sel['exp-name'])),
    h('label', { class: 'mt10',}, nogps, ' Remove the location (GPS and place names) from the copies'),
    h('label', { class: 'mt6',}, flat, ' All files in one folder (no album folders)'),
    h('label', { class: 'mt6',}, side, ' Include digiKam XMP sidecar files'),
    h('label', { class: 'mt8',}, 'Split into zip files of at most ', size),
    h('p', { class: 'muted', style: 'font-size:12.5px;margin:8px 0 0' }, 'Copies are made for the download; the photos in the library are not changed. Resized and converted copies keep their dates, camera details, keywords and captions. Videos keep their size and format.'),
    h('div', { class: 'modal-btns' }, h('button', { class: 'pillbtn', onclick: () => back.remove() }, 'Close')));
  // the browser downloads each part through a hidden form, so even huge selections work
  const fetchPart = (i) => {
    const q = new URLSearchParams(p);
    q.set('name', name); q.set('part', i); q.set('part_mb', size.value);
    if (side.checked) q.set('sidecars', '1');
    if (+sel['exp-size'].value) q.set('xsize', sel['exp-size'].value);
    if (sel['exp-fmt'].value !== 'keep') q.set('xfmt', sel['exp-fmt'].value);
    if (sel['exp-name'].value !== 'keep') q.set('xname', sel['exp-name'].value);
    if (nogps.checked) q.set('nogps', '1');
    if (flat.checked) q.set('flat', '1');
    if (!document.querySelector('iframe[name=dlframe]')) document.body.append(h('iframe', { name: 'dlframe', style: 'display:none', title: 'download' }));
    const form = h('form', { method: 'post', action: '/api/zip', target: 'dlframe', style: 'display:none' }, [...q].map(([k, v]) => h('input', { type: 'hidden', name: k, value: v })));
    document.body.append(form); form.submit(); form.remove();  // an error reply lands in the hidden frame, not over the app
  };
  const plan = async () => {
    const q = new URLSearchParams(p);
    q.set('part_mb', size.value);
    let r;
    try {
      r = await send('/api/zip/plan', q);
    } catch (e) { fill(body, h('p', { class: 'warn' }, String(e.message || e))); return; }
    if (!r.files) { fill(body, h('p', null, 'Nothing to download.')); return; }
    const one = r.parts.length === 1;
    fill(body,
      h('p', null, `${nf.format(r.files)} ${r.files === 1 ? 'file' : 'files'}, ${fmtBytes(r.bytes)} as originals${+sel['exp-size'].value || sel['exp-fmt'].value !== 'keep' ? ' (less once resized or converted)' : ''}${one ? '.' : `, split into ${r.parts.length} zip files.`}`),
      one ? h('button', { class: 'pillbtn on', onclick: () => { fetchPart(0); toast('Download started.'); } }, icon('download'), 'Download')
        : [h('div', { class: 'alist' }, r.parts.map((pt, i) => { const b = h('div', { class: 'frow', title: `${pt.first} … ${pt.last}`, onclick: () => { fetchPart(i); b.classList.add('on'); } },
            icon('download'), h('span', { class: 'nm' }, `Part ${i + 1}`, h('span', { class: 'muted' }, `  ${pt.first === pt.last ? pt.first : pt.first + ' … ' + pt.last}`)), h('span', { class: 'ct' }, `${nf.format(pt.files)} · ${fmtBytes(pt.bytes)}`)); return b; })),
          h('p', { class: 'muted', style: 'margin:10px 0 0;font-size:12.5px' }, 'Click each part to download it. Parts follow folder order, and unzipping them all into one folder gives the complete set.')]);
  };
  size.onchange = plan;
  plan();
}
