# Backups and the data folder

iopix's own records (accounts, settings, favorites, albums, the edit log, AI results, its copy of the library) live in its [MariaDB or MySQL database](../connect/iopix-database.md). Everything else it writes lives in its **data folder** (`DATA_DIR`): `/data` in Docker, `/opt/iopix/data` with the installers, `data/` in a checkout. It must not be inside your photo or digiKam folders; iopix refuses to start if it is.

## What is in it

| Path | Contents | Safe to delete? |
| --- | --- | --- |
| `viewer.db.imported` | the SQLite database of a version before 1.113, kept after its one-time import; nothing reads it | yes, once you are sure the import went well |
| `index.db` | the search index, rebuilt in seconds | yes |
| `library-base.sqlite` | the snapshot Sync with digiKam compares against, rebuilt when needed | yes |
| `cache/` | thumbnails, previews and face crops | yes |
| `embeddings*.npz` | content-search vectors | yes, recomputed |
| `models/`, `geonames/` | downloaded models | yes, downloaded again |
| `secret` | signs session cookies; deleting it signs everyone out | yes |
| `sidecar-backups/` | each sidecar as it was before iopix first changed it | your call |
| `file-backups/` | each image file as it was before iopix first wrote into it | your call |
| `history/` | sidecar copies for the edit log's Restore | your call |
| `deleted-photos/` | photos deleted for good, with a record of what they were | your call |
| `db-backups/` | copies of digiKam's database before destructive changes (the last 10 of each kind are kept), and `.sql` dumps of iopix's own database taken before a version changes its shape | your call |
| `iopix.log` | iopix's own log | yes |

Sidecar and file backups are never deleted automatically. Allow disk space for them; Maintenance › Storage and backups shows how much each part takes.

## Backing iopix up

Back up the database server and the data folder with the rest of your server, or use **Maintenance › Storage and backups › Download a backup › iopix data**, which gives `viewer.sql`, a `mysqldump` of iopix's database, plus the content-search embeddings files and the restore steps.

To restore: stop iopix, load the dump into an empty database (`mariadb iopix < viewer.sql`), copy the other files into the data folder, make sure the `DB_*` settings in `config.env` point at that database, and start iopix again.

## Rolling back digiKam's database

iopix copies digiKam's database before destructive changes (deleting photos, deleting a tag, merging). To roll back, quit digiKam and copy a backup over `digikam4.db`, or restore the dump on MariaDB.
