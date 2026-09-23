#!/bin/bash
# Installs the 3D tools for fly-throughs on the Windows side (they use the
# NVIDIA card through Windows' own drivers, nothing to compile):
#   COLMAP  - works out where the drone was for every frame
#   Brush   - builds the 3D scene (Gaussian splats) from those frames
# Into C:\Users\<you>\zerko-3d. Safe to run again. Run from WSL as the normal user.
set -o pipefail
step() { echo; echo "=== $*"; }
fail() { echo; echo "FAILED: $*"; exit 1; }

step "Windows folder"
WINHOME=$(cmd.exe /c "echo %USERPROFILE%" 2>/dev/null | tr -d '\r')
[ -n "$WINHOME" ] || fail "cannot ask Windows for the user folder (WSL interop off?)"
ROOT="$(wslpath -u "$WINHOME")/zerko-3d"
mkdir -p "$ROOT" || fail "cannot create $ROOT"
echo "$ROOT"
cd "$ROOT" || exit 1

PY=python3
command -v $PY >/dev/null || PY="$(dirname "$0")/../../venv/bin/python"
unzip_to() { $PY - "$1" "$2" <<'PYZ'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PYZ
}

get() {   # get <url> <file>
  [ -s "$2" ] && { echo "have $2"; return 0; }
  echo "downloading $2"
  curl -L --fail --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2" || fail "download $2"
}

step "COLMAP (camera positions)"
get https://github.com/colmap/colmap/releases/latest/download/colmap-x64-windows-cuda.zip colmap.zip
[ -d colmap ] || { unzip_to colmap.zip colmap || fail "unzip COLMAP"; }
COLMAP=$(find "$ROOT/colmap" -iname 'colmap.exe' | head -1)
[ -n "$COLMAP" ] || COLMAP=$(find "$ROOT/colmap" -iname 'COLMAP.bat' | head -1)
[ -n "$COLMAP" ] || fail "colmap.exe not found in the download"
find "$ROOT/colmap" -iname '*.exe' -exec chmod +x {} +     # unzipped files are not runnable from WSL until marked
echo "$COLMAP"
"$COLMAP" help 2>&1 | head -5 || fail "COLMAP does not start"

step "Brush (3D scene)"
get https://github.com/ArthurBrussee/brush/releases/latest/download/brush-app-x86_64-pc-windows-msvc.zip brush.zip
[ -d brush ] || { unzip_to brush.zip brush || fail "unzip Brush"; }
BRUSH=$(find "$ROOT/brush" -iname '*.exe' | head -1)
[ -n "$BRUSH" ] || fail "brush exe not found in the download"
chmod +x "$BRUSH"
echo "$BRUSH"
"$BRUSH" --help > "$(dirname "$0")/brush_help.log" 2>&1
grep -E "^\s+--(total-steps|export|eval|max-resolution|sh-degree|max-splats)" "$(dirname "$0")/brush_help.log"

step "Settings"
printf '{"root": "%s", "colmap": "%s", "brush": "%s"}\n' "$ROOT" "$COLMAP" "$BRUSH" > "$ROOT/zerko.json"
cat "$ROOT/zerko.json"
echo
echo "DONE. 3D tools are in $WINHOME\\zerko-3d"
