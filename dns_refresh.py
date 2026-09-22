"""Keep a dynamic DNS hostname pointing at this connection.

This used to be a Windows scheduled task running `wsl -- bash -lc ...` every
fifteen minutes. It worked, but `wsl` opens a console window and takes focus
with it, so every quarter of an hour a black box stole your keystrokes - in
the middle of typing, or of a game. The server is already running all the
time, so it may as well do this itself: one thread, no window, nothing to
schedule.

Reads deploy/duckdns.conf, the same file the shell script used:

    DUCKDNS_DOMAIN=your-subdomain      (no .duckdns.org)
    DUCKDNS_TOKEN=the-token

No file, or no values in it, means dynamic DNS is simply not in use and this
does nothing at all.
"""

import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONF = APP_DIR / "deploy" / "duckdns.conf"
LOG = APP_DIR / "deploy" / "duckdns.log"

INTERVAL_SECONDS = 15 * 60
TIMEOUT = 20


def _read_conf() -> dict:
    """Parse the KEY=value file. Tolerates comments, blank lines and quotes -
    it is edited by hand, so it will not always be tidy."""
    out = {}
    try:
        for line in CONF.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return out


def _log(message: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}  {message}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(f"[dns] {line}", flush=True)


def refresh_once() -> tuple:
    """(ok, message). Leaving ip= empty tells DuckDNS to use the address the
    request arrived from, which is the one the outside world has to reach."""
    conf = _read_conf()
    domain = conf.get("DUCKDNS_DOMAIN") or os.environ.get("DUCKDNS_DOMAIN")
    token = conf.get("DUCKDNS_TOKEN") or os.environ.get("DUCKDNS_TOKEN")
    if not domain or not token:
        return False, "not configured"

    url = ("https://www.duckdns.org/update?"
           + urllib.parse.urlencode({"domains": domain, "token": token, "ip": ""}))
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        return False, f"could not reach DuckDNS: {e}"

    if body == "OK":
        return True, f"updated {domain}.duckdns.org"
    # KO is DuckDNS for "that domain and token do not go together".
    return False, f"refused (response: {body or 'none'}) - check the token"


def _loop():
    # A first pass shortly after boot: the address may well have changed while
    # the machine was off, and that is exactly when the site is unreachable.
    time.sleep(20)
    while True:
        ok, msg = refresh_once()
        if msg != "not configured":
            _log(msg if ok else f"FAILED - {msg}")
        time.sleep(INTERVAL_SECONDS)


def start_scheduler():
    if not _read_conf().get("DUCKDNS_DOMAIN") and not os.environ.get("DUCKDNS_DOMAIN"):
        return False
    t = threading.Thread(target=_loop, name="dns-refresh", daemon=True)
    t.start()
    return True
