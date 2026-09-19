#!/bin/bash
# One-time setup inside WSL: system packages, python venv, dependencies.
# Safe to re-run - everything here checks before it acts.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo ""
echo "  Zerko - first run setup"
echo "  ======================="
echo ""

need_apt=0
command -v python3 >/dev/null 2>&1 || need_apt=1
command -v ffmpeg  >/dev/null 2>&1 || need_apt=1
python3 -c "import venv" >/dev/null 2>&1 || need_apt=1

if [ "$need_apt" = "1" ]; then
    echo "  Installing system packages (your WSL password may be asked for)..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-venv python3-pip ffmpeg
fi

echo "  python3 : $(python3 --version 2>&1)"
echo "  ffmpeg  : $(ffmpeg -version 2>/dev/null | head -1 | cut -c1-40)"

if [ ! -x venv/bin/python ]; then
    echo "  Creating the Python environment..."
    python3 -m venv venv
fi

echo "  Installing Zerko's dependencies..."
venv/bin/python -m pip install --upgrade pip --quiet
venv/bin/pip install -r requirements.txt --quiet

# A secret unique to THIS installation. Without it every install would share
# the same signing key, and anyone holding it could forge a login anywhere.
if [ ! -f .env ]; then
    echo "SECRET_KEY=$(venv/bin/python -c 'import secrets;print(secrets.token_hex(32))')" > .env
    chmod 600 .env
    echo "  Generated a unique security key for this installation."
fi

echo ""
echo "  Ready. Zerko will open in your browser in a moment."
echo ""
