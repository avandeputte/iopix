# iopix's own database

iopix keeps its own data (accounts, settings, favorites, its own albums, the edit log, AI results, and its copy of the library) in one database, separate from digiKam's.

## SQLite (default)

A single file, `viewer.db`, in the data folder. Nothing to set up.

## MariaDB or MySQL

Useful if you already back up a database server, or run iopix in a container you might rebuild.

1. Create an empty database and account for iopix on the server. Use its own database (for example `iopix`), not digiKam's.
2. In **Settings › digiKam and server › iopix database**, enter the server details and press **Test connection**.
3. Tick **Copy all the data** (and **Drop and recreate** for a clean slate), then **Switch**.

Every row is copied across and iopix restarts on the new database. The connection settings are kept in `viewer-db.json` in the data folder, or come from these variables:

`VIEWER_DB_TYPE`, `VIEWER_DB_HOST`, `VIEWER_DB_PORT`, `VIEWER_DB_DATABASE`, `VIEWER_DB_USER`, `VIEWER_DB_PASSWORD`

If that server cannot be reached at start, iopix falls back to the SQLite file and says so in Settings.

## Switching back

The iopix backup download always includes a SQLite copy, whichever database is in use, so going back to SQLite is the same procedure in reverse.

## The search index

The search index (`index.db`) is separate and disposable. iopix rebuilds it from its own database and digiKam's whenever needed, in about a second for most libraries.
