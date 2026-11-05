# Configuration variables

Every setting is an environment variable. `setup.sh` and the installers write the ones they ask about to `config.env`; in Docker they go in the `environment` block. Many can also be changed in Settings.

## Library and data

| Variable | Default | Meaning |
| --- | --- | --- |
| `DIGIKAM_DB_DIR` | `~/Pictures/digikamDB` | folder containing `digikam4.db` (SQLite) |
| `PHOTOS_ROOT` | `~/Pictures/photos` | photo folder, when the library has one album root |
| `DATA_DIR` | `./data` | everything iopix writes; must be outside the two folders above |
| `SIDECAR_COMPAT` | `0` | `1` writes new sidecars as `photo.xmp` |

## digiKam on MariaDB / MySQL

| Variable | Default | Meaning |
| --- | --- | --- |
| `DIGIKAM_DB_TYPE` | `sqlite` | `sqlite` or `mysql`, or `none` when you don't use digiKam |
| `DIGIKAM_DB_HOST` / `DIGIKAM_DB_PORT` | `127.0.0.1` / `3306` | server |
| `DIGIKAM_DB_NAME` / `DIGIKAM_DB_USER` / `DIGIKAM_DB_PASSWORD` | `digikam` / `digikam` / empty | database and account |
| `DIGIKAM_DB_BACKUP` | `auto` | `auto`: back up before destructive operations; `skip`: never |
| `DIGIKAM_DB_DUMP` | unset | a command that writes a full dump to standard output |
| `DIGIKAM_MYSQL_DATES` | measured | `local` or `utc` |

## iopix's own database

iopix's own data lives on a MariaDB or MySQL server (see [iopix's own database](../connect/iopix-database.md)). These settings name it (the set-up page and Settings › iopix database write them into `config.env`); without them iopix shows a set-up page asking for the details.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DB_TYPE` | | `mysql` for MariaDB or MySQL |
| `DB_HOST` / `DB_PORT` | / `3306` | server |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | | database and account; its own database, not digiKam's |

## Accounts and access

| Variable | Default | Meaning |
| --- | --- | --- |
| `ADMIN_USER` / `ADMIN_PASSWORD` | unset | create the admin at start when no account exists |
| `PUBLIC_URL` | unset | the address people reach iopix at (e.g. `https://photo.example.com`), shown on the ioThing suite's start page and in the other apps; unset, the best address iopix has been reached at is used |
| `HOST` / `PORT` | `0.0.0.0` / `8600` | where the server listens (every app of the suite has its own port) |
| `FORWARDED_ALLOW_IPS` | `*` | the addresses allowed to pass on the visitor's address (your reverse proxy's) |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | unset | HTTPS served directly instead of behind a reverse proxy |
| `LOG_FILE` | `DATA_DIR/iopix.log` | the server's rotating log (empty: none) |
| `READ_ONLY` | `0` | `1` refuses every change, for everyone |
| `EDITING` | `1` | `0` removes the editing controls |
| `DB_WRITE` | `1` | `0` never writes to digiKam's database |
| `DIGIKAM_CHECK` | `pgrep` | `pgrep` checks whether digiKam runs on this machine before writing; `none` does not check |
| `AUTH_PROXY`, `AUTH_PROXY_HEADER`, `AUTH_PROXY_TRUSTED`, `AUTH_PROXY_CREATE` | off | sign-in through a proxy, see [Sign-in through a proxy](../admin/proxy-signin.md) |
| `CF_ACCESS_TEAM`, `CF_ACCESS_AUD` | unset | Cloudflare Access team domain and AUD tag |
| `MCP_USER` | unset | the account `python -m app.mcp` (stdio) acts as; over HTTP every `/mcp` request signs in with an app password |

## AI and background work

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUTO_MODELS` | empty | models to download at start, such as `siglip2,yolox,geonames` |
| `ANALYSIS` | `1` | `0` turns background analysis off |
| `SEMANTIC` | `1` | `0` turns content search off |
| `CAPTIONS` | `1` | `0` turns photo descriptions off |
| `BG_THREADS` | half the cores | cores one background job may use |
| `PREWARM` | `1` | build all grid thumbnails after start |
| `REINDEX_POLL` | `20` | seconds between checks for changes in digiKam's database |
| `HF_HUB_OFFLINE` | unset | `1` never contacts the model hub |

## Thumbnails and previews

| Variable | Default | Meaning |
| --- | --- | --- |
| `THUMB_HEIGHT` / `THUMB_MAX_WIDTH` / `THUMB_QUALITY` | `360` / `1080` / `76` | grid thumbnails |
| `PREVIEW_SIZE` / `PREVIEW_QUALITY` | `2560` / `86` | viewer previews |
| `PREVIEW_CACHE_MAX_MB` | `8000` | preview cache size cap |

## Maps and outside services

| Variable | Meaning |
| --- | --- |
| `GOOGLE_MAPS_API_KEY` | Google Maps key (Settings can store one instead) |
| `GOOGLE_SERVER_KEY` | a separate key for server-side Google calls |
| `IOCONTACTS_URL`, `IOCONTACTS_USER`, `IOCONTACTS_PASSWORD` | ioContacts' address, and the account and app password iopix reads the birthdays as (see [ioContacts](../integrations/iocontacts.md)) |
| `IOCONTACTS_CF_CLIENT_ID`, `IOCONTACTS_CF_CLIENT_SECRET` | the Cloudflare Access service token for ioContacts |
| `UPDATE_CHECK` | `0` turns the daily update check off for good |
| `EXIFTOOL` | path to exiftool, if not on the path |
