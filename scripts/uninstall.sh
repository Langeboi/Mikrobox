#!/usr/bin/env bash
# Removes a named (side-by-side) install completely, e.g. the test build:
#
#   sudo ./scripts/uninstall.sh --name test
#
# Stops its devices, removes its service, folders and host network leftovers. The main
# install is never touched, and this script refuses to remove it.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run this as root (sudo)." >&2; exit 1; }
NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    *) echo "Usage: sudo ./scripts/uninstall.sh --name NAME" >&2; exit 1 ;;
  esac
done
[[ "$NAME" =~ ^[a-z][a-z0-9-]{0,15}$ ]] && [ "$NAME" != "main" ] || {
  echo "Give the name of a side-by-side install, e.g. --name test. The main install is not removed by this." >&2
  exit 1
}
ENVFILE="/etc/mikrobox/$NAME.env"
[ -f "$ENVFILE" ] || { echo "No install called '$NAME' ($ENVFILE is missing)." >&2; exit 1; }
TAG="$(sed -n 's/^MIKROBOX_TAG=//p' "$ENVFILE" | head -1)"
[[ "$TAG" =~ ^[a-z]{2}$ ]] && [ "$TAG" != "mb" ] || { echo "Unexpected tag '$TAG' in $ENVFILE; stopping." >&2; exit 1; }
SERVICE="mikrobox-$NAME"
RUN="/run/$SERVICE"

echo "This removes the '$NAME' install: its labs, devices, accounts and images."
if [ -t 0 ]; then
  read -r -p "Type the name '$NAME' to continue: " answer
  [ "$answer" = "$NAME" ] || { echo "Nothing removed."; exit 1; }
fi

echo "==> Stopping the service and its devices"
systemctl disable --now "$SERVICE.service" 2>/dev/null || true
for pidfile in "$RUN"/n*.pid; do
  [ -f "$pidfile" ] || continue
  pid="$(cat "$pidfile")"
  grep -q qemu "/proc/$pid/cmdline" 2>/dev/null && kill "$pid" 2>/dev/null || true
done
[ -f "$RUN/dnsmasq.pid" ] && kill "$(cat "$RUN/dnsmasq.pid")" 2>/dev/null || true
sleep 2

echo "==> Removing its host network names (${TAG}...)"
for ns in $(ip netns list 2>/dev/null | awk '{print $1}' | grep -E "^${TAG}c[0-9]+$" || true); do
  ip netns del "$ns" || true
  rm -rf "/etc/netns/$ns"
done
for dev in $(ls /sys/class/net | grep -E "^${TAG}[0-9]+p[0-9]+$" || true); do
  ip link del "$dev" 2>/dev/null || true
done
ip link del "${TAG}cloud" 2>/dev/null || true
nft delete table ip "mikrobox_${TAG}" 2>/dev/null || true

echo "==> Removing its files"
rm -f "/etc/systemd/system/$SERVICE.service" "$ENVFILE"
systemctl daemon-reload
rm -rf "/opt/mikrobox-$NAME" "/var/lib/mikrobox-$NAME" "$RUN"
echo "Done. The '$NAME' install is gone; the main install was not touched."
echo "The folder you installed it from is still there; delete it yourself if you like."
