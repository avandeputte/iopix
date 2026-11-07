/** Sign-in and accounts: the session (loadUser sets S.USER and the fields that come with it), the user menu, the sign-in,
 *  setup and account dialogs, and the Accounts and Suite sections of Settings. */
import { $, SETTINGS, act, api, errorBox, fill, h, icon, post } from './core.js';
import { S, applyBadgePref, applyTheme, avatarOf, brandName, hashFor, onRoute, photosWith, toast, toastError } from './state.js';
import { confirmDialog, modal } from './actions.js';
import { personPicker } from './faces.js';
import { boot, pollStatus } from './main.js';
import { loadSuite } from './suite.js';
import { popMenu } from './lightbox.js';

// ---- users: sign in / out, own password, first-admin setup, admin user management
export async function loadUser() {
  try { const me = await api('/api/auth/me'); S.USER = me.user; SETTINGS.user = me.user ? me.settings : null; S.IS_ADMIN = me.isAdmin; S.USERS_EXIST = me.usersExist; S.REQUIRE_LOGIN = !!me.requireLogin; S.VIA_PROXY = !!me.viaProxy; } catch { S.USER = null; SETTINGS.user = null; }
  renderUserMenu();
}
export function renderUserMenu() {
  loadSuite();  // the other apps' menu is per account: drawn again when the account changes
  const el = $('#usermenu'); if (!el) return;
  const btn = h('button', { class: 'iconbtn tool', title: S.USER ? `${S.USER.name} (${S.USER.role})` : S.USERS_EXIST ? 'Sign in' : 'Create the admin account',
    onclick: () => (!S.USER ? (S.USERS_EXIST ? loginDialog() : setupDialog()) : popMenu(btn, [  // the avatar menu, the same in every ioThing app
      ['Account', accountDialog],
      ['Appearance', () => { location.hash = hashFor('settings', new URLSearchParams('tab=display')); }],
      ['Settings', () => { location.hash = hashFor('settings', new URLSearchParams()); }],
      S.IS_ADMIN ? ['Maintenance', () => { location.hash = hashFor('maintenance', new URLSearchParams()); }] : null,
      'Session',
      ['Sign out everywhere else', async () => {
        if (!await confirmDialog('Sign this account out everywhere else? Every other browser and phone will have to sign in again; this one stays.', { ok: 'Sign out everywhere' })) return;
        await act(() => post('/api/auth/logout_all', {}), { toast: 'Signed out everywhere else.' });
      }],
      ['Sign out', signOut],
    ])) }, S.USER ? avatarOf(S.USER) : icon('person'));
  el.replaceChildren(btn);
}
async function afterLogin(me) {
  S.USER = me.user; SETTINGS.user = me.settings || {}; renderUserMenu();
  if (!S.BOOTED) { applyBadgePref(); applyTheme(); await boot(); return; }  // sign-in was required: load the library now
  await pollStatus(); applyBadgePref(); applyTheme(); S.viewKey = null; onRoute();
}
/** required: the viewer shows nothing else until someone signs in (no Cancel, no closing). */
export function loginDialog(required) {
  // the account picker: a tile per account; one click for a passwordless viewer, a password for the others
  const name = h('input', { class: 'einput', placeholder: 'User name', autocomplete: 'username' });
  const pw = h('input', { class: 'einput', type: 'password', placeholder: 'Password', autocomplete: 'current-password' });
  const form = h('div', { style: 'display:grid;gap:8px;margin-top:10px' }, name, pw);
  const tiles = h('div', { class: 'accounts' });
  const submit = async () => { try { const me = await post('/api/auth/login', { name: name.value.trim(), password: pw.value }); back.remove(); await afterLogin(me); toast(`Signed in as ${me.user.name}.`); } catch (e) { toastError(e); } };
  [name, pw].forEach((i) => { i.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') submit(); }; });
  const back = modal('Sign in', tiles, form,
    h('p', { class: 'muted small' }, required ? `Sign in to use ${S.APP_NAME}.` : 'Without signing in you can browse everything; signing in keeps your display settings, favorites and albums on the server, and editors can change the library.'),
    { buttons: [required ? null : 'Cancel', ['Sign in', submit, 'on']] });
  back.querySelector('.modal').prepend(h('div', { class: 'signin-brand' }, h('span', { class: 'logo' }), brandName()));  // the logo and the name above the title
  if (required) { back.onclick = null; back.classList.add('required'); }
  api('/api/auth/accounts').then((d) => {
    if (!d.accounts.length) { name.focus(); return; }
    form.hidden = true;
    fill(tiles, d.accounts.map((a) => h('button', { class: 'account', title: a.passwordless ? `Sign in as ${a.name}` : `${a.name} (password)`, onclick: async () => {
      if (a.passwordless) { try { const me = await post('/api/auth/login', { name: a.name }); back.remove(); await afterLogin(me); toast(`Signed in as ${a.name}.`); } catch (e) { toastError(e); } return; }
      name.value = a.name; form.hidden = false; tiles.querySelectorAll('.account').forEach((t) => t.classList.toggle('on', t === tiles.querySelector(`[data-n="${CSS.escape(a.name)}"]`))); pw.focus();
    }, 'data-n': a.name }, avatarOf(a, true), h('span', { class: 'nm' }, a.name), a.passwordless ? null : h('span', { class: 'muted', style: 'font-size:11px' }, 'password'))),
      h('button', { class: 'account', title: 'Type a user name', onclick: () => { form.hidden = false; name.value = ''; name.focus(); } }, h('span', { class: 'avatar big', style: 'background:var(--bg3);color:var(--fg2)' }, '…'), h('span', { class: 'nm' }, 'Other')));
  }).catch(() => name.focus());
}
export function setupDialog() {
  const name = h('input', { class: 'einput', placeholder: 'Admin user name', autocomplete: 'username' });
  const pw = h('input', { class: 'einput', type: 'password', placeholder: 'Password (8 characters or more)', autocomplete: 'new-password' });
  const submit = async () => { try { const me = await post('/api/auth/setup', { name: name.value.trim(), password: pw.value }); back.remove(); await afterLogin({ user: me.user, settings: {} }); toast('Admin account created; you are signed in.'); } catch (e) { toastError(e); } };
  [name, pw].forEach((i) => { i.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') submit(); }; });
  const back = modal('Create the admin account', h('p', { class: 'muted' }, `Right now ${S.APP_NAME} is open to anyone who can reach it. Once an admin exists, visitors browse read-only, signed-in users keep their settings on the server, and only editors and admins can change the library or the settings.`),
    h('div', { class: 'stack',}, name, pw),
    { buttons: ['Cancel', ['Create', submit, 'on']] });
  name.focus();
}
function accountDialog() {
  const cur = h('input', { class: 'einput', type: 'password', placeholder: 'Current password', autocomplete: 'current-password' });
  const pw = h('input', { class: 'einput', type: 'password', placeholder: 'New password (8 characters or more)', autocomplete: 'new-password' });
  [cur, pw].forEach((i) => { i.onkeydown = (e) => e.stopPropagation(); });
  // how this browser is signed in, and so what Sign out does
  const how = h('p', { class: 'muted small' });
  const outBtn = h('button', { class: 'pillbtn', onclick: async () => {
    back.remove(); let r; try { r = await post('/api/auth/logout', {}); } catch (e) { toastError(e); return; }
    if (r && r.cloudflareLogout) { location.href = r.cloudflareLogout; return; }  // Cloudflare's own sign-out page ends its session too
    if (S.REQUIRE_LOGIN) { location.reload(); return; } S.USER = null; SETTINGS.user = null; renderUserMenu(); await pollStatus(); applyBadgePref(); applyTheme(); S.viewKey = null; onRoute(); toast('Signed out.'); } }, 'Sign out');
  api('/api/auth/me').then((me) => {
    const by = me.signedInBy;
    fill(how, h('b', null, 'Signed in: '), by === 'cloudflare' ? (me.throughCloudflare ? 'through Cloudflare Access, from outside your network.' : 'through Cloudflare Access; this browser stays signed in on your home network too.')
      : by === 'proxy' ? 'at the sign-in page of your proxy; sign out there.' : by === 'oneclick' ? 'here, with one click (no password).' : 'here, with a password.');
    outBtn.textContent = by === 'cloudflare' && me.throughCloudflare ? 'Sign out (of Cloudflare too)' : 'Sign out';
    outBtn.title = by === 'cloudflare' ? 'Ends the session here and Cloudflare Access’s, so Cloudflare asks for sign-in again next time' : '';
    if (by === 'proxy') { outBtn.remove(); allBtn.remove(); }
  }).catch(() => {});
  const allBtn = h('button', { class: 'pillbtn', title: 'Ends this account’s sessions on every other browser and phone; this one stays signed in', onclick: async () => {
    if (!await confirmDialog('Sign this account out everywhere else? Every other browser and phone will have to sign in again; this one stays.', { ok: 'Sign out everywhere' })) return;
    await act(() => post('/api/auth/logout_all', {}), { toast: 'Signed out everywhere else.' }); } }, 'Sign out everywhere else');
  const back = modal(`${S.USER.name} · ${S.USER.role}`,
    h('p', { class: 'muted' }, S.USER.role === 'admin' ? 'You can change the library, the settings and the users.' : S.USER.role === 'editor' ? 'You can change the library (edits, tags, faces, places).' : 'You can browse, and keep favorites, albums and display settings.'),
    h('h4', null, 'Who you are in People'),
    h('div', {}, avatarOf(S.USER, true), h('span', null, S.USER.person && S.tagById.get(S.USER.person) ? S.tagById.get(S.USER.person).name : 'not linked'),
      S.IS_ADMIN ? h('button', { class: 'pillbtn', onclick: () => personPicker(async (t) => { try { S.USER = await post('/api/auth/person', { person: t.id }); renderUserMenu(); back.remove(); accountDialog(); } catch (e) { toastError(e); } }) }, S.USER.person ? 'Change' : 'Link to a person')
        : h('span', { class: 'muted' }, S.USER.person ? '' : '(a suite admin links your account in Settings › Suite › People)'),
      S.USER.person ? h('a', { class: 'lnk', href: hashFor('photos', photosWith({ tag: S.USER.person })), onclick: () => back.remove() }, 'Photos of me') : null),
    how,
    S.VIA_PROXY ? null : [h('h4', null, 'Change password'), h('div', {}, cur, pw)],
    S.VIA_PROXY ? null : h('div', {}, h('button', { class: 'pillbtn', onclick: async () => { try { await post('/api/auth/password', { current: cur.value, password: pw.value }); toast('Password changed.'); cur.value = pw.value = ''; } catch (e) { toastError(e); } } }, 'Change password')),
    { buttons: [allBtn, outBtn, 'Done'] });
}
/** Settings › Suite (suite admins): the people and their role in every app (here too), app passwords, keys and tokens,
 *  the apps and sign-in at the proxy, for the whole suite. The same panel in every io app, served by iosuite-shared. */
export function suiteSection() {
  const box = h('div');
  import(new URL('api/suite-admin/ui.js', document.baseURI).href)
    .then((ui) => ui.mount(box, { sub: S.route.params.get('part'), toast, onSub: (x) => { S.route.params.set('part', x); history.replaceState(null, '', hashFor('settings', S.route.params)); } }))
    .catch((e) => fill(box, errorBox(new Error(`The Suite panel did not load: ${e.message}`))));
  return box;
}
