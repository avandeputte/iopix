# Import, move, rename, trash

## Import

**Folders › Import**, **Maintenance › Import**, or **More** on a phone. Drop photos and videos, choose them, or take one with the camera.

- Files go into folders **by the date they were taken**, in the layout you choose: `2026/202610` like digiKam, `2026/10`, `2026/2026-10-16`, `202610` or `2026`. Admins can set the default.
- A file already in the library (same size and contents) is left out, with a link to the photo it duplicates.
- Each new file gets a sidecar with its own metadata and is added to the library at once, with its camera, place, keywords and faces.

On Android, sharing photos to the installed app opens Import with them ready. On iPhone, the Import page's picker reads the photo library.

## Files added outside iopix

The **file scanner** (Maintenance › Library › File scanner, off by default) looks in the album folders for photos added, changed or removed some other way (a file manager, a sync tool, a phone backup) and records them as digiKam's own scanner would. A disk that looks unmounted is left alone. Leave it off if everything arrives through Import.

## Move and rename

- **Move to a folder** (selection bar, info panel) moves the file with its sidecars.
- **Rename** (info panel) renames one file.
- **Rename by date** (selection bar) renames many files by the date taken.
- Folders are renamed or moved from the Folders page, with their subfolders.

Favorites, albums, edits and the edit log stay attached to the photo. Every move has Undo.

## Trash

The trash button, the ++delete++ key, or the selection bar move photos to the trash at once, with Undo in the message.

The trash is **digiKam's own**: the file and its sidecars go to the album root's `.dtrash` folder, so digiKam's Trash view lists the same items and either program can restore them. **Folders › Trash** shows everything in it.

## Deleting for good

From the Trash page, after a confirmation. Before anything is removed, the files and sidecars are copied to the data folder with a record of what they were, and digiKam's database is backed up. A deleted photo can be brought back from the edit log while that copy exists.
