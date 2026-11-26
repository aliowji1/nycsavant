#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu 24.04 server (DigitalOcean, Hetzner, a Raspberry Pi...).
# Run as root:
#   curl -fsSL https://raw.githubusercontent.com/aliowji1/nycsavant/main/ops/setup_server.sh -o setup.sh
#   bash setup.sh
# It asks for your BusTime key once. Safe to re-run.
set -euo pipefail

APP=/opt/nycsavant
REPO=https://github.com/aliowji1/nycsavant.git
USER_NAME=savant

echo "== packages"
# A fresh server installs its own updates for a few minutes after boot and holds
# the package lock meanwhile. Retry until it's free (up to ~20 minutes).
apt_retry() {
  for i in $(seq 1 120); do
    if DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=10 "$@" 2>/tmp/apt.err; then
      return 0
    fi
    if grep -q "lock" /tmp/apt.err; then
      echo "   package lock busy (the server's own updates are running), retrying..."
      sleep 10
    else
      cat /tmp/apt.err >&2
      return 1
    fi
  done
  echo "gave up waiting for the package lock" >&2
  return 1
}
apt_retry update -q
apt_retry install -y -q python3-venv python3-pip git rsync
timedatectl set-timezone America/New_York

echo "== swap (headroom for the nightly job on small servers)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== app user and code"
id "$USER_NAME" >/dev/null 2>&1 || useradd -m -s /bin/bash "$USER_NAME"
if [ ! -d "$APP/.git" ]; then
  git clone -q "$REPO" "$APP"
else
  git -C "$APP" pull -q
fi
mkdir -p "$APP/logs" "$APP/data"
chown -R "$USER_NAME:$USER_NAME" "$APP"

echo "== python environment"
sudo -u "$USER_NAME" python3 -m venv "$APP/.venv"
sudo -u "$USER_NAME" "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== settings"
if [ ! -f "$APP/.env" ] || ! grep -q '^BUSTIME_KEY=.' "$APP/.env"; then
  read -rp "Paste your BusTime API key: " KEY
  printf 'BUSTIME_KEY=%s\nDEPLOY_TARGET=none\n' "$KEY" > "$APP/.env"
  chown "$USER_NAME:$USER_NAME" "$APP/.env"
  chmod 600 "$APP/.env"
fi

echo "== MTA schedules (a few minutes)"
sudo -u "$USER_NAME" bash -c "cd $APP && .venv/bin/python -m savant static"

echo "== collector service (starts now, restarts on crash or reboot)"
cat > /etc/systemd/system/savant-collector.service <<EOF
[Unit]
Description=NYC Savant realtime feed collector
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -m savant collect
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now savant-collector

echo "== nightly + weekly jobs"
crontab -u "$USER_NAME" - <<EOF
CRON_TZ=America/New_York
40 4 * * *  cd $APP && .venv/bin/python -m savant nightly >> logs/nightly.log 2>&1 && ./ops/deploy.sh >> logs/deploy.log 2>&1
10 3 * * 0  cd $APP && .venv/bin/python -m savant static >> logs/static.log 2>&1
EOF

echo
echo "Done. The collector is running:"
systemctl --no-pager --lines=3 status savant-collector | sed -n '1,3p'
echo
echo "Check it any time with:  journalctl -u savant-collector -f"
echo "Snapshots land in:       $APP/data/raw/"
