#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ -f .env ]] || { printf '%s\n' 'Create deploy/.env privately first.' >&2; exit 1; }
chmod 600 .env
docker info >/dev/null
docker compose --env-file .env -f compose.server.yaml config --quiet
docker compose --env-file .env -f compose.server.yaml --profile setup run --rm initialize
docker compose --env-file .env -f compose.server.yaml up -d desktop console harness worker
printf '%s\n' 'Console: server loopback 127.0.0.1:13332. Desktop: 127.0.0.1:13331.'
printf '%s\n' 'Next: enable IP HTTPS, scan WeChat QR, enroll current account and selected chats in the console. Automatic chat starts disabled.'
