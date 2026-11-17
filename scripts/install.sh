#!/usr/bin/env bash
# Installs or updates Mikrobox on a Debian or Ubuntu VM: packages, the CHR image, a systemd service.
#
#   sudo ./scripts/install.sh                          the main install, on port 8080
#   sudo ./scripts/install.sh --name test --port 8081  a second, separate install next to it
#
# A named install has its own folders, service, accounts, labs and host network names, so it
# can run a different branch without touching the main install. The name is remembered for
# this folder: later, a plain `sudo ./scripts/install.sh` here updates that same install.
#
# Options for a named install (defaults in brackets):
#   --port N          web port [8081]
#   --tag XX          two letters that prefix its interfaces, bridge and firewall table [mx]
#   --cloud-net CIDR  its Cloud's private range [10.98.0.0/16]
#   --label TEXT      shown in its top bar [Test build, from the name]
# Pin a RouterOS version with CHR_VERSION=7.x.y.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run this as root (sudo)." >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REMEMBER="$REPO/.mikrobox-install"

NAME="" PORT="" TAG="" CLOUD_NET="" LABEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --cloud-net) CLOUD_NET="${2:-}"; shift 2 ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
  esac
done

if [ -z "$NAME" ] && [ -f "$REMEMBER" ]; then
  NAME="$(tr -d '[:space:]' < "$REMEMBER")"
  echo "==> This folder belongs to the '$NAME' install; updating that one."
fi

if [ -n "$NAME" ]; then
  [[ "$NAME" =~ ^[a-z][a-z0-9-]{0,15}$ ]] || { echo "--name: lowercase letters, digits and dashes, e.g. test." >&2; exit 1; }
  [ "$NAME" != "main" ] || { echo "--name main is reserved; leave --name out for the main install." >&2; exit 1; }
  SERVICE="mikrobox-$NAME"
  PREFIX="/opt/mikrobox-$NAME"
  DATA="/var/lib/mikrobox-$NAME"
  ENVFILE="/etc/mikrobox/$NAME.env"
  # Keep earlier choices when updating, unless overridden now.
  if [ -f "$ENVFILE" ]; then
    old() { sed -n "s/^$1=//p" "$ENVFILE" | head -1; }
    PORT="${PORT:-$(old MIKROBOX_PORT)}"; TAG="${TAG:-$(old MIKROBOX_TAG)}"
    CLOUD_NET="${CLOUD_NET:-$(old MIKROBOX_CLOUD_NET)}"; LABEL="${LABEL:-$(old MIKROBOX_LABEL)}"
  fi
  PORT="${PORT:-8081}"; TAG="${TAG:-mx}"; CLOUD_NET="${CLOUD_NET:-10.98.0.0/16}"
  LABEL="${LABEL:-${NAME^} build}"
  [[ "$TAG" =~ ^[a-z]{2}$ ]] && [ "$TAG" != "mb" ] || { echo "--tag: two lowercase letters other than mb." >&2; exit 1; }
  [ "$CLOUD_NET" != "10.99.0.0/16" ] || { echo "--cloud-net: 10.99.0.0/16 is the main install's range." >&2; exit 1; }
