# How editing works

Editing in iopix is designed so that digiKam always agrees with what you did, and so that every change can be undone.

## Who can edit

Only **editors** and **admins** (see [Users and sign-in](../admin/users.md)). Visitors and viewers can browse but never change the library. **Read-only mode** (Settings, or `READ_ONLY=1`) refuses every change for everyone; `EDITING=0` removes the editing controls altogether.

## Changes show instantly

When you rate, tag or caption a photo, the screen updates at once and the writing happens in the background. If a write fails, the change is put back on screen and a message says why. The message after each change says where it was actually written.

## Where a change goes

In this order:

1. **The XMP sidecar** (`photo.jpg.xmp`), with the same fields digiKam writes, checked against digiKam 9.1: rating, titles, captions, the complete tag list in every namespace digiKam uses, face regions, dates and GPS.
2. **The image file itself**, only if you turn on **Settings › Editing › Write metadata into image files** (like digiKam's "sidecar and file" mode). JPEG, PNG, TIFF and WebP. Only the metadata block changes.
3. **digiKam's database**, in one transaction, so digiKam shows the change straight away without rereading the files.

Tags, faces, folders and some other changes live in digiKam's own tables and go straight to its database, always with the sidecar kept in step.

## The safety rails

- **Backups first.** Before iopix first rewrites a sidecar or an image file, the original is copied to the data folder. Writes stop if less than 2 GB of disk would be left.
- **The pixel guard.** Before writing metadata into an image file, iopix takes a checksum of the image data alone; after the write it checks again. A write that changed the picture is undone and reported. (Needs exiftool 12.58 or newer.)
- **Pixels change only when you ask**: rotate, flip, crop or adjust. JPEG rotation and cropping are lossless; RAW files are never rewritten.
- **One guarded transaction** for digiKam's database, with an integrity check before it is committed.
- **Full database backup** before the operations you cannot easily reverse: deleting photos, deleting a tag, merging changes back.
- **digiKam's own trash** for deleted photos.

## When digiKam is open

digiKam keeps parts of its database in memory. On the same machine, iopix checks whether digiKam is running before it writes to digiKam's tables, and holds those writes until it is closed; your change is kept in the sidecar and in iopix meanwhile, and appears under **Edits waiting for digiKam** in Maintenance. In a container, where the check is not possible, writes go ahead.

## Undo

Every change is in the [edit log](edit-log.md), with Undo.
