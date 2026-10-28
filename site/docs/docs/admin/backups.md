# Backups and the data folder

Everything iopix writes lives in its **data folder** (`DATA_DIR`): `/data` in Docker, `/opt/iopix/data` with the installers, `data/` in a checkout. It must not be inside your photo or digiKam folders; iopix refuses to start if it is.

## What is in it

| Path | Contents | Safe to delete? |
| --- | --- | --- |
| `viewer.db` | iopix's own database: accounts, settings, favorites, albums, edit log, AI results, its copy of the library (absent when on MariaDB) | **no** |
| `viewer-db.json` | where iopix's database is, when on MariaDB/MySQL | no |
| `index.db` | the search index, rebuilt in seconds | yes |
| `cache/` | thumbnails, previews and face crops | yes |
| `embeddings*.npz` | content-search vectors | yes, recomputed |
| `models/`, `geonames/` | downloaded models | yes, downloaded again |
| `secret` | signs session cookies; deleting it signs everyone out | yes |
| `sidecar-backups/` | each sidecar as it was before iopix first changed it | your call |
| `file-backups/` | each image file as it was before iopix first wrote into it | your call |
| `history/` | sidecar copies for the edit log's Restore | your call |
| `deleted-photos/` | photos deleted for good, with a record of what they were | your call |
| `db-backups/` | copies of digiKam's database before destructive changes; the last 10 of each kind are kept | your call |
| `viewer.log` | iopix's own log | yes |

Sidecar and file backups are never deleted automatically. Allow disk space for them; Maintenance › Storage and backups shows how much each part takes.

## Backing iopix up

Back up the data folder with the rest of your server, or use **Maintenance › Storage and backups › Download a backup › iopix data**, which gives iopix's database as SQLite plus the content-search index and the restore steps.

To restore: stop iopix, copy the files into the data folder, start it again.

## Rolling back digiKam's database

iopix copies digiKam's database before destructive changes (deleting photos, deleting a tag, merging). To roll back, quit digiKam and copy a backup over `digikam4.db`, or restore the dump on MariaDB.