else
  SERVICE="mikrobox"
  PREFIX="/opt/mikrobox"
  DATA="/var/lib/mikrobox"
  PORT="${PORT:-8080}"
  BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [[ "$BRANCH" == experimental/* ]]; then
    echo "This folder is on the $BRANCH branch, and this would replace your MAIN install with it." >&2
    echo "For a separate test install use:  sudo ./scripts/install.sh --name test --port 8081" >&2
    if [ -t 0 ]; then
      read -r -p "Replace the main install anyway? [y/N] " answer
      [[ "$answer" =~ ^[Yy]$ ]] || exit 1
    else
      exit 1
    fi
  fi
fi
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || { echo "--port: a number from 1024 to 65535." >&2; exit 1; }
IMAGE="$DATA/images/chr.img"

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  qemu-system-x86 qemu-utils iproute2 dnsmasq-base nftables tcpdump arp-scan bridge-utils \
  traceroute iputils-ping bash python3 python3-venv unzip curl ca-certificates \
  iperf3 dnsutils nmap mtr-tiny netcat-openbsd busybox iputils-arping tshark

if [ ! -e /dev/kvm ]; then
  echo "WARNING: /dev/kvm is missing. Routers will run without acceleration and be very slow." >&2
  echo "         On Proxmox, set the VM's CPU type to 'host' to enable nested virtualization." >&2
fi

echo "==> Installing Mikrobox into $PREFIX"
mkdir -p "$PREFIX" "$DATA/images" "$DATA/disks"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$REPO"

if [ -f "$IMAGE" ]; then
  echo "==> CHR image already present at $IMAGE"
elif [ -n "$NAME" ] && [ -f /var/lib/mikrobox/images/chr.img ]; then
  echo "==> Copying the CHR image from the main install"
  cp /var/lib/mikrobox/images/chr.img "$IMAGE.part"
  mv "$IMAGE.part" "$IMAGE"
  cp /var/lib/mikrobox/images/chr.version "$DATA/images/" 2>/dev/null || true
else
  VERSION="${CHR_VERSION:-}"
  if [ -z "$VERSION" ]; then
    VERSION="$(curl -fsSL https://upgrade.mikrotik.com/routeros/NEWESTa7.stable | awk '{print $1}')" || true
  fi
  if [ -z "$VERSION" ]; then
    echo "Could not detect the current RouterOS version. Re-run with CHR_VERSION=7.x.y," >&2
    echo "or place a raw CHR disk image at $IMAGE yourself." >&2
    exit 1
  fi
  echo "==> Downloading CHR $VERSION from MikroTik"
  TMP="$(mktemp -d)"
  curl -fL -o "$TMP/chr.zip" "https://download.mikrotik.com/routeros/$VERSION/chr-$VERSION.img.zip"
  unzip -p "$TMP/chr.zip" > "$IMAGE.part"
  mv "$IMAGE.part" "$IMAGE"
  rm -rf "$TMP"
  echo "$VERSION" > "$DATA/images/chr.version"
fi

echo "==> Installing the systemd service $SERVICE"
ENV_LINE=""
if [ -n "$NAME" ]; then
  mkdir -p /etc/mikrobox
  cat > "$ENVFILE" <<EOF
MIKROBOX_DATA=$DATA
MIKROBOX_RUN=/run/$SERVICE
MIKROBOX_PORT=$PORT
MIKROBOX_TAG=$TAG
MIKROBOX_CLOUD_NET=$CLOUD_NET
MIKROBOX_CLOUD_MODE=nat
MIKROBOX_LABEL=$LABEL
EOF
  ENV_LINE="EnvironmentFile=$ENVFILE"
  echo "$NAME" > "$REMEMBER"
elif [ "$PORT" != "8080" ]; then
  ENV_LINE="Environment=MIKROBOX_PORT=$PORT"
fi
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Mikrobox ${NAME:+($NAME) }lab
After=network-online.target
Wants=network-online.target

[Service]
$ENV_LINE
ExecStart=$PREFIX/venv/bin/python -m mikrobox
Restart=on-failure
RuntimeDirectory=$SERVICE
RuntimeDirectoryPreserve=yes
# Routers keep running when the API restarts.
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE.service"
systemctl restart "$SERVICE.service"  # running routers survive the restart

IP="$(hostname -I | awk '{print $1}')"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/api/auth/status" 2>/dev/null && break
  sleep 1
done
echo
if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/api/auth/status" 2>/dev/null; then
  echo "Mikrobox ${NAME:+($NAME) }is running:  http://$IP:$PORT/"
else
  echo "The service did not answer on port $PORT. See:  journalctl -u $SERVICE -n 50" >&2
fi
echo
if [ -n "$NAME" ]; then
  echo "This install is separate from the main one: it has its own accounts (create one on first"
  echo "visit), labs and images. Its Cloud is $CLOUD_NET, behind this VM. To reach its devices from"
  echo "your PC, add a route:  sudo ip route add $CLOUD_NET via $IP   (Linux/macOS)"
  [[ "$CLOUD_NET" == */16 ]] && echo "                       route add ${CLOUD_NET%/*} mask 255.255.0.0 $IP   (Windows)"
  echo "To update it later, from this folder:  git pull && sudo ./scripts/install.sh"
  echo "To remove it:  sudo ./scripts/uninstall.sh --name $NAME"
else
  echo "The Cloud currently gives lab devices internet through this VM (NAT)."
  echo "To put them directly on your own network instead, see what would change with:"
  echo "  sudo $PREFIX/venv/bin/python -m mikrobox.cloudsetup enable"
fi
