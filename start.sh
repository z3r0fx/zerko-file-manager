#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

echo "Starting Zerko File Manager..."
echo "================================"
echo ""
echo "Installation check..."

# Check if we're in the right directory
if [ ! -f "main.py" ]; then
    echo "Error: main.py not found. Please run this script from the mediamanager directory."
    exit 1
fi

# Check python3 is available
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Please install Python 3."
    exit 1
fi

# Check if virtual environment exists, if not create one
if [ ! -d "venv" ]; then
    echo "No virtual environment found. Creating one with python3..."
    # Use --without-pip because ensurepip may not be available (e.g. python3.14 on Debian)
    python3 -m venv venv --without-pip
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create virtual environment."
        exit 1
    fi
fi

# Ensure pip is available inside the venv
if [ ! -f "venv/bin/pip" ]; then
    echo "Installing pip into venv..."
    # Download get-pip.py once and cache it in /tmp
    GET_PIP="/tmp/get-pip.py"
    if [ ! -f "$GET_PIP" ]; then
        curl -sS https://bootstrap.pypa.io/get-pip.py -o "$GET_PIP"
        if [ $? -ne 0 ]; then
            echo "Error: Failed to download get-pip.py. Check internet connection."
            exit 1
        fi
    fi
    venv/bin/python "$GET_PIP"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to install pip into venv."
        exit 1
    fi
fi

# Dependencies. Checking for one import only answers "has pip ever run here",
# not "are the CURRENT requirements installed" - so an update that adds a
# package started fine and then failed at the first import of it. Hashing the
# file catches exactly that: the hash changes, the install runs again.
REQ_STAMP=".requirements-installed"
REQ_HASH="$(sha256sum requirements.txt 2>/dev/null | cut -d' ' -f1)"
if ! venv/bin/python -c "import fastapi" 2>/dev/null \
   || [ "$(cat "$REQ_STAMP" 2>/dev/null)" != "$REQ_HASH" ]; then
    echo "Installing dependencies..."
    venv/bin/pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "Error: Failed to install dependencies."
        exit 1
    fi
    echo "$REQ_HASH" > "$REQ_STAMP"
fi

# Load secrets (SECRET_KEY etc). Without this the app falls back to the
# placeholder key baked into auth.py, which is public in the upstream project -
# anyone who knows it can forge an admin token without a password.
if [ ! -f .env ]; then
    echo "SECRET_KEY=$(venv/bin/python -c 'import secrets;print(secrets.token_hex(32))')" > .env
    chmod 600 .env
    echo "  Generated a unique security key for this installation."
fi
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

if [ -z "$SECRET_KEY" ] || [ "${SECRET_KEY#your-super-secret}" != "$SECRET_KEY" ]; then
    echo ""
    echo "  [!] SECRET_KEY is missing or still the default."
    echo "      Anyone who knows the default can sign in as admin."
    echo ""
fi

PORT="${PORT:-9600}"
# Where the footage lives is chosen in the setup wizard and saved to
# zerko.config.json. Nothing is assumed before that.
if [ -z "$MEDIA_ROOT" ] && [ -f zerko.config.json ]; then
    MEDIA_ROOT=$(venv/bin/python -c "import json;print(json.load(open('zerko.config.json')).get('media_root',''))" 2>/dev/null)
fi
MEDIA_ROOT="${MEDIA_ROOT:-$HOME}"

# ffmpeg/ffprobe are required for durations, thumbnails and proxy generation
if ! command -v ffprobe &>/dev/null; then
    echo ""
    echo "  [!] ffmpeg is missing - thumbnails, durations and proxies need it."
    echo "      Installing it now (your WSL password may be asked for)..."
    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg \
        && echo "      ffmpeg installed." \
        || echo "      [!] Could not install ffmpeg. Run: sudo apt install ffmpeg"
    echo ""
fi

if [ ! -d "$MEDIA_ROOT" ]; then
    echo ""
    echo "  [!] Media folder not found: $MEDIA_ROOT"
    echo "      Check the drive is mounted, or set MEDIA_ROOT to the right path."
    echo ""
fi

# An update staged before the app was closed is applied now, at the one
# moment nothing is running.
if [ -d .update-staged ] && [ -f .update-staged/main.py ] && [ -x ./apply_update.sh ]; then
    ./apply_update.sh
fi

LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo "  ============================================"
echo "   Zerko File Manager is starting"
echo "  ============================================"
echo ""
echo "   On this PC      :  http://localhost:$PORT"
if [ -n "$LAN_IP" ]; then
    echo "   On your network :  http://$LAN_IP:$PORT"
fi
echo ""
echo "   Media folder    :  $MEDIA_ROOT"
echo ""
echo "   Login           :  admin (change this under Change password)"
echo ""
echo "   Press Ctrl+C in this window to stop the server."
echo "  ============================================"
echo ""

# Start the application using the venv python
# Supervisor loop.
#
# The app exits with code 42 when an update has been downloaded and is ready.
# Applying it HERE - between two runs, with nothing running - is the only
# moment it is safe to replace the files of a Python program. The console
# window the user is watching stays open the whole time.
while true; do
    PORT="$PORT" MEDIA_ROOT="$MEDIA_ROOT" SECRET_KEY="$SECRET_KEY" venv/bin/python main.py
    EXIT_CODE=$?

    if [ "$EXIT_CODE" = "42" ] && [ -f ./apply_update.sh ]; then
        ./apply_update.sh
        echo "  Restarting..."
        echo ""
        sleep 2
        continue
    fi

    break
done
exit $EXIT_CODE
