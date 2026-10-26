# Choosing an install

There are four ways to install iopix. They all run the same app; they differ in how it is packaged and updated.

| | Best for | Updates | Notes |
| --- | --- | --- | --- |
| [**Docker**](docker.md) | a home server or NAS that already runs containers (Dockge, Portainer, Unraid, Synology) | `docker compose pull && docker compose up -d` | image for Intel and ARM; models live in a volume |
| [**Proxmox container**](proxmox.md) | a Proxmox VE host | `update` inside the container, or **Update now** in the app | creates a Debian container and mounts your library |
| [**Linux installer**](linux.md) | any Linux server or VM (Debian, Ubuntu, Fedora, openSUSE, Arch) | **Update now** in the app, or `update.sh` | one file; installs a systemd service |
| [**setup.sh**](setup-script.md) | the Mac or PC digiKam runs on, or a developer checkout | **Update now** in the app, or `update.sh` | can start at login (launchd or systemd) |

## Which one should I pick?

- **digiKam on a desktop, photos on a NAS?** Run iopix on the NAS with Docker, pointed at the same photo folder and database.
- **Already using Proxmox?** The Proxmox script is the quickest way to a tidy, separate container.
- **Everything on one Mac?** `setup.sh` on that Mac, with "start at login" on.
- **A plain Linux box?** The Linux installer.

## Things that are the same everywhere

- iopix listens on **port 8600**.
- It needs **read access** to the photo folders and digiKam's database, and **write access** only if you want to edit.
- Its own data goes in a separate data folder (`/data` in Docker, `/opt/iopix/data` with the installers, `data/` in a checkout).
- The admin account can be created during install, or later from Settings.
