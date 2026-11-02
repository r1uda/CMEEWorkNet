#!/bin/bash
set -euo pipefail
install -d /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironment=CMEE_DOMAIN=cmee-work.top\n' > /etc/systemd/system/caddy.service.d/domain.conf
install -m 0644 /opt/cmee/deploy/Caddyfile /etc/caddy/Caddyfile
CMEE_DOMAIN=cmee-work.top caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl reload caddy
