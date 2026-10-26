# FAQ and troubleshooting

## General

**Does iopix replace digiKam?**
It works alongside it. iopix covers browsing, searching and the everyday editing (ratings, captions, tags, faces, dates, places, files) from any device. Everything it writes is in digiKam's format, so you can keep using both.

**Does it copy or move my photos?**
No. It reads them where they are. It only writes when someone with editor rights changes something, and then as digiKam would.

**Does it need the internet?**
No. Model downloads, the daily update check, OpenStreetMap place search and Google Maps (if chosen) are the only outside requests, and each can be turned off.

**Can I try it without any risk?**
Yes: mount the library read-only (Docker), or set `READ_ONLY=1`. Every change is then refused.

## Setup

**iopix starts but shows no photos.**
Check the album roots: digiKam recorded the folders with the paths on its own machine. Map each to where iopix sees it under **Settings › digiKam and server › Album roots**.

**Thumbnails are missing for some photos.**
The files are not where iopix looks, or not readable by its user (uid 1000 in Docker). Check the photo mount and its permissions.

**"Add to Home Screen" does not give a full-screen app.**
The browser only allows that over HTTPS. Put iopix behind a [reverse proxy](../install/reverse-proxy.md).

**In Docker, iopix cannot write its data folder.**
An old volume is owned by root. Run `docker run --rm -v iopix-data:/data alpine chown -R 1000:1000 /data` once.

**Dates on MariaDB look a few hours off.**
Set `DIGIKAM_MYSQL_DATES` to `local` or `utc`; see [digiKam on MariaDB](../connect/mariadb.md).

## Editing

**I changed a photo in iopix but digiKam does not show it.**
If digiKam was open, the change waits in the sidecar and in iopix. Close digiKam, or use **Maintenance › Library › Edits waiting for digiKam › Apply to digiKam database**.

**A face shows up twice for the same person.**
That is usually a leftover from older digiKam versions. **Maintenance › Library › Metadata quality** finds and removes these duplicates.

**How do I undo something?**
Every change is in the [edit log](../edit/edit-log.md), with Undo. The message after each change also has an Undo button.

## AI

**Content search finds nothing.**
The model may still be indexing; **Maintenance › AI analysis and models** shows progress. Search switches on when the index is complete.

**The server is busy for hours after install.**
The first pass over a large library takes time. Jobs run one at a time on half the cores by default; lower it in **Settings › AI and search**.
