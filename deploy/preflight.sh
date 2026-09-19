#!/bin/bash
# Checks the things that actually stop Let's Encrypt working, BEFORE you spend
# an hour wondering why the certificate never arrives.
#
#   ./preflight.sh your-name.duckdns.org
#
# Run it from the machine that will host the server.

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    echo "  usage: ./preflight.sh your-name.duckdns.org"
    exit 1
fi

pass=0; fail=0
ok()   { echo "  [ OK ]  $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL]  $1"; [ -n "$2" ] && echo "          $2"; fail=$((fail+1)); }
warn() { echo "  [WARN]  $1"; [ -n "$2" ] && echo "          $2"; }

echo ""
echo "  Preflight for $DOMAIN"
echo "  ======================================================"
echo ""

# 1. What the world thinks our address is.
PUBIP=""
for u in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
    PUBIP=$(curl -fsS --max-time 8 "$u" 2>/dev/null | tr -d '[:space:]')
    case "$PUBIP" in [0-9]*.[0-9]*.[0-9]*.[0-9]*) break;; *) PUBIP="";; esac
done
if [ -n "$PUBIP" ]; then ok "public IP is $PUBIP"
else warn "could not determine the public IP" "no internet, or the lookup sites are blocked"; fi

# 2. Does the hostname point at us?
RESOLVED=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1)
[ -z "$RESOLVED" ] && RESOLVED=$(nslookup "$DOMAIN" 2>/dev/null | awk '/^Address: /{print $2}' | tail -1)

if [ -z "$RESOLVED" ]; then
    bad "$DOMAIN does not resolve" "create it at duckdns.org, then wait a minute"
elif [ -n "$PUBIP" ] && [ "$RESOLVED" != "$PUBIP" ]; then
    bad "$DOMAIN points at $RESOLVED, but you are $PUBIP" \
        "update the IP on duckdns.org (your line probably got a new address)"
else
    ok "$DOMAIN resolves to $RESOLVED"
fi

# 3. Is the app actually up?
if curl -fsS --max-time 5 -o /dev/null http://localhost:9600/ 2>/dev/null; then
    ok "Zerko is running on localhost:9600"
else
    bad "nothing answering on localhost:9600" "start the server first"
fi

# 4. Port 80 from the outside. THE common failure: many home ISPs block it,
#    and Let's Encrypt's HTTP challenge needs it.
echo ""
echo "  Checking ports from outside (this needs a moment)..."
check_port() {
    local port="$1" label="$2"
    local body
    body=$(curl -fsS --max-time 20 "https://ports.yougetsignal.com/check-port.php" \
             --data "remoteAddress=$DOMAIN&portNumber=$port" 2>/dev/null)
    if [ -z "$body" ]; then
        warn "could not test port $port from outside" "check manually at portchecker.co"
        return
    fi
    case "$body" in
        *"is open"*)   ok "port $port ($label) is reachable from the internet";;
        *"is closed"*) bad "port $port ($label) is CLOSED from the internet" \
                           "forward it on the router to this PC, and check your ISP allows it";;
        *)             warn "port $port test was inconclusive";;
    esac
}
check_port 80  "Let's Encrypt challenge"
check_port 443 "HTTPS"

echo ""
echo "  ======================================================"
if [ "$fail" -eq 0 ]; then
    echo "  All clear. Run:  ./setup-https.sh"
else
    echo "  $fail problem(s) to fix first - see above."
    echo ""
    echo "  If port 80 is blocked by your ISP (common on home lines),"
    echo "  Let's Encrypt's normal method cannot work. Tell Claude and"
    echo "  it will switch you to the DNS method instead, which needs"
    echo "  no open port 80 at all."
fi
echo ""
