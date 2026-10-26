#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} == 0 ]] || { printf '%s\n' 'Run as root.' >&2; exit 1; }
ip=${1:?Usage: setup-ip-https.sh PUBLIC_IPV4 CERTIFICATE_CONTACT_EMAIL}
email=${2:?Supply certificate contact email}
python3 - "$ip" "$email" <<'PY'
import ipaddress,sys
address=ipaddress.ip_address(sys.argv[1])
assert address.version==4 and address.is_global,'A real public IPv4 is required'
assert '@' in sys.argv[2] and not any(c.isspace() for c in sys.argv[2]),'Invalid email'
PY
apt-get update
apt-get install -y nginx python3-venv ca-certificates
python3 -m venv /opt/whale-certbot
/opt/whale-certbot/bin/pip install --index-url https://pypi.org/simple 'certbot>=5.4,<6'
install -d -m 755 /var/www/whale-acme
cat > /etc/nginx/conf.d/whale-acme.conf <<EOF
# Managed by wechat-deepseek deployment.
server {
    listen 80;
    server_name $ip;
    location /.well-known/acme-challenge/ { root /var/www/whale-acme; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
nginx -t
systemctl enable --now nginx
systemctl reload nginx
/opt/whale-certbot/bin/certbot certonly --non-interactive --agree-tos --email "$email" \
  --webroot --webroot-path /var/www/whale-acme --ip-address "$ip" --preferred-profile shortlived
directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 - "$directory/nginx-ip-tls.conf.template" "$ip" <<'PY'
from pathlib import Path
import sys
source=Path(sys.argv[1]).read_text()
Path('/etc/nginx/conf.d/whale-agent.conf').write_text(source.replace('__PUBLIC_IP__',sys.argv[2]))
PY
nginx -t
systemctl reload nginx
cat > /etc/systemd/system/whale-cert-renew.service <<'EOF'
[Unit]
Description=Renew Whale IP certificate
After=network-online.target
[Service]
Type=oneshot
ExecStart=/opt/whale-certbot/bin/certbot renew --quiet --deploy-hook "nginx -t && systemctl reload nginx"
EOF
cat > /etc/systemd/system/whale-cert-renew.timer <<'EOF'
[Unit]
Description=Check short-lived IP certificates twice daily
[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now whale-cert-renew.timer
printf 'Console: https://%s/\nWeChat desktop: https://%s:9443/\n' "$ip" "$ip"
