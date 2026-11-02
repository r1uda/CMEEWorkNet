#!/bin/bash
set -euo pipefail
stage=/home/admin/cmee-upload
test "$(id -u)" = 0
test -f "$stage/code.tar.gz"
test -f "$stage/workbench.sqlite3"
test ! -e /opt/cmee/data/workbench.sqlite3
id cmee >/dev/null 2>&1 || useradd --system --home /opt/cmee --shell /usr/sbin/nologin cmee
install -d -m 0755 /opt/cmee
tar -xzf "$stage/code.tar.gz" -C /opt/cmee
chown -R root:root /opt/cmee/app /opt/cmee/deploy
install -d -o cmee -g cmee -m 0700 /opt/cmee/data /opt/cmee/data/quill /opt/cmee/backups
install -o root -g cmee -m 0640 "$stage/production.env" /opt/cmee/.env
install -o cmee -g cmee -m 0600 "$stage/workbench.sqlite3" /opt/cmee/data/workbench.sqlite3
install -o cmee -g cmee -m 0600 "$stage/member_contacts_pending.json" /opt/cmee/data/member_contacts_pending.json
install -m 0644 /opt/cmee/deploy/cmee.service /etc/systemd/system/cmee.service
install -m 0644 /opt/cmee/deploy/cmee-backup.service /etc/systemd/system/cmee-backup.service
install -m 0644 /opt/cmee/deploy/cmee-backup.timer /etc/systemd/system/cmee-backup.timer
install -d /etc/systemd/system/cmee.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/bin/python3 -m app.server --host 127.0.0.1 --port 8765 --no-worker\n' > /etc/systemd/system/cmee.service.d/staging.conf
systemctl daemon-reload
systemctl enable --now cmee
systemctl enable --now cmee-backup.timer
systemctl start cmee-backup.service
