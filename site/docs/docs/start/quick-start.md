# Quick start

This page gets iopix running against your digiKam library with the fewest decisions. Every step links to a fuller page if you want more.

## 1. Check what you have

You need:

- A **digiKam library**: the folder with your photos and digiKam's database. The database is either a file called `digikam4.db` (SQLite, digiKam's default) or a MariaDB/MySQL server.
- Or, if you don't use digiKam, just the **folder with your photos**: see [Using iopix without digiKam](../connect/no-digikam.md).
- A **computer that can see them**, which stays on: a home server, a NAS that runs Docker, a Proxmox host, or the Mac or Linux PC digiKam runs on.

See [What you need](requirements.md) for the details.

## 2. Install

Pick the way that matches your machine. If you are not sure, Docker is the easiest to keep tidy.

=== "Docker"

    ```sh
    curl -O https://raw.githubusercontent.com/avandeputte/iopix/main/docker-compose.yml
    # edit the three CHANGE ME lines: photo folder, digiKam database, admin password
    docker compose up -d
    ```

    Full guide: [Docker](../install/docker.md).

=== "Proxmox"

    On the Proxmox host, as root:

    ```sh
    bash -c "$(curl -fsSL https://raw.githubusercontent.com/avandeputte/iopix/main/proxmox/ct/iopix.sh)"
    ```

    Full guide: [Proxmox container](../install/proxmox.md).

=== "Linux"

    Download `iopix-install.sh` from the [latest release](https://github.com/avandeputte/iopix/releases/latest), then as root:

    ```sh
    sh iopix-install.sh --photos /srv/photos --digikam-db /srv/digikamDB
    # or, without digiKam:
    sh iopix-install.sh --photos /srv/photos --no-digikam
    ```

    Full guide: [Linux installer](../install/linux.md).

=== "macOS"

    ```sh
    git clone https://github.com/avandeputte/iopix && cd iopix
    ./setup.sh
    ```

    Full guide: [setup.sh](../install/setup-script.md).

Each of these asks for (or lets you set) the photo folder, digiKam's database (or that you don't use digiKam), and an admin account.

## 3. Open it

Go to `http://<your-server>:8600` in a browser. The first visit builds the thumbnails in the background; you can browse straight away while it works.

!!! tip "Use it from anywhere"
    iopix speaks plain HTTP. To use it away from home, or to install it on a phone, put it behind a reverse proxy with HTTPS. See [HTTPS and a reverse proxy](../install/reverse-proxy.md).

## 4. Sign in and set up

1. Sign in with the admin account from the person icon.
2. Open **Settings** to choose the theme, the name iopix shows, and the map provider.
3. Open **Maintenance › AI analysis and models** to download the features you want, such as content search or places.
4. Create accounts for the family under **Settings › Users and sign-in**, and link each to their person from People.

## 5. Turn on editing when you are ready

iopix writes nothing to your library until someone with editor rights makes a change. Read [How editing works](../edit/how-editing-works.md) before you start, so you know where each change goes and how to undo it.
