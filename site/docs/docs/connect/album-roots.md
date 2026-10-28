# Album roots and folders

digiKam records the full path of each album root, for example `/Users/you/Pictures/Photos` on the Mac where it runs. When iopix runs somewhere else (a NAS, a container), the same folder is usually at a different path, such as `/library/photos`.

## One root

With a single album root, set the photo folder (the installers ask for it; in Docker it is `PHOTOS_ROOT=/library/photos`) and iopix maps digiKam's root to it.

## Several roots

**Settings › digiKam and server › Album roots** lists every root digiKam recorded, with a field for where it is mounted here. A saved mapping overrides `PHOTOS_ROOT`. iopix rebuilds its index after a change.

## Folders and digiKam's albums

What digiKam calls an album is a folder on disk, and iopix shows them under **Folders**. The albums you create in iopix are something else: groupings that live only in iopix and never touch the disk. See [Folders, albums and tags](../use/collections.md).

## The data folder must be separate

iopix refuses to start if its data folder is inside a photo folder or digiKam's database folder, so its thumbnails and backups can never end up in your library.
