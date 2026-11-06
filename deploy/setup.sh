#!/usr/bin/env bash
# Installs or updates HackerGit on an Ubuntu server. Safe to run again on every deploy.
# Run as root:  DOMAIN=hackergit.org bash setup.sh /path/to/unpacked/repo
# An optional GitHub token (for stars) can be passed as the first line on stdin.
set -euo pipefail

SRC=${1:?usage: setup.sh <source dir>}
DOMAIN=${DOMAIN:-hackergit.org}
TOKEN=""
if [ ! -t 0 ]; then read -r TOKEN || true; fi
APT="apt-get -y -q -o DPkg::Lock::Timeout=600"
export DEBIAN_FRONTEND=noninteractive

# A fresh server may still be finishing its first boot.
cloud-init status --wait >/dev/null 2>&1 || true

# Packages: python3 (already on Ubuntu) and Caddy for HTTPS.
if ! command -v caddy >/dev/null; then
  $APT update
  $APT install curl gnupg debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  $APT update
  $APT install caddy
fi
command -v python3 >/dev/null || { $APT update; $APT install python3; }

# 1 GB of swap so a small server never runs out of memory during index rebuilds.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# App user, code and data folders.
id hackergit >/dev/null 2>&1 || useradd --system --home-dir /var/lib/hackergit --shell /usr/sbin/nologin hackergit
install -d -o hackergit -g hackergit /var/lib/hackergit
rm -rf /opt/hackergit.new && install -d /opt/hackergit.new
cp -r "$SRC/hackergit.py" "$SRC/server.py" "$SRC/static" /opt/hackergit.new/
rm -rf /opt/hackergit.old && { [ -d /opt/hackergit ] && mv /opt/hackergit /opt/hackergit.old || true; }
mv /opt/hackergit.new /opt/hackergit

# Optional GitHub token for stars. Kept from earlier deploys if none is passed.
touch /etc/hackergit.env && chmod 600 /etc/hackergit.env
if [ -n "$TOKEN" ]; then printf 'GITHUB_TOKEN=%s\n' "$TOKEN" > /etc/hackergit.env; fi

install -m 644 "$SRC/deploy/hackergit.service" /etc/systemd/system/hackergit.service
sed "s/__DOMAIN__/$DOMAIN/g" "$SRC/deploy/Caddyfile" > /etc/caddy/Caddyfile.new
caddy validate --config /etc/caddy/Caddyfile.new --adapter caddyfile
mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --quiet hackergit caddy
systemctl restart hackergit
systemctl reload caddy 2>/dev/null || systemctl restart caddy

for _ in $(seq 1 120); do
  if curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    echo "HackerGit is running."
    exit 0
  fi
  sleep 1
done
echo "HackerGit did not start. Recent logs:" >&2
journalctl -u hackergit -n 50 --no-pager >&2
exit 1
