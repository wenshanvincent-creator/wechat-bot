#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} == 0 ]] || { printf '%s\n' 'Run as root.' >&2; exit 1; }
directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
application=$(cd -- "$directory/.." && pwd)
[[ "$application" != *' '* ]] || { printf '%s\n' 'Deployment path must not contain spaces.' >&2; exit 1; }
[[ -f "$directory/.env" ]] || { printf '%s\n' 'Missing private deploy/.env.' >&2; exit 1; }
cat > /etc/systemd/system/whale-admin-executor.service <<EOF
[Unit]
Description=Execute only console-approved Whale host commands
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=$application
ExecStart=/usr/bin/python3 $application/admin_executor.py --url http://127.0.0.1:13332 --secrets $directory/.env
Restart=on-failure
RestartSec=10
UMask=0077
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now whale-admin-executor.service
printf '%s\n' 'Host executor enabled. Queued/unapproved commands are never executed.'
