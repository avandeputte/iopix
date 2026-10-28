# What you need

## A digiKam library, or just your photos

!!! tip "No digiKam?"
    A folder of photos is enough: iopix then keeps the library itself. See [Using iopix without digiKam](../connect/no-digikam.md).

iopix is built around digiKam's own database and does not need digiKam itself to be running. It has been checked against **digiKam 9.1**.

- **Database:** SQLite (the `digikam4.db` file) or MariaDB/MySQL. Both are fully supported for reading and writing.
- **Photos:** the folders digiKam calls album roots. One or several roots work.
- **Sidecars:** if digiKam writes XMP sidecars (`photo.jpg.xmp` or `photo.xmp`), iopix reads and writes them the same way.

iopix can also keep running when digiKam's database is unreachable, from its own copy of the library. See [Sync and working without digiKam](../connect/sync.md).

## A machine to run it on

| | Minimum | Comfortable |
| --- | --- | --- |
| CPU | 2 cores | 4 cores or more (AI jobs use half by default) |
| Memory | 2 GB for browsing | 4 GB with content search and objects; 8 GB with photo descriptions |
| Disk for iopix's data | a few GB for thumbnails | plus about 1.6 GB for the default AI models, and room for backups |
| System | Linux (amd64 or arm64), macOS, or Docker | |

The AI features run on the CPU. A graphics card is not needed. On a Mac with Apple silicon, photo descriptions use the GPU automatically.

## Tools iopix uses

The installers add these for you. If you install by hand, have them on the path:

| Tool | Used for |
| --- | --- |
| Python 3.10 or newer | iopix itself |
| exiftool (12.58 or newer recommended) | reading and writing metadata |
| ffmpeg | video thumbnails and playback |
| jpegtran (libjpeg-turbo) | lossless rotate, flip and crop |
| MariaDB client (`mysqldump`) | backing up a MariaDB digiKam database before writes |

## A browser

Any current browser: Safari, Chrome, Edge or Firefox, on desktop or phone. To install iopix as an app on a phone, it must be served over HTTPS.
