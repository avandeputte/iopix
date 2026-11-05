# Linux installer

`iopix-install.sh` is one self-extracting file of about 200 KB (the app without models). It is attached to every [release](https://github.com/avandeputte/iopix/releases/latest).

## Install

As root on the target machine:

```sh
sh iopix-install.sh --photos /srv/photos --digikam-sqlite /srv/digikamDB --db local
```

If you don't use digiKam, give `--no-digikam` instead of `--digikam-sqlite` (see [Using iopix without digiKam](../connect/no-digikam.md)).

iopix keeps its own data in a MariaDB or MySQL database. `--db local` installs MariaDB on the machine and creates the database and account; `--db HOST:PORT/DB:USER:PASSWORD` uses a server you already run (the one digiKam uses will do, with a separate database). Without the option the installer asks. See [iopix's own database](../connect/iopix-database.md).

It then:

1. unpacks the app into `/opt/iopix/app`,
2. installs the system packages iopix needs (Python with venv, ffmpeg, exiftool, libheif, the MariaDB client, and the MariaDB server with `--db local`) with apt, dnf, zypper or pacman,
3. creates the Python environment and installs the packages,
4. downloads the models for the features you choose,
5. creates the admin account,
6. writes the settings to `/opt/iopix/app/config.env`, with the data in `/opt/iopix/data`,
7. installs and starts a systemd service called `iopix`, running as the `iopix` user on port 8600.

A sudoers rule lets the service user run exactly `systemctl restart iopix`, so **Update now** in the app works.

## Options

| Option | Meaning |
| --- | --- |
| `--photos PATH` | the photo folder |
| `--digikam-sqlite PATH` | the folder with `digikam4.db` |
| `--digikam-db HOST:PORT/DB:USER:PASSWORD` | digiKam on MariaDB/MySQL instead |
| `--no-digikam` | no digiKam: iopix keeps the library itself |
| `--db local` or `HOST:PORT/DB:USER:PASSWORD` | iopix's own database: MariaDB installed and created on this machine, or a server you name |
| `--features search,places,objects,ocr,captions,speech` | the AI features; `none` for browsing only |
| `--captions` | all features, including photo descriptions |
| `--admin NAME:PASS` | the admin account |
| `--prefix`, `--data` | where the app and its data go |
| `--port`, `--host` | where it listens (default 8600, all addresses) |
| `--user` | the service user |
| `--read-only` | browsing only |
| `--no-service` | do not install the systemd service |
| `--no-system-deps` | do not install system packages |
| `--yes` | take every default |
| `--help` | list everything |

To keep passwords off the command line, set `IOPIX_ADMIN=NAME:PASS` and `IOPIX_DIGIKAM_DB=HOST:PORT/DB:USER:PASSWORD` in the environment instead.

## Upgrading

Running the installer again upgrades the code and keeps the data folder and the settings file. So does `./update.sh` in `/opt/iopix/app`, or **Update now** in the app.

!!! note "Upgrading from a version before 1.113"
    An install that still has `viewer.db` in its data folder needs a MariaDB or MySQL database from 1.113 on. Run the installer again with `--db local` (or a server), then open iopix: its set-up page offers to import the old file. See [iopix's own database](../connect/iopix-database.md).
