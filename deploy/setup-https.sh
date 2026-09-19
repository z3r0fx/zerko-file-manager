#!/bin/bash
# Installs Caddy inside WSL and starts it against the Caddyfile next to this
# script. Safe to re-run.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo ""
echo "  Zerko File Manager - HTTPS setup"
echo "  ================================"
echo ""

if ! command -v caddy >/dev/null 2>&1; then
    echo "  Installing Caddy..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y caddy
else
    echo "  Caddy already installed: $(caddy version | head -1)"
fi

CONFIG="Caddyfile"
if grep -q "YOUR-DOMAIN" Caddyfile 2>/dev/null; then
    echo ""
    echo "  [!] Caddyfile still says YOUR-DOMAIN."
    echo ""
    echo "      To get a free hostname:"
    echo "        1. Go to https://www.duckdns.org and sign in"
    echo "        2. Create a subdomain, e.g. your-name"
    echo "        3. Set its IP to your public IP"
    echo "        4. Put your-name.duckdns.org in the Caddyfile"
    echo "           (and your email at the top)"
    echo "        5. Forward port 80 AND 443 on the router to this PC"
    echo ""
    echo "      Falling back to a self-signed certificate on port 9443."
    echo "      Your browser will warn once; that is expected."
    echo ""
    CONFIG="Caddyfile.local"
fi

# Check the things that actually break, before Caddy starts asking Let's
# Encrypt for a certificate it cannot get.
if [ "$CONFIG" = "Caddyfile" ]; then
    DOMAIN=$(grep -oE '^[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}[[:space:]]*\{' Caddyfile \
             | head -1 | sed 's/[[:space:]]*{//')
    if [ -n "$DOMAIN" ] && [ -x ./preflight.sh ]; then
        ./preflight.sh "$DOMAIN"
        echo ""
        read -r -p "  Continue anyway? [y/N] " reply
        case "$reply" in [yY]*) ;; *) echo "  Stopped."; exit 1;; esac
    fi
fi

echo "  Validating $CONFIG..."
caddy validate --config "$CONFIG" || { echo "  [!] Config is not valid - nothing started."; exit 1; }

echo "  Starting Caddy with $CONFIG"
exec caddy run --config "$CONFIG"
