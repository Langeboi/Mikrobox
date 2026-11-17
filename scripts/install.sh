#!/usr/bin/env bash
# Installs Mikrobox on a Debian or Ubuntu VM: packages, the CHR image, a systemd service.
# Run from the repository root:  sudo ./scripts/install.sh
# Pin a RouterOS version with:   sudo CHR_VERSION=7.x.y ./scripts/install.sh
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run this as root (sudo)." >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/mikrobox
DATA=/var/lib/mikrobox
IMAGE="$DATA/images/chr.img"

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  qemu-system-x86 qemu-utils iproute2 dnsmasq-base nftables tcpdump arp-scan bridge-utils \
  traceroute iputils-ping bash python3 python3-venv unzip curl ca-certificates \
  iperf3 dnsutils nmap mtr-tiny netcat-openbsd busybox iputils-arping

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

echo "==> Installing the systemd service"
cat > /etc/systemd/system/mikrobox.service <<EOF
[Unit]
Description=Mikrobox CHR lab
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$PREFIX/venv/bin/python -m mikrobox
Restart=on-failure
RuntimeDirectory=mikrobox
RuntimeDirectoryPreserve=yes
# Routers keep running when the API restarts.
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable mikrobox.service
systemctl restart mikrobox.service  # running routers survive the restart

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Mikrobox is running:  http://$IP:8080/"
echo
echo "The Cloud currently gives lab devices internet through this VM (NAT)."
echo "To put them directly on your own network instead, see what would change with:"
echo "  sudo $PREFIX/venv/bin/python -m mikrobox.cloudsetup enable"
