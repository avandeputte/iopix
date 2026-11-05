# iopix's own database

iopix keeps its own data (accounts, settings, favorites, its own albums, the edit log, AI results, and its copy of the library) in one database, separate from digiKam's. Since version 1.113 that database is a **MariaDB or MySQL** database; it is no longer a file in the data folder. digiKam's database is not affected: it may still be the `digikam4.db` file or a server, as before.

Both databases may live on the same server, but give iopix its own database, not digiKam's.

!!! note "Upgrading from a version before 1.113"
    Older versions kept everything in a SQLite file, `viewer.db`, in the data folder. After the update, iopix starts on a set-up page asking for a MariaDB or MySQL database. Enter the details and leave **Import the existing data** ticked: every row of `viewer.db` is copied across, and the file is renamed `viewer.db.imported`. Nothing reads it afterwards; keep or delete it as you like.

## Creating the database

On your MariaDB or MySQL server:

```sql
CREATE DATABASE iopix CHARACTER SET utf8mb4;
CREATE USER 'iopix'@'%' IDENTIFIED BY 'choose-a-password';
GRANT ALL ON iopix.* TO 'iopix'@'%';
```

The account needs full rights on that one database: iopix creates and changes its own tables there.

The installers can do this for you:

| Install | How |
| --- | --- |
| [Docker](../install/docker.md) | the compose file ships a `mariadb` service (`iopix-db`) with the `DB_*` lines already pointing at it |
| [Linux installer](../install/linux.md) | `--db local` installs MariaDB on the machine and creates the database, or give `--db HOST:PORT/DB:USER:PASSWORD` |
| [Proxmox container](../install/proxmox.md) | MariaDB inside the container, or a server you name |
| [setup.sh](../install/setup-script.md) | asks for the details |

## Setting it up

iopix reads the connection from `viewer-db.json` in the data folder, or from the environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DB_TYPE` | | `mysql` for MariaDB or MySQL |
| `DB_HOST` | | server address |
| `DB_PORT` | `3306` | server port |
| `DB_NAME` | | database name |
| `DB_USER` | | account |
| `DB_PASSWORD` | | password |

Without either, iopix starts in a **database needed** state: the web app shows only a set-up page, the same panel as **Settings › digiKam and server › iopix database**, asking for the server details. **Test connection** checks them; **Use this database** saves them to `viewer-db.json` and restarts iopix on the new database. Until then nothing else answers: API calls return 503 with `needsDatabase: true`.

If the configured server cannot be reached at start, the same page shows the error and a **Retry** button only. Nobody can sign in without the database, so the page does not let anyone test or switch to another server then; to change the connection, edit `data/viewer-db.json` (or the `DB_*` variables) and restart. iopix waits for the database; it does not fall back to anything else.

!!! tip "Server on the Docker host"
    From a container, `DB_HOST: host.docker.internal` reaches a server on the same machine.

## Moving to another database

Create the new database as above, then in **Settings › digiKam and server › iopix database** enter its details, press **Test connection** and then **Use this database**. Every row is copied across and iopix restarts on the new database. The old database is left as it was.

## Backups

**Maintenance › Storage and backups › Download a backup › iopix data** gives `viewer.sql`, a `mysqldump` of the iopix database (the MCP admin token left out), plus the content-search embeddings files and a README with the restore steps. To restore, create an empty database and load the dump into it:

```sh
mariadb iopix < viewer.sql
```

then point iopix at it. If you already back the database server up, that covers iopix too.

Before a version changes the shape of the database, iopix writes a dump to `db-backups/` in the data folder as a `.sql` file.

## What stays in the data folder

Two cache files remain SQLite files in the data folder and need no server: the search index (`index.db`) and the merge-base snapshot (`library-base.sqlite`) used by [Sync with digiKam](sync.md). Both are rebuilt when needed and can be deleted.
