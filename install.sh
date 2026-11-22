#!/bin/sh
# Installs ev9-autolock on Debian/Ubuntu (systemd). Run as root from the repo folder: sudo ./install.sh
# Safe to re-run: keeps an existing config, secrets and state.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo ./install.sh)"; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "needs Python 3.12+"; exit 1; }
"$PY" -c 'import venv, ensurepip' 2>/dev/null || { echo "install the venv module first, e.g.: apt install python3-venv"; exit 1; }

id ev9lock >/dev/null 2>&1 || useradd --system --home /var/lib/ev9-autolock --no-create-home --shell /usr/sbin/nologin ev9lock

install -d -m 0755 /opt/ev9-autolock
if [ "$SRC" != /opt/ev9-autolock ]; then
  cp -r "$SRC/src" "$SRC/bin" "$SRC/deploy" "$SRC/requirements.lock" "$SRC/README.md" "$SRC/LICENSE" /opt/ev9-autolock/
fi
[ -x /opt/ev9-autolock/venv/bin/python ] || "$PY" -m venv /opt/ev9-autolock/venv
/opt/ev9-autolock/venv/bin/pip install -q -r /opt/ev9-autolock/requirements.lock
chown -R root:root /opt/ev9-autolock && chmod -R go-w /opt/ev9-autolock && chmod 755 /opt/ev9-autolock/bin/ev9-autolock

install -d -m 0750 -o root -g ev9lock /etc/ev9-autolock
[ -f /etc/ev9-autolock/config.toml ] || install -m 0640 -o root -g ev9lock /opt/ev9-autolock/deploy/config.toml /etc/ev9-autolock/config.toml
if [ ! -f /etc/ev9-autolock/secrets.env ]; then
  ( umask 077; printf 'KIA_USERNAME=\nKIA_PASSWORD=\nNOTIFY_WEBHOOK_URL=\n' > /etc/ev9-autolock/secrets.env )
  NEW_SECRETS=1
fi
chown root:root /etc/ev9-autolock/secrets.env && chmod 600 /etc/ev9-autolock/secrets.env
install -d -m 0700 -o ev9lock -g ev9lock /var/lib/ev9-autolock
ln -sf /opt/ev9-autolock/bin/ev9-autolock /usr/local/bin/ev9-autolock
install -m 0644 /opt/ev9-autolock/deploy/ev9-autolock.service /etc/systemd/system/ev9-autolock.service
systemctl daemon-reload

cat <<MSG
Installed (not started). Next:
  1. sudo nano /etc/ev9-autolock/secrets.env      # your Kia Connect email + password${NEW_SECRETS:+  (new file)}
  2. sudo ev9-autolock setup-otp                  # one-time code by email/SMS
  3. sudo ev9-autolock status                     # compare with the Kia app
  4. sudo systemctl enable --now ev9-autolock     # starts in dry-run (logs would_lock only)
  5. after a day or two of correct would_lock events: set dry_run = false in
     /etc/ev9-autolock/config.toml and sudo systemctl restart ev9-autolock
MSG
