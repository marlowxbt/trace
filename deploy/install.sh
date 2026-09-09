#!/usr/bin/env bash
# Put TRACE on a fresh Ubuntu or Debian box. Safe to run twice.
#
#   curl -fsSL https://raw.githubusercontent.com/marlowxbt/trace/main/deploy/install.sh | sudo bash
#
# or, having cloned it already:  sudo bash deploy/install.sh
#
# It does not ask for your X key and never prints one. The last step tells you
# which file to put it in.
set -euo pipefail

REPO=${REPO:-https://github.com/marlowxbt/trace.git}
ROOT=/opt/trace
USER_NAME=trace

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this with sudo"; exit 1; }

say "1/7  packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 ufw >/dev/null

say "2/7  user and directories"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$ROOT" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$ROOT/data"
if [ -d "$ROOT/app/.git" ]; then
  git -C "$ROOT/app" pull --ff-only
else
  git clone --depth 1 "$REPO" "$ROOT/app"
fi

say "3/7  python environment"
[ -d "$ROOT/venv" ] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install -q --upgrade pip
"$ROOT/venv/bin/pip" install -q -r "$ROOT/app/requirements.txt"

say "4/7  config"
if [ ! -f "$ROOT/config.toml" ]; then
  cp "$ROOT/app/config.example.toml" "$ROOT/config.toml"
  # the database belongs beside the service, not inside the checkout, so that
  # `git pull` can never touch it
  sed -i 's|^db_path.*|db_path = "/opt/trace/data/trace.db"|' "$ROOT/config.toml"
fi
if [ ! -f /etc/trace.env ]; then
  cat > /etc/trace.env <<'ENVEOF'
# The twitterapi.io key. This file is the only place it lives on this machine.
# Nothing reads it but systemd, and it is not in the repository.
TRACE_X_BEARER=put-your-key-here
ENVEOF
fi
chmod 600 /etc/trace.env
chown root:root /etc/trace.env
chown -R "$USER_NAME:$USER_NAME" "$ROOT"

say "5/7  services"
cp "$ROOT/app/deploy/trace-collector.service" /etc/systemd/system/
cp "$ROOT/app/deploy/trace-desk.service"      /etc/systemd/system/
cp "$ROOT/app/deploy/trace-backup.service"    /etc/systemd/system/
cp "$ROOT/app/deploy/trace-backup.timer"      /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trace-desk trace-backup.timer >/dev/null

say "6/7  firewall"
# Nothing here should be reachable from outside. The desk is on loopback and
# stays there; ssh is the only way in.
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

say "7/7  done - one thing left"
cat <<TXT

The collector is installed but NOT started, because it would spend money with
the placeholder key. Two commands:

  sudo nano /etc/trace.env          # replace put-your-key-here
  sudo systemctl enable --now trace-collector

Then watch it:

  journalctl -u trace-collector -f

And to see the desk from your laptop, on your laptop:

  ssh -N -L 8080:127.0.0.1:8080 root@$(hostname -I 2>/dev/null | awk '{print $1}')
  # then open http://127.0.0.1:8080

TXT
