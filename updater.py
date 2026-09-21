"""Self-updating from GitHub releases.

The shape of this matters more than the code. Replacing the files of a
RUNNING Python app is how you get a half-updated install that fails in a way
nobody can debug, so nothing is ever overwritten in place. Instead:

    1. check     ask GitHub what the latest release is
    2. download  fetch the zip, verify it, unpack to .update-staged/
    3. apply     the app exits with code 42; start.sh sees the staged folder,
                 swaps the files while nothing is running, and starts again

start.sh is the supervisor. That means the update happens in the gap between
two runs, which is the only moment it is safe, and the console window the user
is watching never closes.

If the new version fails to start, start.sh puts the backup back.
"""

import hashlib
import json
import os
import re
import shutil
import ssl
import stat
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
VERSION_FILE = APP_DIR / "VERSION"
STAGED_DIR = APP_DIR / ".update-staged"
CONFIG_PATH = Path(os.environ.get("ZERKO_CONFIG", APP_DIR / "zerko.config.json"))

# Where updates come from. Only this repo - never a URL from a response,
# which is how an update channel turns into an attack.
GITHUB_OWNER = os.environ.get("ZERKO_GH_OWNER", "z3r0fx")
GITHUB_REPO = os.environ.get("ZERKO_GH_REPO", "zerko-file-manager")

USER_AGENT = "ZerkoFileManager-Updater"

# The built-in source. Changing it means running code from somewhere else, so
# it is refused unless the person running the server opts in on the machine
# itself (an environment variable), not through the web interface.
DEFAULT_OWNER = GITHUB_OWNER
DEFAULT_REPO = GITHUB_REPO
ALLOW_CUSTOM_SOURCE = os.environ.get("ZERKO_ALLOW_CUSTOM_UPDATE_SOURCE") == "1"
ALLOW_UNVERIFIED = os.environ.get("ZERKO_ALLOW_UNVERIFIED_UPDATES") == "1"
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def current_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cfg(d: dict):
    CONFIG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")


def settings() -> dict:
    c = _cfg()
    return {
        "auto_check": c.get("auto_check_updates", True),
        "auto_apply": c.get("auto_apply_updates", False),
        "owner": c.get("gh_owner") or GITHUB_OWNER,
        "repo": c.get("gh_repo") or GITHUB_REPO,
        "check_minutes": int(c.get("update_check_minutes", 60)),
    }


class SettingsRefused(Exception):
    pass


def save_settings(**kw):
    for key, default in (("owner", DEFAULT_OWNER), ("repo", DEFAULT_REPO)):
        val = kw.get(key)
        if val is None or val == default:
            continue
        if not _NAME_RE.match(str(val)):
            raise SettingsRefused(f"'{val}' is not a valid GitHub {key} name.")
        if not ALLOW_CUSTOM_SOURCE:
            raise SettingsRefused(
                "Changing the update source is turned off. Updates install and "
                "run code, so they only come from the built-in repository. To "
                "use a fork, set ZERKO_ALLOW_CUSTOM_UPDATE_SOURCE=1 on the "
                "machine running Zerko and restart it.")
    c = _cfg()
    for key, cfg_key in (("auto_check", "auto_check_updates"),
                         ("auto_apply", "auto_apply_updates"),
                         ("owner", "gh_owner"), ("repo", "gh_repo"),
                         ("check_minutes", "update_check_minutes")):
        if key in kw and kw[key] is not None:
            c[cfg_key] = kw[key]
    _save_cfg(c)
    return settings()


