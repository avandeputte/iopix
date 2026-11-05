/** Sign-in and accounts: the session (loadUser sets S.USER and the fields that come with it), the user menu, the sign-in,
 *  setup and account dialogs, and the Users and proxy sign-in sections of Settings. */
import { $, SETTINGS, act, api, errorBox, fill, h, icon, post } from './core.js';
import { S, applyBadgePref, applyTheme, avatarOf, brandName, hashFor, onRoute, photosWith, toast, toastError } from './state.js';
import { confirmDialog, modal, promptDialog } from './actions.js';
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
        : h('span', { class: 'muted' }, S.USER.person ? '' : '(an admin can link your account in Settings › Users)'),
      S.USER.person ? h('a', { class: 'lnk', href: hashFor('photos', photosWith({ tag: S.USER.person })), onclick: () => back.remove() }, 'Photos of me') : null),
    how,
    S.VIA_PROXY ? null : [h('h4', null, 'Change password'), h('div', {}, cur, pw)],
    S.VIA_PROXY ? null : h('div', {}, h('button', { class: 'pillbtn', onclick: async () => { try { await post('/api/auth/password', { current: cur.value, password: pw.value }); toast('Password changed.'); cur.value = pw.value = ''; } catch (e) { toastError(e); } } }, 'Change password')),
    { buttons: [allBtn, outBtn, 'Done'] });
}
export function usersSection() {
  const body = h('div');
  const wrap = h('div', null, h('h2', null, 'Users'),
    h('div', { class: 'muted', style: 'max-width:820px' }, 'Visitors who are not signed in browse read-only with display settings in their browser. Viewers keep settings, favorites and albums on the server; editors can also change the library; admins can also change settings, run maintenance and manage users.'),
    h('div', { class: 'srow' }, h('div', null, h('b', null, 'Require sign-in'), h('div', { class: 'muted', style: 'font-size:13px' }, 'Visitors see only the sign-in screen; nothing from the library (not even thumbnails) is served without an account. MCP clients then need the admin token as a bearer header (see the README).')),
      h('label', { style: 'display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap' }, h('input', { type: 'checkbox', id: 'reqlogin', checked: S.REQUIRE_LOGIN, disabled: !S.USERS_EXIST, onchange: async (e) => {
        try { await post('/api/settings', { require_login: e.target.checked }); S.REQUIRE_LOGIN = e.target.checked; toast(S.REQUIRE_LOGIN ? 'Visitors now have to sign in.' : 'Visitors can browse again.'); } catch (err) { e.target.checked = !e.target.checked; toastError(err); } } }), 'Require sign-in')), body);
  const draw = async () => {
    let d; try { d = await api('/api/users'); } catch (e) { fill(body, errorBox(e)); return; }
    const name = h('input', { class: 'einput', placeholder: 'User name', style: 'width:160px' }), pw = h('input', { class: 'einput', type: 'password', placeholder: 'Password', style: 'width:160px' });
    const email = h('input', { class: 'einput', type: 'email', placeholder: 'Email (optional)', autocomplete: 'off', style: 'width:200px' });
    const role = h('select', null, d.roles.map((r) => h('option', { value: r }, r)));
    const nopw = h('label', { style: 'display:flex;gap:6px;align-items:center;font-size:13px' }, h('input', { type: 'checkbox', onchange: (e) => { pw.disabled = e.target.checked; if (e.target.checked) { pw.value = ''; role.value = 'viewer'; } } }), 'no password (one-click sign-in, viewer only)');
    [name, pw, email].forEach((i) => { i.onkeydown = (e) => e.stopPropagation(); });
    const run = (fn, after = draw) => act(fn).then(() => after());
    const setPerson = (u, t) => run(async () => { await post(`/api/users/${u.id}`, { person: t ? t.id : null }); if (S.USER && S.USER.id === u.id) { S.USER.person = t ? t.id : null; renderUserMenu(); } });
    fill(body, h('table', { class: 'utable' },
      h('thead', null, h('tr', null, ['Account', 'Email', 'Person in People', 'Role', 'Password', 'Sign-in list', ''].map((c) => h('th', null, c)))),
      h('tbody', null, d.users.map((u) => h('tr', { 'data-user': u.name },
        h('td', null, h('span', { class: 'ucell' }, avatarOf(u), h('span', null, u.name, u.id === S.USER?.id ? h('span', { class: 'muted' }, ' (you)') : null))),
        h('td', null, u.email || h('span', { class: 'muted' }, 'none'), ' ',
          h('button', { type: 'button', class: 'lnk', onclick: async () => { const v = await promptDialog(`Email address for ${u.name} (empty to clear)`, u.email || ''); if (v !== null) run(async () => { await post(`/api/users/${u.id}`, { email: v.trim() }); }); } }, u.email ? 'Change' : 'Set')),
        h('td', null, u.person && S.tagById.get(u.person) ? S.tagById.get(u.person).name : h('span', { class: 'muted' }, 'not linked'), ' ',
          h('button', { type: 'button', class: 'lnk', onclick: () => personPicker((t) => setPerson(u, t)) }, u.person ? 'Change' : 'Link'), u.person ? [' · ', h('button', { type: 'button', class: 'lnk', onclick: () => setPerson(u, null) }, 'Unlink')] : null),
        h('td', null, h('select', { onchange: (e) => run(async () => { await post(`/api/users/${u.id}`, { role: e.target.value }); toast(`${u.name} is now ${e.target.value}.`); }) }, d.roles.map((r) => h('option', { value: r, selected: r === u.role }, r)))),
        h('td', null, u.passwordless ? h('span', { class: 'muted' }, 'none (one-click)') : 'set', ' ',
          h('button', { type: 'button', class: 'lnk', onclick: async () => { const p = await promptDialog(`New password for ${u.name}`, { type: 'password', placeholder: '8 characters or more', ok: 'Set password' }); if (p) run(async () => { await post(`/api/users/${u.id}`, { password: p }); toast('Password set.'); }, () => {}); } }, 'Set'),
          u.role === 'viewer' && !u.passwordless ? [' · ', h('button', { type: 'button', class: 'lnk', title: 'One-click sign-in from the account picker', onclick: async () => { if (await confirmDialog(`Remove ${u.name}’s password? Anyone who can reach ${S.APP_NAME} can then sign in as ${u.name} with one click.`)) run(() => post(`/api/users/${u.id}`, { password: '' })); } }, 'Remove')] : null),
        h('td', { style: 'text-align:center' }, h('input', { type: 'checkbox', checked: u.listed !== false, title: `Offered on the sign-in screen. Unticked, ${u.name} signs in through “Other”, by name${u.passwordless ? '' : ' and password'}.`,
          onchange: (e) => run(async () => { await post(`/api/users/${u.id}`, { listed: e.target.checked }); toast(e.target.checked ? `${u.name} is offered on the sign-in screen again.` : `${u.name} is no longer offered on the sign-in screen; “Other” and the name still sign in.`); }) })),
        h('td', { style: 'text-align:right' }, u.id !== S.USER?.id ? h('button', { class: 'pillbtn', onclick: async () => { if (await confirmDialog(`Delete the user ${u.name}?`, { ok: 'Delete', danger: true })) run(() => post(`/api/users/${u.id}`, { delete: true })); } }, 'Delete') : null))))),
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px' }, name, email, pw, role, nopw, h('button', { class: 'pillbtn on', onclick: async () => {
        await act(() => post('/api/users', { name: name.value.trim(), email: email.value.trim(), password: nopw.querySelector('input').checked ? '' : pw.value, role: nopw.querySelector('input').checked ? 'viewer' : role.value }), { toast: `Added ${name.value.trim()}.` }); draw();
      } }, icon('add'), 'Add user')));
  };
  draw();
  wrap.append(proxySection());
  return wrap;
}
// sign-in at a reverse proxy (Cloudflare Access, Authelia behind nginx...): who signed in there is signed in here
function proxySection() {
  const box = h('div');
  const draw = async () => {
    let d; try { d = await api('/api/settings/proxy'); } catch (e) { fill(box, errorBox(e)); return; }
    const v = d.values, env = new Set(d.fromEnv || []);
    const lock = (k, el) => { if (env.has(k)) { el.disabled = true; el.title = 'Set in config.env'; } el.onkeydown = (e) => e.stopPropagation(); return el; };
    const inp = (k, ph, w = 320) => lock(k, h('input', { class: 'einput', value: v[k] || '', placeholder: ph, style: `margin:0;width:${w}px;max-width:100%` }));
    const modeSel = lock('AUTH_PROXY', h('select', null, [['off', 'Off'], ['cloudflare', 'Cloudflare Access'], ['haproxy', 'HAProxy'], ['nginx', 'nginx'], ['caddy', 'Caddy'], ['traefik', 'Traefik'], ['header', 'Another proxy']]
      .map(([k, l]) => h('option', { value: k, selected: (v.AUTH_PROXY || 'off').toLowerCase() === k }, l))));
    const team = inp('CF_ACCESS_TEAM', 'myteam.cloudflareaccess.com'), aud = inp('CF_ACCESS_AUD', 'Application Audience (AUD) tag', 520);
    const header = inp('AUTH_PROXY_HEADER', 'default: ' + (d.mode === 'cloudflare' ? 'Cf-Access-Authenticated-User-Email' : 'Remote-User, X-Forwarded-User, X-Auth-Request-Email…'));
    const trusted = inp('AUTH_PROXY_TRUSTED', 'default: 127.0.0.1, ::1');
    const create = lock('AUTH_PROXY_CREATE', h('select', null, [['', 'Gets the sign-in screen (default)'], ['deny', 'Is denied access'], ['viewer', 'Gets a viewer account'], ['editor', 'Gets an editor account']]
      .map(([k, l]) => h('option', { value: k, selected: (v.AUTH_PROXY_CREATE || '') === k }, l))));
    const row = (label, detail, el) => h('div', { class: 'srow' }, h('div', null, h('b', null, label), h('div', { class: 'muted', style: 'font-size:13px' }, detail)), el);
    const cf = h('div', null,
      row('Cloudflare team domain', 'Zero Trust › Settings › Team name and domain (the address the Access sign-in page is on).', team),
      row('Application Audience (AUD) tag', 'Zero Trust › Access › Applications › this app › Overview. With both set, the token Cloudflare signs is checked and the email comes from it; nothing else is believed.', aud));
    const other = h('div', null,
      row('User header', 'The request header the proxy puts the signed-in user (name or email) in. Comma-separate several.', header),
      row('Proxy addresses', 'The header is believed only from these addresses or networks (where the proxy connects from).', trusted));
    const seen = d.seen && d.seen.iss ? h('div', { class: 'muted', style: 'font-size:13px;margin:6px 0' }, `Last request through your tunnel (its claims as sent, unverified until saved): team ${String(d.seen.iss).replace(/^https:\/\//, '')}, AUD ${[].concat(d.seen.aud || []).join(', ')}${d.seen.email ? ', ' + d.seen.email : ''}${d.seen.account !== undefined ? (d.seen.account ? ' (account ' + d.seen.account + ')' : ' (no account)') : ''}. `,
      env.has('CF_ACCESS_TEAM') || env.has('CF_ACCESS_AUD') ? null : h('button', { type: 'button', class: 'lnk', onclick: () => { team.value = String(d.seen.iss).replace(/^https:\/\//, ''); aud.value = [].concat(d.seen.aud || [])[0] || ''; } }, 'Use these')) : null;
    const show = () => { cf.style.display = modeSel.value === 'cloudflare' ? '' : 'none'; other.style.display = modeSel.value === 'off' ? 'none' : ''; rest.style.display = modeSel.value === 'off' ? 'none' : ''; };
    const rest = h('div', null, row('Someone with no account here', 'When the proxy lets in a person whose email or name matches no account. Denied: a “No access” page, with no way to sign in here.', create));
    modeSel.onchange = show;
    const state = d.mode === 'off' ? 'Off: only the sign-in here.' : d.mode === 'cloudflare' ? (d.cloudflareVerified ? 'On: Cloudflare Access tokens are verified.' : 'On, but without the team domain and AUD tag: the header is believed from the proxy addresses only.') : `On (${d.mode}): the user header is believed from ${d.trusted.join(', ')}.`;
    fill(box, h('h2', null, 'Sign-in at a proxy'),
      h('div', { class: 'muted', style: 'max-width:820px' }, 'Someone signed in at your reverse proxy (Cloudflare Access on a tunnel, or Authelia, Authentik or oauth2-proxy behind HAProxy, nginx, Caddy or Traefik) is signed in here too, matched to an account by email, then name. Everyone else still uses the sign-in here; requests from outside through Cloudflare can only use accounts with a password.'),
      h('div', { style: 'margin:8px 0' }, h('b', null, state)),
      env.size ? h('div', { class: 'muted', style: 'font-size:13px' }, `Set in config.env (change them there): ${[...env].join(', ')}`) : null,
      row('Proxy', 'Which reverse proxy signs people in.', modeSel), cf, seen, other, rest,
      h('div', { style: 'margin-top:8px' }, h('button', { class: 'pillbtn on', onclick: async () => {
        const body = {};
        [['AUTH_PROXY', modeSel], ['CF_ACCESS_TEAM', team], ['CF_ACCESS_AUD', aud], ['AUTH_PROXY_HEADER', header], ['AUTH_PROXY_TRUSTED', trusted], ['AUTH_PROXY_CREATE', create]]
          .forEach(([k, el]) => { if (!env.has(k)) body[k] = el.value.trim(); });
        try { await post('/api/settings/proxy', body); toast('Proxy sign-in saved.'); draw(); } catch (e) { toastError(e); }
      } }, 'Save')));
    show();
  };
  draw();
  return box;
}
