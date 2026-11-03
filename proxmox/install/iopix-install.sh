#!/usr/bin/env bash

# Copyright (c) 2026 Alex Van de Putte
# License: MIT
# Source: https://github.com/avandeputte/iopix
# iopix: runs inside the LXC, driven by proxmox/ct/iopix.sh (community-scripts framework).
# This installs the system packages (MariaDB for iopix's own database among them), creates that database and
# fetches the code; the ct script then runs the app's own setup.sh inside the container with the answers given
# on the host (features, models, admin, service). An update (the ct script's update_script) runs update.sh
# only, never this again, so the database is created once and kept.

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt-get install -y \
  git \
  python3 \
  python3-venv \
  python3-pip \
  ffmpeg \
  libimage-exiftool-perl \
  libjpeg-turbo-progs \
  libheif1 \
  libglib2.0-0 \
  mariadb-client \
  mariadb-server \
  curl
msg_ok "Installed Dependencies"

# iopix's own database (settings, accounts, favorites, albums, AI results) is MariaDB-only: a server in the
# container, with the database `iopix` and a user of the same name under a random password. The credentials wait
# in a root-only file for setup.sh (the ct script reads it into setup.sh's environment when the answer on the
# host was "local"; setup.sh then writes VIEWER_DB_* to /opt/iopix/app/iopix.env). CREATE ... IF NOT EXISTS keeps
# an existing database; nothing here is run again on an update.
msg_info "Setting up MariaDB for iopix's database"
$STD systemctl enable --now mariadb
VDB_PASS="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)"  # letters and digits only: safe in SQL, the env file and the DSN
mariadb <<SQL
CREATE DATABASE IF NOT EXISTS \`iopix\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'iopix'@'localhost' IDENTIFIED BY '$VDB_PASS'; ALTER USER 'iopix'@'localhost' IDENTIFIED BY '$VDB_PASS';
CREATE USER IF NOT EXISTS 'iopix'@'127.0.0.1' IDENTIFIED BY '$VDB_PASS'; ALTER USER 'iopix'@'127.0.0.1' IDENTIFIED BY '$VDB_PASS';
GRANT ALL PRIVILEGES ON \`iopix\`.* TO 'iopix'@'localhost'; GRANT ALL PRIVILEGES ON \`iopix\`.* TO 'iopix'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
mkdir -p /opt/iopix
(umask 077; printf 'IOPIX_VIEWER_DB=127.0.0.1:3306/iopix:iopix:%s\n' "$VDB_PASS" > /opt/iopix/viewer-db.env)
msg_ok "Set up MariaDB (database iopix, user iopix; credentials in /opt/iopix/viewer-db.env, root only)"

msg_info "Fetching iopix"
REPO="${IOPIX_REPO:-https://github.com/avandeputte/iopix}"
mkdir -p /opt/iopix /opt/iopix/data /library
$STD git clone --depth 1 "$REPO" /opt/iopix/app
git -C /opt/iopix/app rev-parse HEAD > /opt/iopix/app/COMMIT  # the installed commit, shown in Maintenance
rm -rf /opt/iopix/app/.git
msg_ok "Fetched iopix"

motd_ssh
customize

msg_info "Cleaning up"
$STD apt-get -y autoremove
$STD apt-get -y autoclean
msg_ok "Cleaned"
