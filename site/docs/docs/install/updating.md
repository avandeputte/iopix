# Updating

## From the app

**Maintenance › iopix** shows the installed version. **Check for updates** compares it with the newest version, and **Update now** installs it and restarts iopix. The page reloads by itself when the new version is up.

With **Check for updates daily** on (the default), iopix asks once a day whether a newer version exists and shows admins a note with a link. Only the public list of versions is fetched; nothing about your install is sent. `UPDATE_CHECK=0` turns this off for good.

## From the command line

| Install | Command |
| --- | --- |
| Docker | `docker compose pull && docker compose up -d` |
| Proxmox container | `update` inside the container |
| Linux installer | `/opt/iopix/app/update.sh` |
| setup.sh checkout | `./update.sh` in the checkout |

`update.sh` fetches the new version, installs any new packages **before** switching over (so a failed install leaves the running version alone), keeps `config.env`, the data folder and the Python environment, and restarts the service.

| Option | Does |
| --- | --- |
| `--check` | only reports whether a newer version exists |
| `--force` | reinstalls even when nothing is newer |
| `IOPIX_REF=<commit>` | installs that exact version |

## Database changes

When a new version needs a different shape for iopix's own database, it converts it on the first start, after writing a dump to `db-backups/` in the data folder. digiKam's database is never changed by an update.

!!! note "Upgrading from a version before 1.113"
    From 1.113 iopix's own data lives on a MariaDB or MySQL server instead of the `viewer.db` file. After the update iopix shows a set-up page asking for a database and offers to import the file. See [iopix's own database](../connect/iopix-database.md).
