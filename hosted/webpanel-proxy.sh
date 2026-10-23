#!/bin/bash
# mcsm-webpanel – richtet das psychohorror-Webpanel eines gehosteten Modpack-Servers als
# HTTPS-Reverse-Proxy ein: eigene Subdomain, eigenes Let's-Encrypt-Zertifikat, nginx terminiert
# TLS und reicht intern an 127.0.0.1:<Port> weiter. Der Webpanel-Port bleibt so von außen zu;
# erreichbar ist nur https://<subdomain>.<domain>.
#
# Wird ausschließlich vom mcsm-Daemon über sudo aufgerufen (eine eng begrenzte sudoers-Regel
# erlaubt mcsm genau dieses Skript als root). Das Skript prüft seine Eingaben selbst – die Marke
# und der Port kommen aus dem Daemon, werden hier aber nie blind übernommen.
#
#   mcsm-webpanel set <subdomain> <port>
#   mcsm-webpanel remove <subdomain>
set -euo pipefail

DOMAIN_BASE="${MCSM_DOMAIN:-arcardia-nexus.de}"
SITES="/etc/nginx/sites-enabled"
WEBROOT="/var/www/html"

action="${1:-}"
sub="${2:-}"
port="${3:-}"

# Subdomain streng prüfen (ein einzelnes DNS-Label, Kleinbuchstaben/Ziffern/Bindestrich).
if ! [[ "$sub" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  echo "mcsm-webpanel: ungueltige Subdomain: '$sub'" >&2
  exit 2
fi
full="$sub.$DOMAIN_BASE"
conf="$SITES/webpanel-$sub.conf"

reload_nginx() {
  if ! nginx -t 2>/tmp/mcsm-webpanel-nginx.err; then
    cat /tmp/mcsm-webpanel-nginx.err >&2
    return 1
  fi
  systemctl reload nginx
}

write_acme_vhost() {
  cat > "$conf" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $full;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 404; }
}
EOF
}

write_full_vhost() {
  cat > "$conf" <<EOF
# Automatisch von mcsm-webpanel erzeugt – psychohorror-Webpanel fuer $full
server {
    listen 80;
    listen [::]:80;
    server_name $full;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $full;

    ssl_certificate     /etc/letsencrypt/live/$full/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$full/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    location / {
        proxy_pass http://127.0.0.1:$port;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
    }
}
EOF
}

case "$action" in
  set)
    if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1024 || port > 65535 )); then
      echo "mcsm-webpanel: ungueltiger Port: '$port'" >&2
      exit 2
    fi
    if [[ ! -f "/etc/letsencrypt/live/$full/fullchain.pem" ]]; then
      # Erst nur den :80-vhost fuer die ACME-Challenge, dann das Zertifikat holen.
      write_acme_vhost
      reload_nginx
      certbot certonly --webroot -w "$WEBROOT" -d "$full" \
        --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring
    fi
    write_full_vhost
    reload_nginx
    echo "mcsm-webpanel: https://$full -> 127.0.0.1:$port"
    ;;
  remove)
    rm -f "$conf"
    reload_nginx
    echo "mcsm-webpanel: $full entfernt"
    ;;
  *)
    echo "Aufruf: mcsm-webpanel set <subdomain> <port> | remove <subdomain>" >&2
    exit 2
    ;;
esac
