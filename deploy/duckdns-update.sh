#!/bin/bash
# Keeps your DuckDNS hostname pointing at this connection.
#
# Home lines get a new IP whenever the router reboots or the ISP feels like it.
# When that happens the hostname still points at the OLD address, the site
# stops answering, and the certificate cannot renew either. This fixes that.
#
# Reads deploy/duckdns.conf:
#     DUCKDNS_DOMAIN=your-name
#     DUCKDNS_TOKEN=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#
# The token is a password for your DNS. Keep this file to yourself.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/duckdns.conf"

if [ ! -f "$CONF" ]; then
    echo "  [!] $CONF is missing."
    echo "      Create it with two lines:"
    echo "        DUCKDNS_DOMAIN=your-subdomain     (no .duckdns.org)"
    echo "        DUCKDNS_TOKEN=the-token-from-duckdns.org"
    exit 1
fi

set -a; . "$CONF"; set +a

if [ -z "$DUCKDNS_DOMAIN" ] || [ -z "$DUCKDNS_TOKEN" ]; then
    echo "  [!] DUCKDNS_DOMAIN or DUCKDNS_TOKEN is empty in $CONF"
    exit 1
fi

# Leaving ip= empty tells DuckDNS to use the address the request came from,
# which is exactly the address the outside world needs to reach.
RESPONSE=$(curl -fsS --max-time 15 \
    "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=")

STAMP=$(date '+%Y-%m-%d %H:%M:%S')
if [ "$RESPONSE" = "OK" ]; then
    echo "$STAMP  updated ${DUCKDNS_DOMAIN}.duckdns.org" | tee -a "$HERE/duckdns.log"
else
    echo "$STAMP  FAILED (response: ${RESPONSE:-none}) - check the token" | tee -a "$HERE/duckdns.log"
    exit 1
fi
