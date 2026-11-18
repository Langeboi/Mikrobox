#!/usr/bin/env bash
# Installs Mikrobox on a Debian or Ubuntu VM: packages, the CHR image, a systemd service.
# Run from the repository root:  sudo ./scripts/install.sh
# Pin a RouterOS version with:   sudo CHR_VERSION=7.x.y ./scripts/install.sh
# Leave out Docker for Linux PCs: sudo WITH_DOCKER=0 ./scripts/install.sh
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run this as root (sudo)." >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/mikrobox
DATA=/var/lib/mikrobox
IMAGE="$DATA/images/chr.img"

# Was Docker already running on this VM itself? Then it is someone's, and stays as it is.
was_active() { systemctl is-active --quiet "$1" 2>/dev/null; }
HOST_DOCKER=0; was_active docker.service && HOST_DOCKER=1
HOST_CONTAINERD=0; was_active containerd.service && HOST_CONTAINERD=1

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  qemu-system-x86 qemu-utils iproute2 dnsmasq-base nftables tcpdump arp-scan bridge-utils \
  traceroute iputils-ping bash python3 python3-venv unzip curl ca-certificates \
  iperf3 dnsutils nmap mtr-tiny netcat-openbsd busybox iputils-arping tshark

echo "==> Bonding for servers"
# Servers bond their NICs inside their own namespace. max_bonds=0 stops the module from
# making a bond0 on the VM itself when it loads.
echo "options bonding max_bonds=0" > /etc/modprobe.d/mikrobox-bonding.conf
echo bonding > /etc/modules-load.d/mikrobox-bonding.conf
modprobe bonding max_bonds=0 2>/dev/null || echo "    Could not load the bonding module; servers can't bond their NICs." >&2

if [ "${WITH_DOCKER:-1}" != 0 ]; then
  echo "==> Installing Docker for Linux servers"
  # Each server that has Docker turned on runs its own engine inside it; the VM
  # itself does not need one. Keep the packages from starting one: Docker on the
  # VM would set the firewall to drop forwarded traffic, which the Cloud needs.
  HOLD=""
  if [ "$HOST_DOCKER" = 0 ] && [ ! -e /usr/sbin/policy-rc.d ]; then
    HOLD=/usr/sbin/policy-rc.d
    printf '#!/bin/sh\nexit 101\n' > "$HOLD"; chmod 755 "$HOLD"
    trap 'rm -f "$HOLD"' EXIT
  fi
  apt-get install -y --no-install-recommends docker.io iptables pigz xz-utils
  apt-get install -y --no-install-recommends docker-compose-v2 2>/dev/null \
    || apt-get install -y --no-install-recommends docker-compose 2>/dev/null \
    || echo "    (Docker Compose isn't packaged here; PCs get Docker without it.)"
  if [ -n "$HOLD" ]; then rm -f "$HOLD"; trap - EXIT; fi
  if [ "$HOST_DOCKER" = 0 ]; then
    systemctl disable --now docker.service docker.socket >/dev/null 2>&1 || true
  else
    echo "    Docker was already running on this VM and is left running." >&2
  fi
  if [ "$HOST_CONTAINERD" = 0 ] && [ "$HOST_DOCKER" = 0 ]; then
    systemctl disable --now containerd.service >/dev/null 2>&1 || true
  fi
fi

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
echo "Links that open lab web pages in your browser use ports 20000-20999."
echo "If this VM has a firewall, let those in too (ufw: sudo ufw allow 20000:20999/tcp)."
echo
echo "The Cloud currently gives lab devices internet through this VM (NAT)."
echo "To put them directly on your own network instead, see what would change with:"
echo "  sudo $PREFIX/venv/bin/python -m mikrobox.cloudsetup enable"
