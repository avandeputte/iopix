# How iopix works

iopix is a small web server with a web app on top. It sits next to your digiKam library and does three things.

## It reads your library as it is

iopix reads digiKam's database for the photo list, albums, tags, people and face regions, ratings, captions, dates and places, and reads the photo files themselves to make thumbnails and previews. There is no import step and no second copy of your photos.

When digiKam changes something, iopix notices within about 20 seconds and updates its search index.

## It keeps its own data separately

Everything iopix builds lives in its own **data folder**, never inside your photo or digiKam folders:

- thumbnails and previews,
- the search index,
- its own database: accounts, settings, favorites, its own albums, the edit log, and the results of AI analysis,
- backups of anything it changed.

Its own database is a SQLite file by default, or a MariaDB/MySQL database if you prefer. See [iopix's own database](../connect/iopix-database.md) and [Backups and the data folder](../admin/backups.md).

## It writes back only when you ask

When an editor changes a photo, iopix writes the change the way digiKam would:

1. to the photo's **XMP sidecar**,
2. optionally into the **image file** itself,
3. and into **digiKam's database**, in one guarded transaction.

Every change is recorded in the [edit log](../edit/edit-log.md) with Undo. Details are in [How editing works](../edit/how-editing-works.md).

## What runs where

```
 browser / phone / TV app
          │  HTTPS (through your reverse proxy)
          ▼
 ┌────────────────────┐      reads, and writes on request
 │      iopix         │ ───────────────────────────────▶  digiKam's database (SQLite or MariaDB)
 │  port 8600         │ ───────────────────────────────▶  photo folders and sidecars
 │                    │
 │  data folder:      │
 │  thumbnails, index,│
 │  own database,     │
 │  models, backups   │
 └────────────────────┘
```

## Privacy

iopix does not phone home. The only outside requests it makes are ones you turn on: downloading AI models, the daily check for a new version, place search on OpenStreetMap, and Google Maps if you choose it as the map provider. The AI analysis runs on your machine.
