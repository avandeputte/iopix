# Rotate, crop and adjust

These are the only edits that change pixels, and only when you ask.

## Rotate and flip

The rotate button in the viewer, ++bracket-left++ and ++bracket-right++, or **Rotate** in the selection bar for many photos.

- **JPEG**: turned without loss, in the compressed data, as digiKam's lossless rotation does. Nothing is decoded or re-encoded.
- **Other formats** (PNG, TIFF, WebP, HEIC, RAW): the orientation flag is changed instead, in digiKam's database, the sidecar and, where possible, the file.

Face boxes turn with the picture.

## Crop

Viewer › rotate button › **Crop…**: drag a rectangle, adjust its corners, and Crop.

- **JPEG**: cropped without loss. The edges snap to the 8 or 16 pixel blocks JPEG is made of, so the result can be a few pixels larger than drawn.
- **PNG and TIFF**: without loss. **WebP**: re-saved at high quality. **HEIC**: re-encoded at high quality, and the message says so.
- **RAW files and videos** are never rewritten.

Face boxes move with the crop; a face cropped out is removed, but the person's tag stays.

## Adjust as a new version

Viewer › rotate button › **Adjust**: exposure, contrast, saturation, warmth, straighten and auto levels, previewed on screen. Saving renders a **new file** beside the original (`name_v1.jpg`), with the original's metadata, and links the two the way digiKam links versions. The original file is never changed.

## Undo

Before each rotate or crop the file is backed up (the true original once, plus a copy from just before this edit). The message and the edit log offer **Undo**, which puts the file and its records back exactly.
