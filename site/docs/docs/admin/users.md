# Users and sign-in

## Open until you add an admin

Out of the box iopix is open to anyone who can reach it. Once the first account exists, visitors can still browse read-only, and every change needs an editor or admin.

Create the admin account during install, from **Settings › Create the admin account**, with `ADMIN_USER` and `ADMIN_PASSWORD` in a container, or from the command line:

```sh
.venv/bin/python -m app.users create <name> <password> admin
```

## Roles

| Who | Can |
| --- | --- |
| Not signed in | browse and search everything, read-only; display settings stay in the browser |
| **Viewer** | the same, plus settings, favorites and private albums kept with the account, so they follow you to other devices |
| **Viewer without a password** | the same, and signs in with one tap from the account picker, like a profile on a media server. Can never be more than a viewer |
| **Editor** | plus every change to the library: edits, tags, faces, places, imports, moves, the trash |
| **Admin** | plus settings, maintenance, models, backups, updates and managing accounts |

The server enforces all of this; hiding a button is never the only protection.

## Require sign-in

**Settings › Users and sign-in › Require sign-in** turns open browsing off. Visitors get the sign-in screen and nothing else: no thumbnail, photo or search result is served without an account. Turn it on for anything reachable from the internet.

## Managing accounts

**Settings › Users and sign-in** lists the accounts. For each:

- **role** and **password** (or none, for a one-tap viewer),
- **email address**, used to match sign-ins from a proxy such as Cloudflare Access,
- **person**: link the account to someone in People, so their face is the account's picture,
- **sign-in list**: take an account off the tiles on the sign-in screen (it then signs in through **Other**, by name and password).

## Signing in

The person icon opens the sign-in dialog, with a tile per account. A passwordless viewer signs in with one tap; the others ask for their password.

Sessions last 30 days and renew as you use them. Passwords are stored hashed, never in clear.

## From outside the house

From outside your network, only accounts with a password can sign in, and one-tap profiles are refused. To use a single sign-in for iopix and other services, see [Sign-in through a proxy](proxy-signin.md).
