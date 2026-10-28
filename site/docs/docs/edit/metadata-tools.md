# Metadata tools

## All metadata

Viewer › info panel › File › **All metadata** shows every EXIF, GPS, IPTC and XMP field of the file and of its sidecar, grouped and searchable. Editors can change or add values:

- XMP fields go to the sidecar (and into the file when writing into image files is on),
- EXIF, GPS and IPTC fields live only in the file, so they need that setting.

**Reread from file** does what digiKam's "Read metadata from file" does.

## Metadata consistency

**Maintenance › Library › Metadata consistency** compares rating, title, caption and tags across digiKam's database, the sidecar and the image file, and lists the photos where they disagree.

- **Check new and changed photos**, **Re-check the whole library**, or **Check the current search only**.
- The summary tiles filter the list: photos that disagree, those that differ in one field, those without a sidecar.
- To fix: select photos, pick which copy is right, the copies to update and the fields, then **Sync**.
- Photos without a sidecar can get one written from digiKam's database.

## Metadata quality

**Maintenance › Library › Metadata quality** runs a set of checks, each with its own report:

- photos and videos with **no location**, to place in batches,
- the **face checks** described in [Naming faces](faces.md),

Each report lets you fix the photo, or accept it as it is so it leaves the list.

## Folder properties

Folders › a folder's info button: **caption, date, category and cover photo**, stored in digiKam's album record so digiKam shows the same. **Use as folder cover** in the viewer sets the cover from the photo on screen.
