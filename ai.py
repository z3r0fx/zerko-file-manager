"""Claude for the AI features: looks, room types, declutter, windows, captions.

Two ways to reach Claude, chosen in Manage > AI:

- An API key (console.anthropic.com), kept only on the server that runs
  Zerko, in ai_settings.json next to the database (git-ignored, never packaged
  in a release) or the ANTHROPIC_API_KEY environment variable.
- Claude Code installed and signed in on the same computer as the server
  ("claude" on the PATH): the requests then run on that person's own Claude
  plan, and no key exists anywhere in Zerko.

Nothing about either is in the code or the web app: the browser only ever
learns whether AI is on.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from database import User

router = APIRouter(prefix="/api/ai", tags=["ai"])

SETTINGS_FILE = Path(__file__).resolve().parent / "ai_settings.json"
# "auto": the least costly model that does each job well - Haiku for quick
# sorting and wording, Sonnet where the photo itself has to be judged
DEFAULT_MODEL = "auto"
TIERS = {"fast": "claude-haiku-4-5-20251001", "smart": "claude-sonnet-5"}
CLI_TIERS = {"fast": "haiku", "smart": "sonnet"}
API = os.environ.get("ZK_AI_API") or "https://api.anthropic.com/v1"   # tests point this at a stand-in
_lock = threading.Lock()


class AiOff(Exception):
    """No key: the feature explains how to turn it on instead of failing."""


def _load() -> dict:
    try:
        d = json.loads(SETTINGS_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict):
    with _lock:
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass


def backend() -> str:
    """"api" or "claude_code". Unset: a key wins; with none, a signed-in
    Claude Code on this computer is used."""
    b = (_load().get("backend") or "").strip()
    if b in ("api", "claude_code"):
        return b
    if _api_key():
        return "api"
    return "claude_code" if cli_path() else "api"


def cli_path() -> str:
    """The claude command, if Claude Code is installed for the user the
    server runs as."""
    p = (_load().get("cli_path") or "").strip()
    if p and os.path.isfile(p):
        return p
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for c in (home / ".local/bin/claude", home / ".npm-global/bin/claude", home / ".claude/local/claude",
              Path("/usr/local/bin/claude"), Path("/usr/bin/claude")):
        if c.is_file():
            return str(c)
    # Zerko in WSL, Claude Code installed on Windows itself (the native
    # installer puts claude.exe in the Windows user's .local\bin)
    import glob
    for c in sorted(glob.glob("/mnt/c/Users/*/.local/bin/claude.exe")):
        if os.path.isfile(c):
            return c
    return ""


def key() -> str:
    return _api_key() if backend() == "api" else ""


def _api_key() -> str:
    k = (_load().get("key") or "").strip()
    if k:
        return k
    k = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if k:
        return k
    # the caption writer kept its own key before this module existed
    try:
        import captions
        return (captions.load_settings().get("ai_key") or "").strip()
    except Exception:
        return ""


def model() -> str:
    """The model chosen in Manage > AI, or "auto"."""
    m = (_load().get("model") or "").strip()
    if backend() == "claude_code":
        return m if m in CLI_MODELS else "auto"
    return m if m and m not in CLI_MODELS else DEFAULT_MODEL


def model_for(tier: str = "smart") -> str:
    # "cheap": small writing jobs (captions) always go to the smallest model, whatever is chosen for the rest
    if tier == "cheap":
        return (CLI_TIERS if backend() == "claude_code" else TIERS)["fast"]
    m = model()
    if m != "auto":
        return m
    return (CLI_TIERS if backend() == "claude_code" else TIERS).get(tier, TIERS["smart"] if backend() == "api" else "sonnet")


CLI_MODELS = ("auto", "sonnet", "opus", "haiku")


def enabled() -> bool:
    return bool(key()) if backend() == "api" else bool(cli_path())


_cli_slots = threading.Semaphore(3)


def _ask_cli(content: list, system: str, tier: str = "smart", timeout: float = 240) -> str:
    """One question through Claude Code, on the plan it is signed in with.
    No tools, no settings files, no MCP servers: just the model."""
    cli = cli_path()
    if not cli:
        raise AiOff("AI is off: Claude Code is not installed for the account the server runs as (see Manage > AI).")
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    args = [cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--tools", "", "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "",
            "--model", model_for(tier), "--system-prompt", system or "You are a helpful assistant inside Zerko, a photo studio app."]
    with _cli_slots, tempfile.TemporaryDirectory(prefix="zk-ai-") as cwd:
        try:
            p = subprocess.run(args, input=json.dumps(msg) + "\n", capture_output=True, text=True, timeout=timeout,
                               cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Claude took too long to answer - try again.")
        except OSError as e:
            raise HTTPException(status_code=502, detail=f"Could not start Claude Code: {e}")
    result = None
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "result":
            result = d
    if not result:
        err = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["no answer"]
        low = " ".join(err).lower()
        if "login" in low or "auth" in low or "log in" in low:
            raise HTTPException(status_code=502, detail="Claude Code is not signed in: run `claude` once in the server's terminal and sign in.")
        raise HTTPException(status_code=502, detail=f"Claude Code did not answer: {err[0][:300]}")
    if result.get("is_error"):
        txt = str(result.get("result") or result.get("subtype") or "error")
        if "limit" in txt.lower():
            raise HTTPException(status_code=429, detail=f"Your Claude plan's usage limit was reached: {txt[:200]}")
        raise HTTPException(status_code=502, detail=f"Claude Code: {txt[:300]}")
    return str(result.get("result") or "")


def _post(path: str, body: dict, timeout: float = 90) -> dict:
    k = key()
    if not k:
        raise AiOff("AI is off: an administrator adds an Anthropic API key in Manage > AI.")
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:400]
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 + attempt * 3)
                last = msg
                continue
            if e.code == 401:
                raise HTTPException(status_code=502, detail="The Anthropic API key was refused - check it in Manage > AI.")
            raise HTTPException(status_code=502, detail=f"Claude answered {e.code}: {msg}")
        except Exception as e:
            last = str(e)
            if attempt < 2:
                time.sleep(2)
                continue
    raise HTTPException(status_code=502, detail=f"Could not reach Claude: {last}")


def jpeg_b64(rgb, long_edge: int = 1280, quality: int = 85) -> str:
    """An RGB array (uint8 or 0..1 float) as a base64 JPEG for Claude to look at."""
    import numpy as np
    from PIL import Image
    a = rgb
    if a.dtype != np.uint8:
        a = (np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8)
    im = Image.fromarray(a)
    s = long_edge / max(im.size)
    if s < 1:
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=quality)
    return base64.b64encode(b.getvalue()).decode()


GRID_NOTE = (" A thin magenta grid is drawn over the picture at every 0.1 of its width (numbers along the top) and height "
             "(numbers down the left): read every coordinate off it as exactly as you can, to 0.01. The grid is not part of the photo.")


def grid_b64(rgb, long_edge: int = 1568, quality: int = 88) -> str:
    """The picture with a labelled 0.1 grid over it, as a base64 JPEG: Claude reads positions off the grid
    far more exactly than it guesses them (its plain boxes were often a tenth of the frame out)."""
    import numpy as np
    from PIL import Image, ImageDraw
    a = rgb
    if a.dtype != np.uint8:
        a = (np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8)
    im = Image.fromarray(a)
    s = long_edge / max(im.size)
    if s < 1:
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    for i in range(1, 10):
        x, y = int(im.width * i / 10), int(im.height * i / 10)
        d.line([(x, 0), (x, im.height)], fill=(255, 0, 255), width=1)
        d.line([(0, y), (im.width, y)], fill=(255, 0, 255), width=1)
        d.text((x + 3, 3), f"{i / 10:.1f}", fill=(255, 0, 255))
        d.text((3, y + 3), f"{i / 10:.1f}", fill=(255, 0, 255))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=quality)
    return base64.b64encode(b.getvalue()).decode()


def ask(prompt: str, images: Optional[List[str]] = None, system: str = "", max_tokens: int = 2000,
        temperature: float = 0.4, tier: str = "smart") -> str:
    """One question, optionally with pictures (base64 JPEGs). Returns the text."""
    content: list = []
    for im in images or []:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": im}})
    content.append({"type": "text", "text": prompt})
    if backend() == "claude_code":
        return _ask_cli(content, system, tier).strip()
    body = {"model": model_for(tier), "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "user", "content": content}]}
    if system:
        body["system"] = system
    d = _post("/messages", body)
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()


def ask_json(prompt: str, images: Optional[List[str]] = None, system: str = "", max_tokens: int = 3000,
             temperature: float = 0.3, tier: str = "smart"):
    """Like ask(), for an answer in JSON; tolerant of prose or code fences around it."""
    txt = ask(prompt + "\n\nAnswer with JSON only, no other text.", images, system, max_tokens, temperature, tier)
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1)
    start = min([i for i in (txt.find("{"), txt.find("[")) if i >= 0] or [0])
    txt = txt[start:]
    for end in range(len(txt), 0, -1):
        if txt[end - 1] in "}]":
            try:
                return json.loads(txt[:end])
            except Exception:
                continue
    raise HTTPException(status_code=502, detail="Claude's answer could not be read - try again.")


def _hint(k: str) -> str:
    return ("..." + k[-4:]) if k else ""


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    """What any page needs to know: is AI on (never the key itself)."""
    return {"ai": enabled(), "model": model()}


@router.get("/settings")
def get_settings(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    d = _load()
    k = _api_key()
    src = "settings" if (d.get("key") or "").strip() else ("environment" if os.environ.get("ANTHROPIC_API_KEY") else ("captions" if k else ""))
    return {"ai": enabled(), "backend": backend(), "key_hint": _hint(k), "has_key": bool(k), "source": src, "model": model(),
            "cli": cli_path(), "cli_models": list(CLI_MODELS)}


class AiSettings(BaseModel):
    key: Optional[str] = Field(None, max_length=400)
    model: Optional[str] = Field(None, max_length=120)
    backend: Optional[str] = Field(None, pattern="^(api|claude_code)$")
    cli_path: Optional[str] = Field(None, max_length=400)


@router.put("/settings")
def put_settings(body: AiSettings, current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    d = _load()
    if body.key is not None:
        d["key"] = body.key.strip()
    if body.model is not None:
        d["model"] = body.model.strip()
    if body.backend is not None:
        d["backend"] = body.backend
        d.pop("model", None)       # the two take different model names
    if body.cli_path is not None:
        d["cli_path"] = body.cli_path.strip()
    _save(d)
    return get_settings(current_user)


@router.get("/models")
def models(current_user: User = Depends(get_current_user)):
    """The models this key can use, newest first."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    if backend() == "claude_code":
        return {"models": [{"id": m, "name": {"auto": "Auto (recommended): Haiku for quick jobs, Sonnet for judging photos", "sonnet": "Sonnet (latest)", "opus": "Opus (latest)", "haiku": "Haiku (latest, fastest)"}[m]} for m in CLI_MODELS]}
    k = key()
    if not k:
        return {"models": []}
    req = urllib.request.Request(f"{API}/models?limit=100",
                                 headers={"x-api-key": k, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail="The key was refused" if e.code == 401 else f"Claude answered {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Claude: {e}")
    auto = {"id": "auto", "name": "Auto (recommended): Haiku for quick jobs, Sonnet for judging photos - the lowest cost that does each job well"}
    return {"models": [auto] + [{"id": m.get("id"), "name": m.get("display_name") or m.get("id")} for m in d.get("data", [])]}


@router.post("/test")
def test(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    try:
        t = ask("Reply with the single word: ready", max_tokens=10, temperature=0, tier="fast")
    except AiOff as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "reply": t, "model": model_for("fast")}


def install(app):
    from fastapi.responses import JSONResponse

    async def _off(request, exc: AiOff):
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    app.add_exception_handler(AiOff, _off)
    app.include_router(router)
