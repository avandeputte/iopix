#!/usr/bin/env bash
# iopix LXC for Proxmox VE, in the community-scripts (ProxmoxVE) framework layout:
# this is the ct/ script; proxmox/install/iopix-install.sh runs inside the container.
#
# After the framework's own questions (resources, network) it asks about the app: where the photos
# and digiKam's database are on this host (or MariaDB details, or no digiKam at all), where iopix keeps its own database
# (a MariaDB inside the container, installed by the install script, or an existing server), which features to install (each
# downloads its model), and the admin account. It then creates the container, bind-mounts the
# library into it and runs the app's own setup.sh inside with those answers, which installs the
# packages, downloads the models, writes the settings, creates the admin and starts the service.
#
# Until the scripts are merged upstream, the framework is pointed at this repository for the
# install script (COMMUNITY_SCRIPTS_URL below; the core engine still comes from community-scripts).
# Run on the Proxmox host as root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/avandeputte/iopix/main/proxmox/ct/iopix.sh)"
export COMMUNITY_SCRIPTS_URL="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/avandeputte/iopix/main/proxmox}"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")
# Copyright (c) 2026 Alex Van de Putte
# License: MIT
# Source: https://github.com/avandeputte/iopix

APP="iopix"
var_tags="${var_tags:-photos;media}"
var_cpu="${var_cpu:-4}"
var_ram="${var_ram:-4096}"
var_disk="${var_disk:-16}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_arm64="${var_arm64:-yes}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

