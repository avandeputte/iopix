# Proxmox container

The Proxmox script creates a Debian 13 container (LXC) with iopix inside, your library mounted, and the service running. It follows the community-scripts (ProxmoxVE) layout.

## Run it

On the Proxmox host, as root:

```sh
bash -c "$(curl -fsSL https://raw.githubusercontent.com/avandeputte/iopix/main/proxmox/ct/iopix.sh)"
```

## What it asks

First about iopix:

1. **Photo folder** on the host.
2. **digiKam's database**: the folder with `digikam4.db`, a MariaDB host, port, database, user and password, or **I don't use digiKam** (see [Using iopix without digiKam](../connect/no-digikam.md)).
3. **iopix's own database**: a SQLite file in the container, or MariaDB (the digiKam answers are offered as defaults; use a separate database name).
4. **Writing**: whether iopix may write into the library. Yes mounts the folders read/write and creates a privileged container, because an unprivileged one cannot write files owned by a host user.
5. **Features**: which AI features to install (each downloads its model).
6. **Admin account**.

Then the usual framework questions: CPU, memory, disk and network. The defaults are 4 CPU, 4 GB memory and a 16 GB disk.

The library is mounted at `/library/photos` and `/library/digikamDB` (only the photos, without digiKam) inside the container, and iopix listens on port 8600.

## Unattended install

Set these in the environment to skip the questions:

| Variable | Example |
| --- | --- |
| `IOPIX_PHOTOS` | `/tank/photos` |
| `IOPIX_DB` | `sqlite:/tank/digikamDB`, `mysql:host:3306/digikam:digikam:password` or `none` |
| `IOPIX_VIEWER_DB` | `sqlite` or `host:3306/iopix:iopix:password` |
| `IOPIX_WRITE` | `1` or `0` |
| `IOPIX_FEATURES` | `search,places,objects` |
| `IOPIX_ADMIN` | `admin:password` |

Passwords reach the setup inside the container through a root-only file that is removed afterwards, never on a command line.

## If something fails

The setup log is kept in `/opt/iopix/setup.log` inside the container. On failure the script shows its last lines and prints the command to run it again.

## Updating

Run `update` inside the container, or use **Maintenance › iopix › Update now** in the app. Settings and data are kept.

Because the container cannot see digiKam's process, it runs with `DIGIKAM_CHECK=none`.
