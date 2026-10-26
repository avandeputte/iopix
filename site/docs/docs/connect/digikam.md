# Connecting to digiKam

iopix needs two things from digiKam: the **database** and the **photo folders**.

## The database

=== "SQLite (digiKam's default)"

    digiKam keeps its database in a folder you chose in its first-run wizard, as `digikam4.db` (with `recognition.db`, `similarity.db` and `thumbnails-digikam.db` beside it). Give iopix that folder:

    - `setup.sh` and the installers ask for it,
    - in Docker, mount it at `/library/digikamDB`,
    - or set `DIGIKAM_DB_DIR`.

    In digiKam you can see the folder under **Settings › Configure digiKam › Database**.

=== "MariaDB / MySQL"

    Give iopix the same server, database, user and password digiKam uses. See [digiKam on MariaDB or MySQL](mariadb.md).

You can change these later in **Settings › digiKam and server**, with a **Test connection** button.

## The photo folders

digiKam calls each top-level photo folder an **album root**. iopix reads the paths digiKam recorded. If iopix runs on another machine or in a container, the same folder is probably mounted somewhere else; map each root to where iopix can see it. See [Album roots and folders](album-roots.md).

## Read-only to start

iopix opens digiKam's database **read-only** unless an editor makes a change that belongs there. Photo files are only opened for reading unless you turn on writing metadata into image files.

To be sure nothing is ever written, start with `READ_ONLY=1` or the **Read-only mode** switch in Settings. Every write is then refused, for everyone.

## Is digiKam running?

digiKam caches some of its database in memory, so iopix prefers to write to digiKam's tables while digiKam is closed. On the same machine iopix checks for the digiKam process before such a write. In a container, or with digiKam on another computer, it cannot see the process; there `DIGIKAM_CHECK=none` is set, and you should simply avoid editing the same photo in both programs at the same moment.

## Sidecar file names

digiKam writes a photo's sidecar as `photo.jpg.xmp`, or as `photo.xmp` with its "compatible file name" option. iopix reads either. New sidecars are written the default way unless you choose **Use photo.xmp** in **Settings › Editing**, or set `SIDECAR_COMPAT=1`.
