# Using iopix without digiKam

You don't need digiKam to use iopix. Point it at a folder of photos and it keeps the library itself, in its own database.

## How to set it up

Every install offers it:

| Install | How |
| --- | --- |
| [Proxmox container](../install/proxmox.md) | answer **I don't use digiKam** when it asks where digiKam's database is (unattended: `IOPIX_DB=none`) |
| [Linux installer](../install/linux.md) | `sh iopix-install.sh --photos /srv/photos --no-digikam` |
| [setup.sh](../install/setup-script.md) | answer **no** to "Do you use digiKam with these photos?" (or `--no-digikam`) |
| [Docker](../install/docker.md) | `DIGIKAM_DB_TYPE: none`, without the other `DIGIKAM_DB_*` lines |

An install that already runs can switch too: **Settings › digiKam and server › digiKam database › None**.

To edit photos (tags, faces, captions and so on), iopix needs to be allowed to write into the photo folder, the same as with digiKam.

## What happens on the first start

1. iopix starts an empty library on your photo folder.
2. Its **file scanner** looks through the folder and adds every photo and video it finds, with what the files already hold: the date taken, the camera, the GPS position, captions, keywords and faces marked by other programs.
3. From then on the scanner checks the folder every few minutes, and once right after each start, so photos you copy in show up by themselves. **Maintenance › Library › File scanner** sets how often, or scans now.

Photos added with **Import** or by uploading into a folder are added straight away.

## What works

Everything: browsing and search, People and faces (including finding faces automatically), tags, ratings, titles and captions, dates and places, folders, Import, the trash, the edit log with undo, and the AI features.

Each change is saved in iopix's library and in an XMP sidecar file next to the photo, so other photo programs can read it too.

## What is different

- There is no **Sync with digiKam** setting, and nothing is ever "waiting for digiKam".
- **Backups:** the iopix data backup (**Maintenance › Storage and backups**) holds the whole library: folders, tags, people and faces, captions and places. Keep a copy somewhere else.
- If you keep iopix's own database on MariaDB, the library lives there too. See [iopix's own database](iopix-database.md).

## Starting to use digiKam later

The sidecar files next to your photos hold the tags, people, faces, captions and ratings, in the format digiKam reads, so digiKam picks them up when it first scans the folder.

Once digiKam has scanned the folder, choose its database under **Settings › digiKam and server › digiKam database**. iopix never silently replaces what you made: it keeps using its own library until you pick **Replace with digiKam's** under **Sync with digiKam** (see [Sync and working without digiKam](sync.md#turning-sync-back-on)). Since digiKam has read the sidecars by then, it already holds your tags, people, faces, captions and ratings. **Merge into digiKam** does not work for a library started in iopix (there is no earlier digiKam record to merge against), so it says so instead of guessing.
