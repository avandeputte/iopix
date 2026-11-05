# Docker

The image `ghcr.io/avandeputte/iopix` is public, built for **amd64 and arm64**, and needs no registry login. It runs as its own user (uid 1000), not as root.

## 1. Get the compose file

```sh
mkdir iopix && cd iopix
curl -O https://raw.githubusercontent.com/avandeputte/iopix/main/docker-compose.yml
```

It also works as a stack in Dockge or Portainer: paste its contents in.

## 2. Edit the CHANGE ME spots

```yaml
services:
  iopix:
    image: ghcr.io/avandeputte/iopix:latest
    container_name: iopix
    restart: unless-stopped
    ports:
      - "8600:8600"
    volumes:
      - /path/to/photos:/library/photos:ro      # 1. the folder digiKam manages
      - iopix-data:/data
    environment:
      TZ: Europe/Brussels
      DIGIKAM_DB_TYPE: mysql                     # 2. digiKam's database
      DIGIKAM_DB_HOST: db.example.lan
      DIGIKAM_DB_PORT: "3306"
      DIGIKAM_DB_NAME: digikam
      DIGIKAM_DB_USER: digikam
      DIGIKAM_DB_PASSWORD: change-me
      PHOTOS_ROOT: /library/photos
      ADMIN_USER: admin                          # 3. the first admin account
      ADMIN_PASSWORD: change-me-please
      DB_TYPE: mysql                      # iopix's own database: the iopix-db service below
      DB_HOST: iopix-db
      DB_PORT: "3306"
      DB_NAME: iopix
      DB_USER: iopix
      DB_PASSWORD: change-me-too          # 4. same as MARIADB_PASSWORD below
      AUTO_MODELS: siglip2,yolox,geonames
      DIGIKAM_CHECK: none
    depends_on:
      - iopix-db
  iopix-db:
    image: mariadb:11
    container_name: iopix-db
    restart: unless-stopped
    volumes:
      - iopix-db:/var/lib/mysql
    environment:
      MARIADB_DATABASE: iopix
      MARIADB_USER: iopix
      MARIADB_PASSWORD: change-me-too
      MARIADB_RANDOM_ROOT_PASSWORD: "1"
volumes:
  iopix-data:
  iopix-db:
```

The `iopix-db` service is MariaDB for iopix's own data (accounts, settings, favorites, albums, the edit log, AI results); it has nothing to do with digiKam's database. To use a server you already run instead, drop the service and point the `DB_*` lines at it (`host.docker.internal` for a server on the Docker host). See [iopix's own database](../connect/iopix-database.md).

!!! note "No digiKam"
    Set `DIGIKAM_DB_TYPE: none` and remove the other `DIGIKAM_DB_*` lines. iopix then keeps the library itself, in the data volume. Remove `:ro` from the photos line so edits can be saved next to the photos. See [Using iopix without digiKam](../connect/no-digikam.md).

!!! note "digiKam on SQLite instead of MariaDB"
    Remove the `DIGIKAM_DB_*` lines and mount the folder that holds `digikam4.db`:

    ```yaml
          - /path/to/digikamDB:/library/digikamDB:ro
    ```

## 3. Start it

```sh
docker compose up -d
```

Open `http://<server>:8600`. On the first start the container downloads the models named in `AUTO_MODELS` (about 1.6 GB by default: content search, object detection and places) into the data volume, one after another. The library is browsable meanwhile, and each feature switches on as its model arrives. Set `AUTO_MODELS: ""` to skip this.

!!! note "Upgrading from a version before 1.113"
    If the data volume still holds `viewer.db`, add the `iopix-db` service and the `DB_*` lines, then open iopix: its set-up page offers to import the old file into the new database.

## Allowing edits

The example mounts the library **read-only** (`:ro`), which is the safest way to start. To edit:

- drop `:ro` from the photo mount, so sidecars can be written next to the photos;
- with digiKam on SQLite, mount the digiKam folder writable too;
- make sure uid 1000 may write those folders on the host.

With MariaDB nothing extra is mounted: the image includes `mariadb-dump`, which backs digiKam's database up before each destructive write.

!!! warning "digiKam open at the same time"
    A container cannot see whether digiKam is running on another machine, so the image sets `DIGIKAM_CHECK=none`. Avoid editing the same photo in iopix and digiKam at the same moment.

## Models

Models are not in the image; they live in the `/data` volume and survive updates. Download more from **Maintenance › AI analysis and models**, or from the command line:

```sh
docker compose exec -e HF_HUB_OFFLINE=0 iopix python -m app.ai.models download blip
```

The model keys are listed on the [Models](../ai/models.md) page.

## Build arguments

To build the image yourself, `docker compose up -d --build` uses the checkout. Two build arguments choose what goes in:

| Argument | Default | Adds |
| --- | --- | --- |
| `AI` | `1` | content search, text recognition and object detection packages |
| `CAPTIONS` | `0` | PyTorch (CPU) and transformers for photo descriptions, about 1 GB |

## Updating

```sh
docker compose pull && docker compose up -d
```

The `iopix-db` volume holds iopix's database and survives updates like the data volume does. **Download a backup** in Maintenance includes a dump of it.

**Update now** in Maintenance does not apply in Docker; it tells you to pull instead.

## Upgrading an old volume

A data volume or bind-mounted folder from before the image ran as uid 1000 is still owned by root. Fix it once:

```sh
docker run --rm -v iopix-data:/data alpine chown -R 1000:1000 /data
```

## Health check

The container's health check calls `/api/status`, which answers even when sign-in is required.
