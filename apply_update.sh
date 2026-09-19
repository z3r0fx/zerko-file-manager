#!/bin/bash
# Swap in a staged update. Only ever run BETWEEN app runs, never while the
# server is live - see start.sh, which calls this when the app exits with 42.
#
# Order matters: back up first, then replace, then verify. If the new version
# cannot even import, the backup goes straight back so the user is never left
# with a broken install.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

STAGED="$HERE/.update-staged"
BACKUP="$HERE/.update-backup"

[ -d "$STAGED" ] && [ -f "$STAGED/main.py" ] || exit 0

NEW_VER=$(cat "$STAGED/VERSION" 2>/dev/null | tr -d '[:space:]')
OLD_VER=$(cat "$HERE/VERSION" 2>/dev/null | tr -d '[:space:]')

echo ""
echo "  ================================================"
echo "   Updating Zerko:  ${OLD_VER:-?}  ->  ${NEW_VER:-?}"
echo "  ================================================"
echo ""

# Never replaced - the user's library, settings and identity.
PROTECTED="mediamanager.db mediamanager.db-wal mediamanager.db-shm media.db
           .env zerko.config.json venv .venv thumbnails proxies uploads
           transcriptions .update-staged .update-backup .setup-done
           .requirements-installed server.out.log duckdns.conf Caddyfile
           duckdns.log caddy.log access.log"

is_protected() {
    for p in $PROTECTED; do [ "$1" = "$p" ] && return 0; done
    return 1
}

echo "  Backing up the current version..."
rm -rf "$BACKUP"
mkdir -p "$BACKUP"
for item in "$HERE"/* "$HERE"/.[!.]*; do
    [ -e "$item" ] || continue
    name=$(basename "$item")
    is_protected "$name" && continue
    cp -a "$item" "$BACKUP/" 2>/dev/null
done

echo "  Installing the new files..."
for item in "$STAGED"/* "$STAGED"/.[!.]*; do
    [ -e "$item" ] || continue
    name=$(basename "$item")
    is_protected "$name" && continue
    [ "$name" = ".update-info.json" ] && continue
    rm -rf "$HERE/${name:?}"
    cp -a "$item" "$HERE/" 2>/dev/null
done

chmod +x "$HERE"/*.sh 2>/dev/null
[ -d "$HERE/deploy" ] && chmod +x "$HERE"/deploy/*.sh 2>/dev/null

# Dependencies may have changed with the new version.
if [ -x venv/bin/pip ]; then
    NEW_HASH=$(sha256sum requirements.txt 2>/dev/null | cut -d" " -f1)
    if [ "$(cat .requirements-installed 2>/dev/null)" != "$NEW_HASH" ]; then
        echo "  Updating dependencies..."
        venv/bin/pip install -r requirements.txt --quiet \
            && echo "$NEW_HASH" > .requirements-installed
    fi
fi

# Does the new version actually load? A syntax error or a missing dependency
# would otherwise be discovered by the user, as a crash, with no way back.
echo "  Checking the new version starts..."
if [ -x venv/bin/python ] && ! venv/bin/python -c "import main" >/dev/null 2>&1; then
    echo ""
    echo "  [!] The update does not start. Putting the previous version back."
    for item in "$BACKUP"/* "$BACKUP"/.[!.]*; do
        [ -e "$item" ] || continue
        name=$(basename "$item")
        is_protected "$name" && continue
        rm -rf "$HERE/${name:?}"
        cp -a "$item" "$HERE/" 2>/dev/null
    done
    rm -rf "$STAGED"
    echo "  Rolled back to ${OLD_VER:-the previous version}. Nothing was lost."
    echo ""
    exit 1
fi

rm -rf "$STAGED"
echo ""
echo "  Updated to ${NEW_VER:-the new version}."
echo "  The previous version is kept in .update-backup for now."
echo ""
exit 0
