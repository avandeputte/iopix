# digiKam on MariaDB or MySQL

digiKam can keep its database on a MariaDB or MySQL server, which suits a library shared by several computers. iopix supports this fully, for reading and writing.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DIGIKAM_DB_TYPE` | `sqlite` | `mysql` for MariaDB or MySQL |
| `DIGIKAM_DB_HOST` | `127.0.0.1` | server address |
| `DIGIKAM_DB_PORT` | `3306` | server port |
| `DIGIKAM_DB_NAME` | `digikam` | database name |
| `DIGIKAM_DB_USER` | `digikam` | account |
| `DIGIKAM_DB_PASSWORD` | empty | password |

The same can be set in **Settings › digiKam and server**, which tests the connection before saving.

!!! tip "Server on the Docker host"
    From a container, use `DIGIKAM_DB_HOST: host.docker.internal` to reach a MariaDB server on the same machine.

## Backups before writes

Before anything destructive (deleting photos, deleting a tag, merging changes back), iopix takes a full dump of digiKam's database with `mysqldump` or `mariadb-dump`. The Docker image and the installers include it.

| Variable | Meaning |
| --- | --- |
| `DIGIKAM_DB_BACKUP=auto` | a dump before destructive operations (default); without a dump tool, those operations are refused |
| `DIGIKAM_DB_BACKUP=skip` | never; your server is backed up another way |
| `DIGIKAM_DB_DUMP` | a command that writes a full dump to standard output, used instead of `mysqldump`, for example through `docker exec` |

## Dates and times

digiKam's source writes UTC times on MySQL, but many real libraries hold local time there. iopix measures which one yours uses when it starts, by comparing digiKam's recorded file times with the files themselves. If it cannot see the files, set `DIGIKAM_MYSQL_DATES` to `local` or `utc`.

## Account rights

For browsing, the account only needs to read. For editing it needs to insert, update and delete rows in digiKam's tables, the same rights digiKam itself has. Using digiKam's own account is simplest.
