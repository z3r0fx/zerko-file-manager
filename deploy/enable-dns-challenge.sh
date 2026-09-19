#!/bin/bash
# Switch Caddy to the DNS challenge, and to a high HTTPS port.
#
# Why: Let's Encrypt's normal methods need to connect INTO your network on
# port 80 or 443. Both time out here, which on a residential line almost
# always means the ISP blocks low ports. Port 9600 works, so high ports are
# fine - the block is specifically on 80/443.
#
# So:
#   * the certificate is proved by writing a DNS record at DuckDNS, using the
#     token you already have. Nothing has to connect in. No port 80.
#   * the site is served on 9443, which your ISP lets through.
#
# Run once. Safe to re-run.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

HTTPS_PORT="${HTTPS_PORT:-9443}"

echo ""
echo "  Switching to the DNS challenge (HTTPS on port $HTTPS_PORT)"
echo "  ========================================================="
echo ""

[ -f duckdns.conf ] || { echo "  [!] duckdns.conf missing"; exit 1; }
set -a; . ./duckdns.conf; set +a
[ -n "$DUCKDNS_TOKEN" ] || { echo "  [!] DUCKDNS_TOKEN is empty in duckdns.conf"; exit 1; }
DOMAIN="${DUCKDNS_DOMAIN}.duckdns.org"

# --- 1. a Caddy that knows how to talk to DuckDNS ------------------------
# Stock Caddy has no DNS providers built in. Rather than installing Go and
# using xcaddy, Caddy's own site compiles a binary with the plugin on demand.
if ! /usr/local/bin/caddy-duckdns list-modules 2>/dev/null | grep -q "dns.providers.duckdns"; then
    echo "  Downloading a Caddy build with the DuckDNS plugin..."
    ARCH=amd64; [ "$(uname -m)" = "aarch64" ] && ARCH=arm64
    URL="https://caddyserver.com/api/download?os=linux&arch=${ARCH}&p=github.com/caddy-dns/duckdns"
    if ! curl -fsSL --max-time 180 -o /tmp/caddy-duckdns "$URL"; then
        echo "  [!] Download failed. Check the internet connection and retry."
        exit 1
    fi
    chmod +x /tmp/caddy-duckdns
    sudo mv /tmp/caddy-duckdns /usr/local/bin/caddy-duckdns
    echo "  Installed: $(/usr/local/bin/caddy-duckdns version 2>/dev/null | head -1)"
else
    echo "  DuckDNS-capable Caddy already installed."
fi

if ! /usr/local/bin/caddy-duckdns list-modules 2>/dev/null | grep -q "dns.providers.duckdns"; then
    echo "  [!] That build does not contain the DuckDNS module. Stopping."
    exit 1
fi
echo "  DuckDNS DNS module present."

# --- 2. the config -------------------------------------------------------
[ -f Caddyfile ] && cp Caddyfile "Caddyfile.backup-$(date +%Y%m%d-%H%M%S)"

cat > Caddyfile <<EOF
# Zerko File Manager - HTTPS front door
#
# Certificate is obtained by proving control of the DNS record (DNS-01), not
# by accepting an inbound connection. That is why no port 80 forward is
# needed, and why this keeps renewing even though the ISP blocks low ports.
#
# Served on ${HTTPS_PORT} because 443 does not get through to this line.

{
	# Optional; Let's Encrypt only uses it to warn about failing renewals.
	# email you@example.com

	# No automatic port-80 redirect: that port is unreachable here, and
	# leaving it on makes Caddy fail at startup trying to bind it.
	auto_https disable_redirects
}

${DOMAIN}:${HTTPS_PORT} {
	tls {
		dns duckdns {env.DUCKDNS_TOKEN}
		resolvers 1.1.1.1 8.8.8.8
	}

	encode zstd gzip

	request_body {
		max_size 200GB
	}

	reverse_proxy localhost:9600 {
		# -1 disables buffering, so the live Jobs progress stream actually
		# streams instead of arriving all at once when the job finishes.
		flush_interval -1

		transport http {
			# Multi-gigabyte transfers over a slow link must not be cut off.
			read_timeout 0
			write_timeout 0
			dial_timeout 10s
		}
	}

	log {
		output file access.log {
			roll_size 20MB
			roll_keep 5
		}
	}
}
EOF

echo "  Caddyfile written for ${DOMAIN}:${HTTPS_PORT}"

DUCKDNS_TOKEN="$DUCKDNS_TOKEN" /usr/local/bin/caddy-duckdns validate --config Caddyfile >/dev/null 2>&1 \
    && echo "  Config is valid." \
    || { echo "  [!] Config is not valid:"; DUCKDNS_TOKEN="$DUCKDNS_TOKEN" /usr/local/bin/caddy-duckdns validate --config Caddyfile 2>&1 | tail -10; exit 1; }

echo ""
echo "  Done. Now:"
echo "    1. ./caddy-bg.sh restart"
echo "    2. Forward port ${HTTPS_PORT} on the router to YOUR-PC-IP"
echo "    3. Open https://${DOMAIN}:${HTTPS_PORT}"
echo ""
echo "  The first certificate takes a minute or two (DNS has to propagate)."
echo "  Watch it with:  ./caddy-bg.sh log"
echo ""
