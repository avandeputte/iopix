# Sync and working without digiKam

iopix keeps a copy of the library's records (photos, folders, tags, faces, places) in its own database, in exactly the shape digiKam uses. That copy lets it keep working when digiKam's database is not there. If you don't use digiKam at all, see [Using iopix without digiKam](no-digikam.md).

## Sync with digiKam on (the default)

The copy follows digiKam's database. After any change, made in iopix or in digiKam, the two are compared and only the rows that differ are updated.

If digiKam's database becomes unreachable (the server is down, the disk is unmounted), iopix keeps running from its copy and tells admins when the copy was taken.

Edits waiting in sidecars are written into digiKam's database automatically when digiKam is closed: a minute after the last edit, and at most every five minutes. **Maintenance › Apply to digiKam database** does the same on request.

## Sync with digiKam off

**Settings › digiKam and server › Sync with digiKam** can be turned off. iopix then never opens digiKam's database and everything works on its own copy: edits, tags, folders and uploads, faces, place tags. Ratings, titles, captions, dates, places and tags are still written to the sidecars, where digiKam will find them.

## Turning sync back on

iopix never overwrites what changed while sync was off. If anything did, it keeps using its own copy and offers three choices:

| Choice | What happens |
| --- | --- |
| **Merge into digiKam** | a three-way merge against what digiKam held when sync was turned off: new tags, folders and uploads get ids in digiKam; renames, moves, tags, faces, ratings, dates and places are applied; deleted photos are removed. Where digiKam changed the same thing meanwhile, iopix's value wins and the conflict is counted. digiKam's database is backed up first. |
| **Replace with digiKam's** | copy the library from digiKam again and drop the changes made here (sidecar edits are kept) |
| **Turn sync off again** | keep working on iopix's copy |

## Photo ids

digiKam gives every photo a number. If digiKam ever renumbers photos, for example after you rebuild its database, iopix recognises each file by its fingerprint and size, and favorites, albums, edits, AI results and the edit log follow the photo to its new number.
