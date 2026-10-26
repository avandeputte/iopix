# Configuration variables

Every setting is an environment variable. `setup.sh` and the installers write the ones they ask about to `iopix.env`; in Docker they go in the `environment` block. Many can also be changed in Settings.

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

| Variable | Meaning |
| --- | --- |
| `VIEWER_DB_TYPE` | `sqlite` (default) or `mysql` |
| `VIEWER_DB_HOST`, `VIEWER_DB_PORT`, `VIEWER_DB_DATABASE`, `VIEWER_DB_USER`, `VIEWER_DB_PASSWORD` | the server, when on MariaDB/MySQL |

## Accounts and access

| Variable | Default | Meaning |
| --- | --- | --- |
| `ADMIN_USER` / `ADMIN_PASSWORD` | unset | create the admin at start when no account exists |
| `READ_ONLY` | `0` | `1` refuses every change, for everyone |
| `EDITING` | `1` | `0` removes the editing controls |
| `DB_WRITE` | `1` | `0` never writes to digiKam's database |
| `DIGIKAM_CHECK` | `pgrep` | `pgrep` checks whether digiKam runs on this machine before writing; `none` does not check |
| `AUTH_PROXY`, `AUTH_PROXY_HEADER`, `AUTH_PROXY_TRUSTED`, `AUTH_PROXY_CREATE` | off | sign-in through a proxy, see [Sign-in through a proxy](../admin/proxy-signin.md) |
| `CF_ACCESS_TEAM`, `CF_ACCESS_AUD` | unset | Cloudflare Access team domain and AUD tag |
| `MCP_ADMIN_TOKEN` | generated | the admin token for MCP admin tools |
| `VIEWER_URL` | `http://localhost:8600` | the public address, for links in MCP results |

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
| `UPDATE_CHECK` | `0` turns the daily update check off for good |
| `EXIFTOOL` | path to exiftool, if not on the path |
