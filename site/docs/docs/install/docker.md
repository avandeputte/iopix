# Docker

The image `ghcr.io/avandeputte/iopix` is public, built for **amd64 and arm64**, and needs no registry login. It runs as its own user (uid 1000), not as root.

## 1. Get the compose file

```sh
mkdir iopix && cd iopix
curl -O https://raw.githubusercontent.com/avandeputte/iopix/main/docker-compose.yml
```

It also works as a stack in Dockge or Portainer: paste its contents in.

## 2. Edit the three CHANGE ME spots

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
      AUTO_MODELS: siglip2,yolox,geonames
      DIGIKAM_CHECK: none
volumes:
  iopix-data:
```

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

**Update now** in Maintenance does not apply in Docker; it tells you to pull instead.

## Upgrading an old volume

A data volume or bind-mounted folder from before the image ran as uid 1000 is still owned by root. Fix it once:

```sh
docker run --rm -v iopix-data:/data alpine chown -R 1000:1000 /data
```

## Health check

The container's health check calls `/api/status`, which answers even when sign-in is required.