# ---- the app's own questions (whiptail on the host; env vars IOPIX_* skip them for unattended runs)
ask_app_settings() {
  local bt="iopix"
  IOPIX_PHOTOS="${IOPIX_PHOTOS:-$(whiptail --backtitle "$bt" --inputbox "Folder on this Proxmox host with your photos (bind-mounted into the container):" 10 72 "/mnt/photos" 3>&1 1>&2 2>&3)}" || exit 1
  [[ -d "$IOPIX_PHOTOS" ]] || { msg_error "Folder not found on this host: $IOPIX_PHOTOS"; exit 1; }
  if [[ -z "${IOPIX_DIGIKAM_DB:-}" ]]; then  # IOPIX_DIGIKAM_DB: sqlite:DIR, mysql:HOST:PORT/DB:USER:PASS or none
    local kind
    kind=$(whiptail --backtitle "$bt" --menu "Where is digiKam's database?" 14 76 3 \
      sqlite "The SQLite file digikam4.db, in a folder on this host" \
      mysql "MySQL / MariaDB" \
      none "I don't use digiKam: iopix keeps the library itself" 3>&1 1>&2 2>&3) || exit 1
    if [[ "$kind" == none ]]; then
      IOPIX_DIGIKAM_DB="none"
    elif [[ "$kind" == mysql ]]; then
      local h p d u w
      h=$(whiptail --backtitle "$bt" --inputbox "MariaDB host or IP" 9 60 "127.0.0.1" 3>&1 1>&2 2>&3) || exit 1
      p=$(whiptail --backtitle "$bt" --inputbox "Port" 9 60 "3306" 3>&1 1>&2 2>&3) || exit 1
      d=$(whiptail --backtitle "$bt" --inputbox "Database" 9 60 "digikam" 3>&1 1>&2 2>&3) || exit 1
      u=$(whiptail --backtitle "$bt" --inputbox "User" 9 60 "digikam" 3>&1 1>&2 2>&3) || exit 1
      w=$(whiptail --backtitle "$bt" --passwordbox "Password" 9 60 3>&1 1>&2 2>&3) || exit 1
      IOPIX_DIGIKAM_DB="mysql:$h:$p/$d:$u:$w"
    else
      local f
      f=$(whiptail --backtitle "$bt" --inputbox "Folder on this host containing digikam4.db (bind-mounted read-only; drop ro=1 later for database writes):" 10 72 "/mnt/digikamDB" 3>&1 1>&2 2>&3) || exit 1
      [[ -f "$f/digikam4.db" ]] || { msg_error "digikam4.db not found in $f"; exit 1; }
      IOPIX_DIGIKAM_DB="sqlite:$f"
    fi
  fi
  [[ "${IOPIX_DB:-}" != sqlite ]] || { msg_error "iopix's own database is on MySQL/MariaDB only (no SQLite file any more): IOPIX_DB=local, or HOST:PORT/DB:USER:PASS"; exit 1; }
  if [[ -z "${IOPIX_DB:-}" ]]; then  # IOPIX_DB: local (MariaDB in the container) or HOST:PORT/DB:USER:PASS
    local holds="settings, accounts, favorites, albums, face groups, the edit log and AI results"
    [[ "$IOPIX_DIGIKAM_DB" == none ]] && holds="the whole library (folders, tags, people and faces, captions, places) plus settings, accounts, favorites, albums and AI results"
    local vkind
    vkind=$(whiptail --backtitle "$bt" --menu "Where does iopix keep its own database (MySQL/MariaDB)?\n\nIt holds $holds." 16 76 2 \
      local "MariaDB inside the container (installed and set up for you)" \
      mysql "An existing MySQL / MariaDB server (digiKam's, for instance)" 3>&1 1>&2 2>&3) || exit 1
    if [[ "$vkind" == mysql ]]; then
      local vh vp vd vu vw dh="127.0.0.1" dp="3306" du="iopix"
      if [[ "$IOPIX_DIGIKAM_DB" == mysql:* ]]; then local r="${IOPIX_DIGIKAM_DB#mysql:}"; dh="${r%%:*}"; r="${r#*:}"; dp="${r%%/*}"; r="${r#*/}"; r="${r#*:}"; du="${r%%:*}"; fi
      vh=$(whiptail --backtitle "$bt" --inputbox "MariaDB host or IP for iopix's database" 9 60 "$dh" 3>&1 1>&2 2>&3) || exit 1
      vp=$(whiptail --backtitle "$bt" --inputbox "Port" 9 60 "$dp" 3>&1 1>&2 2>&3) || exit 1
      vd=$(whiptail --backtitle "$bt" --inputbox "Database (created if the user may)" 9 60 "iopix" 3>&1 1>&2 2>&3) || exit 1
      vu=$(whiptail --backtitle "$bt" --inputbox "User" 9 60 "$du" 3>&1 1>&2 2>&3) || exit 1
      vw=$(whiptail --backtitle "$bt" --passwordbox "Password" 9 60 3>&1 1>&2 2>&3) || exit 1
      IOPIX_DB="$vh:$vp/$vd:$vu:$vw"
    else
      IOPIX_DB="local"
    fi
  fi
  if [[ -z "${IOPIX_FEATURES:-}" ]]; then
    IOPIX_FEATURES=$(whiptail --backtitle "$bt" --checklist "Features (each downloads its model into the container):" 15 78 5 \
      search "Content search: SigLIP 2 (~1.5 GB)" ON \
      places "Places from GPS positions, offline (GeoNames ~30 MB)" ON \
      objects "Object detection (YOLOX 35 MB)" ON \
      ocr "Text in photos (RapidOCR ~60 MB)" ON \
      captions "Photo descriptions (BLIP + PyTorch ~1.5 GB, slow on CPU)" OFF 3>&1 1>&2 2>&3) || exit 1
    IOPIX_FEATURES=$(echo "$IOPIX_FEATURES" | tr -d '"' | tr ' ' ',')
  fi
  if [[ -z "${IOPIX_WRITE:-}" ]]; then
    if whiptail --backtitle "$bt" --yesno "May iopix write into the photo folder?\n\nYes: metadata edits go to XMP sidecar files next to the photos (and to digiKam's database, if you use one). The library is mounted read/write and the container is created PRIVILEGED, because an unprivileged container cannot write files owned by a host user.\n\nNo: browsing only, read-only mounts, unprivileged container." 16 76 3>&1 1>&2 2>&3; then IOPIX_WRITE=1; else IOPIX_WRITE=0; fi
  fi
  if [[ -z "${IOPIX_SUITE_DB:-}" ]]; then  # IOPIX_SUITE_DB: HOST:PORT/DB:USER:PASS of the Shared ioSuite database
    local sh sp sd su sw
    sh="127.0.0.1"; [[ "$IOPIX_DB" == local ]] || { sh="${IOPIX_DB%%/*}"; sh="${sh%%:*}"; }
    sh=$(whiptail --backtitle "$bt" --inputbox "Shared ioSuite database: MariaDB host or IP\n\nAccounts, sessions and suite-wide settings, shared with the other ioThing apps (one sign-in for all)." 12 76 "$sh" 3>&1 1>&2 2>&3) || exit 1
    sp=$(whiptail --backtitle "$bt" --inputbox "Shared ioSuite database: port" 9 60 "3306" 3>&1 1>&2 2>&3) || exit 1
    sd=$(whiptail --backtitle "$bt" --inputbox "Shared ioSuite database: database (created if the user may)" 9 72 "iosuite" 3>&1 1>&2 2>&3) || exit 1
    su=$(whiptail --backtitle "$bt" --inputbox "Shared ioSuite database: user" 9 60 "iosuite" 3>&1 1>&2 2>&3) || exit 1
    sw=$(whiptail --backtitle "$bt" --passwordbox "Shared ioSuite database: password" 9 60 3>&1 1>&2 2>&3) || exit 1
    IOPIX_SUITE_DB="$sh:$sp/$sd:$su:$sw"
  fi
  if [[ -z "${IOPIX_PUBLIC_URL+x}" ]]; then
    IOPIX_PUBLIC_URL=$(whiptail --backtitle "$bt" --inputbox "Public address behind your reverse proxy, e.g. https://photo.example.com (empty: the address it is reached at)" 10 76 "" 3>&1 1>&2 2>&3) || exit 1
  fi
  if [[ -z "${IOPIX_COOKIE_DOMAIN+x}" ]]; then
    IOPIX_COOKIE_DOMAIN=$(whiptail --backtitle "$bt" --inputbox "Domain the sign-in is shared on by every app, e.g. example.com (empty: this host only)" 10 76 "" 3>&1 1>&2 2>&3) || exit 1
  fi
  if [[ -z "${IOPIX_ADMIN:-}" ]]; then
    local an aw
    an=$(whiptail --backtitle "$bt" --inputbox "Admin user name (visitors browse read-only; editors and admins sign in)" 9 72 "admin" 3>&1 1>&2 2>&3) || exit 1
    aw=$(whiptail --backtitle "$bt" --passwordbox "Admin password (8 characters or more; empty: no account yet)" 9 72 3>&1 1>&2 2>&3) || exit 1
    [[ -n "$aw" ]] && IOPIX_ADMIN="$an:$aw" || IOPIX_ADMIN=""
  fi
  if [[ -z "${IOPIX_ALSO+x}" ]]; then  # the other apps of the suite in the same container
    IOPIX_ALSO=$(whiptail --backtitle "$bt" --checklist "Also install in this container (same database server, each app its own database; same accounts and admin; each on its usual port):" 15 78 5 \
      iosuite "ioSuite (the start page)  (8630)" OFF \
      iocalendar "ioCalendar  (8620)" OFF \
      iocontacts "ioContacts  (8640, LDAP 3890)" OFF \
      ionotes "ionotes  (8610)" OFF \
      iohealth "ioHealth  (47800)" OFF 3>&1 1>&2 2>&3) || exit 1
    IOPIX_ALSO=$(echo "$IOPIX_ALSO" | tr -d '"' | tr ' ' ',')
  fi
  if [[ -n "$IOPIX_ALSO" ]]; then  # room for more than one app
    (( var_ram < 2048 )) && var_ram=2048; (( var_disk < 8 )) && var_disk=8
  fi
}

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  if [[ ! -d /opt/iopix/app ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi
  msg_info "Updating ${APP}"
  REPO="${IOPIX_REPO:-https://github.com/avandeputte/iopix}"
  # the app's own updater: latest code, same settings, data and packages, service restarted.
  # Installs made before update.sh existed fetch it first; its output is shown, not hidden.
  [[ -f /opt/iopix/app/update.sh ]] || curl -fsSL "https://raw.githubusercontent.com/avandeputte/iopix/main/update.sh" -o /opt/iopix/app/update.sh
  if sh /opt/iopix/app/update.sh; then msg_ok "Updated successfully!"; else msg_error "update.sh failed; see the lines above"; exit 1; fi
  exit
}

if command -v pveversion >/dev/null 2>&1; then ask_app_settings; fi  # on the Proxmox host; inside the container `update` needs no answers
if [[ "${IOPIX_WRITE:-0}" = 1 ]]; then var_unprivileged="0"; fi
start
build_container
description

# ---- the library into the container, then the app's own setup with the answers
msg_info "Mounting the library into CT ${CTID}"
RO=",ro=1"; [[ "${IOPIX_WRITE:-0}" = 1 ]] && RO=""
pct set "$CTID" -mp0 "$IOPIX_PHOTOS,mp=/library/photos$RO"
SECRETS=""  # setup.sh reads IOPIX_DIGIKAM_DB, IOPIX_DB, IOPIX_SUITE_DB and IOPIX_ADMIN from its environment: the passwords stay off every command line
if [[ "$IOPIX_DIGIKAM_DB" == none ]]; then
  DBARG="--no-digikam"
elif [[ "$IOPIX_DIGIKAM_DB" == sqlite:* ]]; then
  pct set "$CTID" -mp1 "${IOPIX_DIGIKAM_DB#sqlite:},mp=/library/digikamDB$RO"
  DBARG="--digikam-sqlite /library/digikamDB"
else
  DBARG=""; SECRETS+="IOPIX_DIGIKAM_DB=$(printf '%q' "${IOPIX_DIGIKAM_DB#mysql:}")"$'\n'
fi
[[ -n "${IOPIX_SUITE_DB:-}" ]] && SECRETS+="IOPIX_SUITE_DB=$(printf '%q' "$IOPIX_SUITE_DB")"$'\n'
pct reboot "$CTID"; sleep 8
msg_ok "Mounted the library"

msg_info "Running the app's setup inside the container (packages, models, settings, admin, service)"
# iopix's own database: "local" means the MariaDB the install script set up in the container; its credentials wait
# there in /opt/iopix/viewer-db.env (root only) and go into setup.sh's environment the same way as the other secrets
VDBENV=""; if [[ "${IOPIX_DB:-local}" == local ]]; then VDBENV=". /opt/iopix/viewer-db.env; "; else SECRETS+="IOPIX_DB=$(printf '%q' "$IOPIX_DB")"$'\n'; fi
[[ -n "${IOPIX_ADMIN:-}" ]] && SECRETS+="IOPIX_ADMIN=$(printf '%q' "$IOPIX_ADMIN")"$'\n'
# the answers with passwords go in as a root-only file (pushed, read into setup.sh's environment, removed), never as arguments
SECF="$(mktemp)"; chmod 600 "$SECF"; printf '%s' "$SECRETS" > "$SECF"
pct push "$CTID" "$SECF" /opt/iopix/setup.env --perms 0600 --user 0 --group 0
rm -f "$SECF"
SETUP_CMD="cd /opt/iopix/app && sh ./setup.sh --yes --photos /library/photos $DBARG --data /opt/iopix/data --features '${IOPIX_FEATURES:-none}' --service --no-system-deps --port 8600 --public-url '${IOPIX_PUBLIC_URL:-}' --cookie-domain '${IOPIX_COOKIE_DOMAIN:-}' --also '${IOPIX_ALSO:-none}'"
WRAPPED="set -a; . /opt/iopix/setup.env; ${VDBENV}set +a; rm -f /opt/iopix/setup.env; $SETUP_CMD > /opt/iopix/setup.log 2>&1"
# the setup's output is kept in the container; on failure the last lines are shown here
if ! pct exec "$CTID" -- bash -c "$WRAPPED"; then
  msg_error "setup.sh failed inside the container; last lines of /opt/iopix/setup.log:"
  pct exec "$CTID" -- tail -n 40 /opt/iopix/setup.log || true
  msg_error "Fix the cause, then run it again inside the container with IOPIX_ADMIN=name:password (and IOPIX_DIGIKAM_DB=host:port/db:user:password for a MariaDB digiKam, IOPIX_DB=the same form for iopix's own database, IOPIX_SUITE_DB for the Shared ioSuite database; for the container's own MariaDB it is in /opt/iopix/viewer-db.env) in the environment: pct exec $CTID -- bash -c \"$SETUP_CMD\""
  exit 1
fi
# the container cannot see digiKam's process on the host: ask before database writes instead of pgrep
pct exec "$CTID" -- sh -c "grep -q ^DIGIKAM_CHECK= /opt/iopix/app/config.env || echo DIGIKAM_CHECK=none >> /opt/iopix/app/config.env; systemctl restart iopix"
msg_ok "Setup finished"

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Access it using the following URL:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:8600${CL}"
for a in ${IOPIX_ALSO//,/ }; do
  case "$a" in iosuite) p=8630;; iocalendar) p=8620;; iocontacts) p=8640;; ionotes) p=8610;; iohealth) p=47800;; iopix) p=8600;; *) p="";; esac
  [[ -n "$p" ]] && echo -e "${INFO}${YW}$a:${CL} ${BGN}http://${IP}:$p${CL}"
done
[[ "${IOPIX_DB:-local}" == local ]] && echo -e "${INFO}${YW}iopix's own database: MariaDB inside the container (database iopix; the credentials are in /opt/iopix/app/config.env).${CL}" || echo -e "${INFO}${YW}iopix's own database: the MySQL/MariaDB server you named (DB_* in /opt/iopix/app/config.env).${CL}"
[[ "${IOPIX_WRITE:-0}" = 1 ]] && echo -e "${INFO}${YW}The library is mounted read/write (sidecar edits, database writes on request).${CL}" || echo -e "${INFO}${YW}The library is mounted read-only (browsing only). To allow edits later: recreate with write access, or make the container privileged and remount without ro=1.${CL}"
