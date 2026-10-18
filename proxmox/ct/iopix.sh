#!/usr/bin/env bash
# iopix LXC for Proxmox VE, in the community-scripts (ProxmoxVE) framework layout:
# this is the ct/ script; proxmox/install/iopix-install.sh runs inside the container.
#
# After the framework's own questions (resources, network) it asks about the app: where the photos
# and digiKam's database are on this host (or MariaDB details), which features to install (each
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

# the DKV_* names from before the rename still fill in the answers
for _v in PHOTOS DB FEATURES WRITE ADMIN; do eval "[[ -n \"\${IOPIX_$_v:-}\" ]] || IOPIX_$_v=\"\${DKV_$_v:-}\""; done
# ---- the app's own questions (whiptail on the host; env vars IOPIX_* skip them for unattended runs)
ask_app_settings() {
  local bt="iopix"
  IOPIX_PHOTOS="${IOPIX_PHOTOS:-$(whiptail --backtitle "$bt" --inputbox "Folder on this Proxmox host with the photos digiKam manages (bind-mounted read-only into the container):" 10 72 "/mnt/photos" 3>&1 1>&2 2>&3)}" || exit 1
  [[ -d "$IOPIX_PHOTOS" ]] || { msg_error "Folder not found on this host: $IOPIX_PHOTOS"; exit 1; }
  if [[ -z "${IOPIX_DB:-}" ]]; then
    if whiptail --backtitle "$bt" --yesno "Is digiKam's database on MySQL/MariaDB?\n\nNo: the SQLite file digikam4.db in a folder on this host." 11 72 3>&1 1>&2 2>&3; then
      local h p d u w
      h=$(whiptail --backtitle "$bt" --inputbox "MariaDB host or IP" 9 60 "192.168.1.2" 3>&1 1>&2 2>&3) || exit 1
      p=$(whiptail --backtitle "$bt" --inputbox "Port" 9 60 "3306" 3>&1 1>&2 2>&3) || exit 1
      d=$(whiptail --backtitle "$bt" --inputbox "Database" 9 60 "digikam" 3>&1 1>&2 2>&3) || exit 1
      u=$(whiptail --backtitle "$bt" --inputbox "User" 9 60 "digikam" 3>&1 1>&2 2>&3) || exit 1
      w=$(whiptail --backtitle "$bt" --passwordbox "Password" 9 60 3>&1 1>&2 2>&3) || exit 1
      IOPIX_DB="mysql:$h:$p/$d:$u:$w"
    else
      local f
      f=$(whiptail --backtitle "$bt" --inputbox "Folder on this host containing digikam4.db (bind-mounted read-only; drop ro=1 later for database writes):" 10 72 "/mnt/digikamDB" 3>&1 1>&2 2>&3) || exit 1
      [[ -f "$f/digikam4.db" ]] || { msg_error "digikam4.db not found in $f"; exit 1; }
      IOPIX_DB="sqlite:$f"
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
    if whiptail --backtitle "$bt" --yesno "May the viewer write into the library?\n\nYes: metadata edits go to XMP sidecar files next to the photos (and to digiKam's database on request). The library is mounted read/write and the container is created PRIVILEGED, because an unprivileged container cannot write files owned by a host user.\n\nNo: browsing only, read-only mounts, unprivileged container." 16 76 3>&1 1>&2 2>&3; then IOPIX_WRITE=1; else IOPIX_WRITE=0; fi
  fi
  if [[ -z "${IOPIX_ADMIN:-}" ]]; then
    local an aw
    an=$(whiptail --backtitle "$bt" --inputbox "Admin user name (visitors browse read-only; editors and admins sign in)" 9 72 "admin" 3>&1 1>&2 2>&3) || exit 1
    aw=$(whiptail --backtitle "$bt" --passwordbox "Admin password (8 characters or more; empty: no account yet)" 9 72 3>&1 1>&2 2>&3) || exit 1
    [[ -n "$aw" ]] && IOPIX_ADMIN="$an:$aw" || IOPIX_ADMIN=""
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

ask_app_settings
if [[ "${IOPIX_WRITE:-0}" = 1 ]]; then var_unprivileged="0"; fi
start
build_container
description

# ---- the library into the container, then the app's own setup with the answers
msg_info "Mounting the library into CT ${CTID}"
RO=",ro=1"; [[ "${IOPIX_WRITE:-0}" = 1 ]] && RO=""
pct set "$CTID" -mp0 "$IOPIX_PHOTOS,mp=/library/photos$RO"
if [[ "$IOPIX_DB" == sqlite:* ]]; then
  pct set "$CTID" -mp1 "${IOPIX_DB#sqlite:},mp=/library/digikamDB$RO"
  DBARG="--sqlite /library/digikamDB"
else
  DBARG="--mysql $(printf '%q' "${IOPIX_DB#mysql:}")"
fi
pct reboot "$CTID"; sleep 8
msg_ok "Mounted the library"

msg_info "Running the app's setup inside the container (packages, models, settings, admin, service)"
ADMINARG=""; [[ -n "${IOPIX_ADMIN:-}" ]] && ADMINARG="--admin $(printf '%q' "$IOPIX_ADMIN")"
SETUP_CMD="cd /opt/iopix/app && sh ./setup.sh --yes --photos /library/photos $DBARG --data /opt/iopix/data --features '${IOPIX_FEATURES:-none}' $ADMINARG --service --no-system-deps --port 8600"
# the setup's output is kept in the container; on failure the last lines are shown here
if ! pct exec "$CTID" -- bash -c "$SETUP_CMD > /opt/iopix/setup.log 2>&1"; then
  msg_error "setup.sh failed inside the container; last lines of /opt/iopix/setup.log:"
  pct exec "$CTID" -- tail -n 40 /opt/iopix/setup.log || true
  msg_error "Fix the cause, then run it again with: pct exec $CTID -- bash -c \"$SETUP_CMD\""
  exit 1
fi
# the container cannot see digiKam's process on the host: ask before database writes instead of pgrep
pct exec "$CTID" -- sh -c "grep -q ^DIGIKAM_CHECK= /opt/iopix/app/iopix.env || echo DIGIKAM_CHECK=none >> /opt/iopix/app/iopix.env; systemctl restart iopix"
msg_ok "Setup finished"

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Access it using the following URL:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:8600${CL}"
[[ "${IOPIX_WRITE:-0}" = 1 ]] && echo -e "${INFO}${YW}The library is mounted read/write (sidecar edits, database writes on request).${CL}" || echo -e "${INFO}${YW}The library is mounted read-only (browsing only). To allow edits later: recreate with write access, or make the container privileged and remount without ro=1.${CL}"
