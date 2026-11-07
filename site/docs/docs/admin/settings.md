# Settings

**Settings** is for admins, apart from the display choices every signed-in account can make for itself. It has six tabs.

## Display

| Setting | Does |
| --- | --- |
| Name | what iopix calls itself: the top of the page, the browser tab, the installed app. Left empty (or set to iopix in any capitalisation) it shows the wordmark **ioPix**, with a capital P like the other ioThing apps |
| Appearance | light (default), dark, or follow the device |
| Units | metric or imperial for distances and altitude |
| Thumbnail badges | faces, place and favorite badges on thumbnails |
| Info panel | which sections the viewer's info panel shows |
| Map provider | OpenStreetMap, or Google Maps with your key |

Display choices are saved with your account when signed in, else in the browser.

## digiKam and server

| Setting | Does |
| --- | --- |
| digiKam database | SQLite folder or MariaDB/MySQL server, with **Test connection** |
| Album roots | where each of digiKam's photo folders is found on this machine |
| iopix database | the MariaDB/MySQL server holding iopix's own data, with **Test connection** and **Use this database**, which copies everything across and restarts iopix; the same panel is the set-up page shown when no database is configured (see [iopix's own database](../connect/iopix-database.md)) |
| Sync with digiKam | on, off, and the merge choices when turning it back on |
| Check for updates daily | a note for admins when a new version is out |
| Read-only mode | refuse every change, for everyone |

## Editing

| Setting | Does |
| --- | --- |
| Write metadata into image files | also write changes into JPEG, PNG, TIFF and WebP files, not only sidecars |
| Sidecar file names | `photo.jpg.xmp` (digiKam's default) or `photo.xmp` |
| Metadata templates | creator, copyright and contact templates |
| Place tags | create `Places/Country/Region/Town` tags from GPS positions |

## AI and search

The content-search model, background threads (how many cores a job may use), and the switches for each analysis job.

## Suite

For suite admins: the suite's admin panel, the same in every io app. People and their role in every app, app passwords, keys and tokens, the apps (default role, sign-in required) and sign-in at the proxy. See [Users and sign-in](users.md).
