# Maintenance

**Maintenance** is the admin's toolbox. It has four tabs.

![Maintenance](/img/maintenance.jpg){.shot}

## Library

| Tool | Does |
| --- | --- |
| Import | add photos and videos, filed by date |
| File scanner | pick up files added, changed or removed outside iopix |
| Lossless JPEG edits | checks that the lossless rotate and crop tool is installed |
| Trash | everything in digiKam's trash |
| Library copy | iopix's own copy of the library, and when it was last compared with digiKam |
| Rescan the library now | rebuild the search index |
| Metadata consistency | compare database, sidecar and file, and fix what disagrees |
| Metadata quality | missing locations, face problems, and more |
| Duplicate files | sets of byte-identical files, from digiKam's fingerprints, and the space the extras take |
| Date suggestions | a year for each photo marked Date unknown, from every clue the library has about it; **Use** sets the date (see [AI results](../ai/results.md#date-suggestions)) |
| Place suggestions | a position for each photo without one, from the photos around it in time; **Use** sets it |
| Jersey numbers | the player a shirt number points to where the face is not named; **Tag** adds the person |
| Clothing matches | who is probably in a photo by what they wear, within one event; **Tag** adds the person |
| Wrongly rotated | photos that read as on their side or upside down, each shown righted; **Rotate** turns the file losslessly |
| Damaged files | read every file and find JPEGs that stop early, lack their end marker or hold damaged data, damaged PNGs, and pictures that cannot be decoded; a repair where there is one: adding a missing end marker, or cropping a photo whose bottom is missing to the part above the damage (the file copied first, undo in the edit log) |
| Pick labels | how many photos carry digiKam's Rejected, Pending and Accepted labels (iopix never sets them), and **Clear…** to take the chosen ones off every photo: digiKam's database backed up first, sidecars cleaned too, undo in the edit log |
| Edits waiting for digiKam | changes in sidecars not yet in digiKam's database, with **Apply to digiKam database** |
| Tag cover photos | choose a picture for every tag automatically |

## AI analysis and models

One row per feature with its model or models: download, switch, remove, and compare the models. Below it, every background job with progress, **Check for new photos now** and **Run again from scratch**. See [AI analysis](../ai/overview.md).

## Storage and backups

The size of each part of the data folder, with what can be done about it on the same row, and the free space on the disk. The sizes are measured in the background and remembered for a few minutes, so the page opens quickly even with a large data folder. **Download a backup** offers:

- **digiKam database**: a consistent copy of `digikam4.db` as it is right now, or a fresh dump for MariaDB,
- **iopix data**: `viewer.sql`, a `mysqldump` of iopix's own database, the content-search embeddings files, and a README with the restore steps (`mariadb <database> < viewer.sql`).

## iopix

| Tool | Does |
| --- | --- |
| Version | the installed version, **Check for updates** and **Update now** |
| Edit log | every change to every photo, with Undo |
| Server log | the end of iopix's own log, filtered by level or text, and a download of the whole file |

The MCP server itself is switched on or off in **Settings › MCP server**.
