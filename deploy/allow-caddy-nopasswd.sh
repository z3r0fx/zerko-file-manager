#!/bin/bash
# Stops Caddy asking for your WSL password every time it starts.
#
# Ports below 1024 need root, so caddy-bg.sh uses sudo. This adds a rule that
# lets YOUR user run THAT ONE BINARY as root without a password - nothing else.
# It is not a blanket "no password for sudo"; every other command still asks.
#
# Undo with:  sudo rm /etc/sudoers.d/zerko-caddy
set -e
BIN=/usr/local/bin/caddy-duckdns
[ -x "$BIN" ] || BIN=$(command -v caddy)
[ -n "$BIN" ] || { echo "  [!] Caddy not found."; exit 1; }

RULE="$(whoami) ALL=(root) NOPASSWD: $BIN, /usr/bin/setsid $BIN, /usr/bin/pkill -f caddy*"
echo "  Adding rule for: $BIN"
echo "$RULE" | sudo tee /etc/sudoers.d/zerko-caddy >/dev/null
sudo chmod 0440 /etc/sudoers.d/zerko-caddy

# A malformed sudoers file can lock you out of sudo entirely, so validate and
# remove it again if it does not parse.
if sudo visudo -c -f /etc/sudoers.d/zerko-caddy >/dev/null 2>&1; then
    echo "  Done - Caddy will no longer ask for a password."
else
    sudo rm -f /etc/sudoers.d/zerko-caddy
    echo "  [!] The rule did not validate and was removed. No harm done."
    exit 1
fi