def _version_tuple(v: str):
    """Compare versions numerically, so 1.10.0 beats 1.9.0."""
    parts = []
    for chunk in str(v).lstrip("vV").split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(candidate: str, than: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(than)


def _get_json(url: str, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_text(url: str, timeout=20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return r.read(1024 * 1024).decode("utf-8", "replace")


def _expected_sha256(assets: list, zip_asset: dict):
    """The SHA-256 the release says its zip should have, or None.

    GitHub records a digest for every asset it stores. A ".sha256" file
    attached beside the zip (or a SHA256SUMS file) is accepted too, for
    releases that predate that.
    """
    digest = str(zip_asset.get("digest") or "")
    if digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].strip().lower()
    zip_name = str(zip_asset.get("name", ""))
    for a in assets:
        n = str(a.get("name", ""))
        if n.lower() in (zip_name.lower() + ".sha256", "sha256sums", "sha256sums.txt"):
            url = a.get("browser_download_url") or ""
            if not url.startswith("https://github.com/"):
                continue
            try:
                for line in _get_text(url).splitlines():
                    m = re.match(r"^([A-Fa-f0-9]{64})(?:\s+\*?(.+))?$", line.strip())
                    if m and (not m.group(2) or m.group(2).strip() == zip_name):
                        return m.group(1).lower()
            except Exception:
                continue
    return None


def check() -> dict:
    """Ask GitHub for the newest release. Never downloads anything."""
    s = settings()
    if not s["owner"]:
        return {"ok": False, "error": "No GitHub repository configured yet.",
                "current": current_version()}

    url = f"https://api.github.com/repos/{s['owner']}/{s['repo']}/releases/latest"
    try:
        data = _get_json(url)
    except Exception as e:
        return {"ok": False, "error": f"Could not reach GitHub: {e}",
                "current": current_version()}

    tag = (data.get("tag_name") or "").lstrip("vV")
    asset = None
    for a in data.get("assets", []):
        if str(a.get("name", "")).lower().endswith(".zip"):
            asset = a
            break

    return {
        "ok": True,
        "current": current_version(),
        "latest": tag,
        "update_available": bool(tag) and is_newer(tag, current_version()),
        "notes": (data.get("body") or "").strip()[:4000],
        "published_at": data.get("published_at"),
        "download_url": (asset or {}).get("browser_download_url"),
        "sha256": _expected_sha256(data.get("assets", []), asset) if asset else None,
        "size": (asset or {}).get("size"),
        "has_asset": asset is not None,
        "checked_at": datetime.utcnow().isoformat(),
    }


# Never overwritten by an update: this is the user's own data and identity.
PROTECTED = {
    "mediamanager.db", "mediamanager.db-wal", "mediamanager.db-shm",
    "media.db", ".env", "zerko.config.json", "venv", ".venv",
    "thumbnails", "proxies", "uploads", "transcriptions",
    ".update-staged", ".update-backup", ".requirements-installed",
    ".setup-done", "server.out.log", "duckdns.conf", "Caddyfile",
}


def download(info: dict = None) -> dict:
    """Fetch the release zip and unpack it into .update-staged/.

    Nothing in the live folder is touched here - this only prepares.
    """
    info = info or check()
    if not info.get("ok"):
        return info
    if not info.get("download_url"):
        return {"ok": False, "error": "That release has no .zip attached."}

    expected = (info.get("sha256") or "").lower() or None
    if not expected and not ALLOW_UNVERIFIED:
        return {"ok": False, "error": (
            "This release has no checksum to verify the download against, so "
            "it was not installed. Attach a .sha256 file to the release, or "
            "set ZERKO_ALLOW_UNVERIFIED_UPDATES=1 to accept it anyway.")}

    url = info["download_url"]
    # Only ever download from GitHub's own domains.
    if not url.startswith(("https://github.com/", "https://objects.githubusercontent.com/")):
        return {"ok": False, "error": "Refusing to download from an unexpected host."}

    tmp = Path(tempfile.mkdtemp(prefix="zerko_update_"))
    zip_path = tmp / "update.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120,
                                    context=ssl.create_default_context()) as r, \
             open(zip_path, "wb") as f:
            got = 0
            h = hashlib.sha256()
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_DOWNLOAD_BYTES:
                    return {"ok": False, "error": "The download is far larger than expected."}
                h.update(chunk)
                f.write(chunk)

        if expected and h.hexdigest() != expected:
            return {"ok": False, "error": (
                "The download does not match the release's checksum, so it was "
                "discarded. Try again; if it keeps happening, do not install it.")}

        if not zipfile.is_zipfile(zip_path):
            return {"ok": False, "error": "The downloaded file is not a zip."}

        unpacked = tmp / "unpacked"
        with zipfile.ZipFile(zip_path) as z:
            # Reject anything that could land outside the folder, or that is a
            # link (a symlink in an archive is how a later file gets written
            # somewhere the archive was never allowed to reach).
            root_resolved = unpacked.resolve()
            for zi in z.infolist():
                name = zi.filename
                p = (unpacked / name).resolve()
                if (name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name)
                        or not p.is_relative_to(root_resolved)):
                    return {"ok": False, "error": f"Unsafe path in archive: {name}"}
                if stat.S_ISLNK(zi.external_attr >> 16):
                    return {"ok": False, "error": f"Archive contains a link: {name}"}
            z.extractall(unpacked)

        # Releases usually wrap everything in one top-level folder.
        entries = [p for p in unpacked.iterdir()]
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else unpacked

        if not (root / "main.py").exists():
            return {"ok": False, "error": "That archive does not look like Zerko."}

        if STAGED_DIR.exists():
            shutil.rmtree(STAGED_DIR, ignore_errors=True)
        shutil.move(str(root), str(STAGED_DIR))

        (STAGED_DIR / ".update-info.json").write_text(json.dumps({
            "from": current_version(),
            "to": info.get("latest"),
            "staged_at": datetime.utcnow().isoformat(),
        }, indent=2), encoding="utf-8")

        return {"ok": True, "staged": True, "version": info.get("latest"),
                "path": str(STAGED_DIR)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def staged_version():
    try:
        d = json.loads((STAGED_DIR / ".update-info.json").read_text(encoding="utf-8"))
        return d.get("to")
    except Exception:
        return None


def has_staged() -> bool:
    return STAGED_DIR.is_dir() and (STAGED_DIR / "main.py").exists()


def clear_staged():
    shutil.rmtree(STAGED_DIR, ignore_errors=True)
