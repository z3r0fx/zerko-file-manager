#!/bin/bash
# Start / stop / check Caddy without needing a window left open.
#
# The foreground version dies the moment its console is closed, which is how
# HTTPS silently stopped. This detaches it and writes everything to caddy.log,
# so there is a record to look at when something goes wrong.
#
#   ./caddy-bg.sh start|stop|status|log

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1
LOG="$HERE/caddy.log"
PIDF="$HERE/caddy.pid"

CONFIG="Caddyfile"
grep -q "YOUR-DOMAIN" Caddyfile 2>/dev/null && CONFIG="Caddyfile.local"

# Prefer the build that has the DuckDNS plugin, if enable-dns-challenge.sh
# has been run. Stock Caddy cannot read a `dns duckdns` directive at all.
CADDY_BIN="caddy"
[ -x /usr/local/bin/caddy-duckdns ] && CADDY_BIN=/usr/local/bin/caddy-duckdns

# The Caddyfile refers to {env.DUCKDNS_TOKEN}; without this it resolves to an
# empty string and the DNS challenge fails with a confusing auth error.
[ -f "$HERE/duckdns.conf" ] && { set -a; . "$HERE/duckdns.conf"; set +a; }
export DUCKDNS_TOKEN

running() {
    [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null && return 0
    pgrep -f 'caddy(-duckdns)? run' >/dev/null 2>&1
}

case "${1:-status}" in
  start)
    if running; then echo "  Caddy is already running (pid $(cat "$PIDF" 2>/dev/null || pgrep -x caddy))"; exit 0; fi
    command -v "$CADDY_BIN" >/dev/null 2>&1 || { echo "  [!] Caddy is not installed. Run Setup HTTPS.bat first."; exit 1; }

    echo "  Validating $CONFIG..."
    "$CADDY_BIN" validate --config "$CONFIG" >/dev/null 2>&1 || {
        echo "  [!] $CONFIG is not valid:"; "$CADDY_BIN" validate --config "$CONFIG" 2>&1 | tail -5; exit 1; }

    # Ports 80/443 are privileged, so this needs root.
    #
    # Authenticate FIRST, in the foreground. Backgrounding sudo itself
    # (`sudo ... &`) puts the password prompt on a process that has no
    # terminal to read from, so it asks repeatedly and then fails - which is
    # exactly what happened the first time this script ran.
    if ! sudo -n true 2>/dev/null; then
        echo "  Your WSL password is needed to use ports 80 and 443:"
        sudo -v || { echo "  [!] Could not authenticate."; exit 1; }
    fi

    echo "  Starting Caddy in the background..."
    # sudo's credentials are cached now, so this inherits them without asking.
    # setsid detaches it from this console, so closing the window leaves it up.
    # `sudo -E` is refused on many systems ("preserving the entire environment
    # is not supported"), which silently drops DUCKDNS_TOKEN and makes the DNS
    # challenge fail with an unhelpful auth error. Caddy's own --envfile reads
    # the KEY=VALUE file directly as root, so the token never has to survive
    # sudo - and never appears in `ps` output either.
    ENVARG=""
    [ -f "$HERE/duckdns.conf" ] && ENVARG="--envfile $HERE/duckdns.conf"
    sudo setsid "$CADDY_BIN" run --config "$HERE/$CONFIG" $ENVARG >"$LOG" 2>&1 < /dev/null &
    disown 2>/dev/null || true

    # Give it a moment to either bind its ports or fail loudly.
    for _ in 1 2 3 4 5 6 7 8; do
        sleep 1
        pgrep -f 'caddy(-duckdns)? run' >/dev/null 2>&1 && break
    done
    pgrep -f 'caddy(-duckdns)? run' | head -1 > "$PIDF" 2>/dev/null

    if running; then
        echo "  Caddy is running. You can close this window."
        echo "  Log: $LOG"
    else
        echo "  [!] Caddy did not stay up. Last lines:"
        tail -20 "$LOG" 2>/dev/null
        exit 1
    fi
    ;;

  restart)
    "$0" stop
    sleep 2
    "$0" start
    ;;

  stop)
    sudo pkill -f 'caddy(-duckdns)? run' 2>/dev/null && echo "  Caddy stopped." || echo "  Caddy was not running."
    rm -f "$PIDF"
    ;;

  status)
    if running; then
        echo "  Caddy: RUNNING (pid $(pgrep -f 'caddy(-duckdns)? run' | head -1))  binary: $CADDY_BIN"
    else
        echo "  Caddy: NOT RUNNING"
    fi
    echo ""
    if [ -f "$LOG" ]; then
        if grep -q "certificate obtained successfully" "$LOG" 2>/dev/null; then
            echo "  Certificate: OBTAINED"
            grep -o '"identifier":"[^"]*"' "$LOG" | tail -1
        elif grep -q "could not get certificate" "$LOG" 2>/dev/null; then
            echo "  Certificate: FAILED - last error:"
            grep "could not get certificate" "$LOG" | tail -1 | cut -c1-300
        else
            echo "  Certificate: still working on it (check ./caddy-bg.sh log)"
        fi
    fi
    ;;

  log)
    tail -n "${2:-40}" "$LOG" 2>/dev/null || echo "  no log yet"
    ;;

  *)
    echo "  usage: ./caddy-bg.sh start|stop|status|log"
    ;;
esac
