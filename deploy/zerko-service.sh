#!/bin/bash
# Zerko as a service - with an off switch that actually stays off.
#
# The rule: a flag file decides whether Zerko SHOULD be running.
#
#   start    creates the flag, starts everything
#   stop     REMOVES the flag, stops everything
#   watchdog starts things ONLY IF the flag exists
#
# The watchdog runs on a timer, so a crash or a reboot brings things back.
# But because `stop` clears the flag, a deliberate shutdown is respected -
# the watchdog sees no flag and does nothing. Nothing pops back up in the
# middle of a game.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"
FLAG="$HERE/.zerko-enabled"
APPLOG="$APP/server.out.log"
PIDF="$HERE/zerko.pid"

# "A process exists" is NOT the same as "the server works". The live Jobs
# stream (SSE) holds connections open, so uvicorn can sit in "waiting for
# connections to close" indefinitely - process alive, port dead. Checking the
# port is the only answer that means anything.
app_running() {
    (exec 3<>/dev/tcp/127.0.0.1/9600) 2>/dev/null && { exec 3<&-; return 0; }
    return 1
}
app_process_exists() { pgrep -f "[p]ython main\.py$" >/dev/null 2>&1; }
caddy_running() { pgrep -f 'caddy(-duckdns)? run' >/dev/null 2>&1; }

start_app() {
    app_running && { echo "  library: already running"; return; }
    # A process that exists but is not serving is a half-dead server from a
    # previous stop. Clear it out rather than reporting success.
    if app_process_exists; then
        echo "  library: clearing a stuck process from last time"
        pkill -9 -f "[p]ython main\.py$" 2>/dev/null
        sleep 2
    fi
    cd "$APP" || return 1
    # Go through start.sh rather than calling main.py directly: it checks that
    # the venv, the dependencies, ffprobe and the media drive are all actually
    # there. Skipping it means a missing dependency shows up as a silent
    # failure at 3am instead of a clear message.
    setsid ./start.sh >"$APPLOG" 2>&1 < /dev/null &

    # Wait for the PORT, not just for a process to appear. Startup runs
    # migrations and resumes pending jobs, so it can take a few seconds.
    for _ in $(seq 1 25); do
        sleep 1
        app_running && break
    done
    pgrep -f "[p]ython main\.py$" | head -1 > "$PIDF" 2>/dev/null
    if app_running; then
        echo "  library: started"
    else
        echo "  library: FAILED TO START - last lines of the log:"
        tail -12 "$APPLOG" 2>/dev/null | sed 's/^/      /'
    fi
}

# The image model for Looks (ComfyUI, ~/zerko-imagegen) holds the graphics
# card's memory while it runs. Zerko starts it when a look is painted; a full
# stop ends it too, so a game or Resolve gets the whole card.
stop_imagegen() {
    if pgrep -f "zerko-imagegen/ComfyUI|[.]venv/bin/python main\.py --listen 127\.0\.0\.1 --port 8188" >/dev/null 2>&1; then
        pkill -f "[.]venv/bin/python main\.py --listen 127\.0\.0\.1 --port 8188" 2>/dev/null
        echo "  image model: stopped"
    fi
}

stop_app() {
    if ! app_process_exists; then
        echo "  library: was not running"
        rm -f "$PIDF"
        return
    fi
    # Ask nicely first.
    pkill -f "[p]ython main\.py$" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8; do
        app_process_exists || break
        sleep 1
    done
    # Then insist. Without this it hangs forever on the open SSE streams, and
    # the leftover process makes the next start think it is already running.
    if app_process_exists; then
        echo "  library: not stopping cleanly (open connections) - forcing"
        pkill -9 -f "[p]ython main\.py$" 2>/dev/null
        sleep 1
    fi
    app_process_exists && echo "  library: STILL RUNNING" || echo "  library: stopped"
    rm -f "$PIDF"
}

case "${1:-status}" in
  start)
    touch "$FLAG"          # "should be running" from now on
    echo "  Starting Zerko..."
    "$HERE/caddy-bg.sh" start 2>&1 | sed 's/^/  /'
    start_app
    echo ""
    echo "  Local  : http://localhost:9600"
    # Read the hostname from the Caddyfile rather than assuming one.
    PUBHOST=$(grep -oE "^[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}" "$HERE/Caddyfile" 2>/dev/null | head -1)
    [ -n "$PUBHOST" ] && echo "  Public : https://$PUBHOST"
    ;;

  stop)
    # Clearing the flag FIRST matters: if the watchdog fires while we are
    # shutting down, it must already know it is meant to stay off.
    rm -f "$FLAG"
    echo "  Stopping Zerko (it will stay stopped until you start it again)..."
    stop_app
    stop_imagegen
    "$HERE/caddy-bg.sh" stop 2>&1 | sed 's/^/  /'
    echo ""
    echo "  Everything is off. Nothing will restart it on its own."
    ;;

  restart)
    # Just the library, not HTTPS. Caddy is a separate process that proxies to
    # us; bouncing it drops every open connection and, on this machine, wants a
    # sudo password. Nothing in a code update touches it, so leave it alone.
    touch "$FLAG"          # a deliberate restart means "should be running"
    echo "  Restarting the library (HTTPS stays up)..."
    stop_app
    start_app
    echo ""
    echo "  Local  : http://localhost:9600"
    echo "  Now press Ctrl+F5 in the browser to pick up the new front end."
    ;;

  watchdog)
    # Runs on a timer, with nobody watching. Does nothing unless the flag says
    # it should be up.
    [ -f "$FLAG" ] || exit 0

    # A scheduled task has no terminal, so it cannot type a sudo password. If
    # the passwordless rule is not installed, Caddy would hang here forever
    # waiting for input. Say so in the log and start the library anyway -
    # half a service beats a stuck one.
    if ! sudo -n true 2>/dev/null; then
        echo "$(date '+%F %T')  cannot start HTTPS unattended - run allow-caddy-nopasswd.sh" \
            >> "$HERE/watchdog.log"
        app_running || start_app >/dev/null 2>&1
        exit 0
    fi
    caddy_running || "$HERE/caddy-bg.sh" start >/dev/null 2>&1
    app_running   || start_app >/dev/null 2>&1
    # Keep the public address current while we are up.
    "$HERE/duckdns-update.sh" >/dev/null 2>&1
    exit 0
    ;;

  status)
    if [ -f "$FLAG" ]; then echo "  Set to: RUNNING (will restart itself if it stops)"
    else echo "  Set to: STOPPED (nothing will start it automatically)"; fi
    app_running   && echo "  library : running" || echo "  library : not running"
    caddy_running && echo "  https   : running" || echo "  https   : not running"
    ;;

  *)
    echo "  usage: ./zerko-service.sh start|stop|restart|status|watchdog"
    ;;
esac
