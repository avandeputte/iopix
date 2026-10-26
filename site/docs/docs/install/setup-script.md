# setup.sh on macOS or Linux

`setup.sh` is the installer inside a checkout of the repository. It is the right choice on the Mac or PC where digiKam lives, or for development.

```sh
git clone https://github.com/avandeputte/iopix && cd iopix
./setup.sh
```

## What it asks

- Where the **photos** are.
- Whether you **use digiKam** with those photos and, if so, whether its database is the **SQLite file** or **MariaDB/MySQL** (it tests the connection). Without digiKam, iopix keeps the library itself: see [Using iopix without digiKam](../connect/no-digikam.md).
- Where iopix keeps **its own database**: a SQLite file, or MariaDB/MySQL (it creates the database if the account may).
- Which **features** you want: content search, places, objects, text recognition, photo descriptions. Each downloads its model.
- The **port**, the **admin account**, and whether to **start at login** (launchd on macOS, systemd on Linux).

Then it creates the Python environment, installs the packages, downloads the models, writes `iopix.env`, creates the admin and starts the service.

It also installs the system tools iopix needs when they are missing (Python 3.10+, exiftool, ffmpeg, jpegtran, the MariaDB client): with **Homebrew** on macOS (offering to install Homebrew first), or apt, dnf, zypper or pacman on Linux.

Run it again at any time to add a feature; it keeps what is there.

## Without questions

Flags skip the questions. `./setup.sh --help` lists them all. For example:

```sh
./setup.sh --yes --photos /srv/photos \
  --mysql db:3306/digikam:digikam:secret \
  --iopix-db db:3306/iopix:iopix:secret \
  --features search,places \
  --admin admin:secret
```

Without digiKam: `./setup.sh --yes --photos /srv/photos --no-digikam --admin admin:secret`.

## Running in the foreground

Without the service, `./run.sh` starts iopix in the terminal.

## Updating

`./update.sh`, or **Maintenance › iopix › Update now**. See [Updating](updating.md).
