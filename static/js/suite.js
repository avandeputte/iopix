/** The ioThing suite's other apps (iothing-auth's suite_apps, GET /api/auth/suite). When other apps are registered,
 *  the iopix logo top left (in the sidebar; in the top bar on a phone) opens a menu of them, each a link to that app.
 *  Alone, the logo and the name are the link to Explore and nothing else changes. Signed-in accounts only (the list needs one). */
import { $, api, h } from './core.js';
import { S, brandName } from './state.js';

let apps = [];
let pop = null;
let loadedFor;  // the account the list was loaded for: loaded again when it changes

export async function loadSuite() {
  const who = S.USER ? S.USER.id : null;
  if (who === loadedFor) return;
  loadedFor = who;
  let list = [];
  if (who != null) {
    try {
      const r = await api('/api/auth/suite');
      list = (r.items || []).filter((a) => a.app !== r.app && a.url);
    } catch { list = []; }  // a convenience: no menu rather than an error
  }
  if (loadedFor !== who) return;  // the account changed meanwhile
  apps = list;
  draw();
}

/** The logo top left opens the menu when there are other apps, and the name beside it stays the link to Explore;
 *  alone, logo and name are one link to Explore. On a phone the sidebar is the bottom bar, so the top bar shows the
 *  logo button instead. */
const mark = () => h('img', { src: 'icons/mark.svg', alt: '', class: 'brand-mark' });
function suiteButton(id) {
  return h('button', { type: 'button', id, class: 'suitebtn', title: 'Other ioThing apps', 'aria-haspopup': 'menu', 'aria-label': 'Open another ioThing app',
    onclick: (e) => toggleSuite(e.currentTarget) }, mark());
}
function draw() {
  closeSuite();
  const head = $('#nav .navhead'), old = head && head.querySelector('.brand');
  if (old) {
    const title = old.title || 'Home';
    const brand = apps.length
      ? h('div', { class: 'brand', title }, suiteButton(null), h('a', { class: 'brand-home', href: '#/explore', title: 'Home' }, brandName()))
      : h('a', { class: 'brand', href: '#/explore', title }, mark(), brandName());
    old.replaceWith(brand);
  }
  let top = $('#suitebtn');
  if (apps.length && !top) $('#topbar').prepend(suiteButton('suitebtn'));
  else if (!apps.length && top) top.remove();
}

export function closeSuite() {
  if (!pop) return;
  pop.remove(); pop = null;
  document.querySelectorAll('.suitebtn[aria-expanded]').forEach((b) => b.removeAttribute('aria-expanded'));
  document.removeEventListener('click', outside, true);
  document.removeEventListener('keydown', onKey, true);
  removeEventListener('resize', closeSuite);
}
function outside(e) { if (pop && !pop.contains(e.target) && !e.target.closest('.suitebtn')) closeSuite(); }
function onKey(e) { if (e.key === 'Escape') { closeSuite(); e.stopPropagation(); } }

function tile(a) {
  const letter = (a.name || a.app).replace(/^io(?=[A-Z])/, '').slice(0, 1).toUpperCase();
  const fallback = () => h('span', { class: 'appic fb', style: `background:${/^#[0-9a-f]{3,8}$/i.test(a.color || '') ? a.color : 'var(--brand)'}` }, letter);
  const ic = a.icon ? h('img', { class: 'appic', src: a.icon, alt: '', onerror: (e) => e.currentTarget.replaceWith(fallback()) }) : fallback();
  return h('a', { class: 'apptile', href: a.url, role: 'menuitem', title: a.description || a.name || a.app, onclick: closeSuite },
    ic, h('span', { class: 'appnm' }, a.name || a.app));
}

function toggleSuite(btn) {
  if (pop) { closeSuite(); return; }
  if (!apps.length) return;
  pop = h('div', { class: 'suitepop', role: 'menu', 'aria-label': 'ioThing apps', style: `--cols:${Math.min(3, apps.length)}` }, apps.map(tile));
  document.body.append(pop);
  btn.setAttribute('aria-expanded', 'true');
  const r = btn.getBoundingClientRect();
  pop.style.top = `${Math.round(r.bottom + 6)}px`;
  pop.style.left = `${Math.round(Math.max(8, Math.min(r.left - 4, innerWidth - pop.offsetWidth - 8)))}px`;
  pop.querySelector('a')?.focus({ preventScroll: true });
  setTimeout(() => { document.addEventListener('click', outside, true); document.addEventListener('keydown', onKey, true); addEventListener('resize', closeSuite); }, 0);
}
