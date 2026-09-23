"""The assistant: ask Zerko in plain words and it does the work.

"Take the photos from the Bell Road card folder, HDR them and have them ready
in a project for 22 Bell Road" becomes: find the folder, make the property
(address looked up and put on the map), make its project, bring the photos
in, start the HDR merge, and open the project.

Claude plans; the steps themselves are Zerko's own API, called as the person
who asked (their permissions apply, nothing is done behind their back).
Anything that moves or renames files already there waits for a Yes in the
chat before it runs.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import ai
from auth import get_current_user
from database import IndexedFolder, User, Video, get_db

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
MAX_STEPS = 14


# --------------------------------------------------------------------------
# calling Zerko's own API as the person asking
# --------------------------------------------------------------------------

class Api:
    def __init__(self, token: str):
        self.token = token
        self.base = f"http://127.0.0.1:{int(os.environ.get('PORT', 9600))}"

    def call(self, method: str, path: str, body: Any = None, query: Optional[dict] = None, timeout: float = 120):
        url = self.base + path + (("?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        # the call is to this same server: never through a proxy
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:
                detail = None
            raise ToolError(f"{e.code}: {detail or e.reason}")


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------
# the tools
# --------------------------------------------------------------------------

TOOLS_DOC = """Tools (call them by name with JSON args):
- find_folders {"query": "words"}: library folders whose name or path match (id, path, how many files).
- list_folder {"folder_id": 12}: its subfolders and files (up to 80 files: id, name, type).
- find_properties {"query": "address words"}: properties (shoots) matching.
- find_files {"query": "words", "folder_id": 12 (optional), "type": "photo|video|any"}: files by name.
- create_folder {"name": "...", "parent_folder_id": 12 (optional, none = top level)}
- rename_folder {"folder_id": 12, "name": "..."}  (asks the person first)
- move_folder {"folder_id": 12, "into_folder_id": 34}  (asks the person first)
- move_files {"file_ids": [..] or "from_folder_id": 12, "into_folder_id": 34}  (asks the person first)
- rename_files {"file_ids": [..], "pattern": "{property} {n}" - words in braces: {name} {n} {date} {property} {folder}}  (asks the person first)
- find_address {"text": "22 Bell Road"}: looks the address up (suburb, map position). Use the best match.
- create_property {"address": "...", "suburb": "...", "agent": "...", "lat": .., "lng": .., "shoot_date": "YYYY-MM-DD"}: a property on the map and in the list. If it already exists you get the existing one.
- update_property {"property_id": 3, "status": "booked|shot|editing|posted", "agent": "...", "shoot_date": "..."}
- create_project {"name": "22 Bell Road", "parent_folder_id": null, "file_ids": [..] (optional), "move_files": true, "property_id": 3 (optional: links its folder to the property)}: a project folder with its stages (01 Originals, 02 HDR, 03 Edited, 04 Selects, 05 Exports). With file_ids those files are brought into 01 Originals (moved, or copied with move_files false) - moving asks the person first.
- hdr {"folder_id": 12 or "file_ids": [..], "mode": "hdr|flambient|panorama"}: finds the brackets and starts merging them. In a project the results go to its 02 HDR folder. Runs in the background.
- job_status {"kind": "hdr", "job_id": "..."}
- open {"page": "folder|property|editor|properties|library|projects", "id": 12}: takes the person there when you are done.
Say what you did in plain words. Never invent ids: find them with the tools first."""

SYSTEM = (
    "You are the assistant inside Zerko, a media manager and photo studio app for a real-estate photography business. "
    "You do what the person asks by calling tools, step by step, until it is done, then say briefly what you did. "
    "Ask a short question only when you cannot tell what they mean (for example two folders match equally); "
    "otherwise go all the way without stopping to check - make sensible choices and say what you chose. "
    "If an address lookup fails, still create the property without a map position (it can be placed on the map later). "
    "When a job asks for a property and a project, make both, link them, bring the files in, start any processing, and open "
    "the project's first stage folder (01 Originals, or 02 HDR after an HDR) so the person lands where their work is. "
    "Keep replies short and plain, no emoji, no markdown headings.\n\n" + TOOLS_DOC + "\n\n"
    'Answer ONLY with JSON: {"say": "text for the person (optional while working)", "calls": [{"tool": "...", "args": {...}}]}. '
    'Use "calls": [] when you are finished or need an answer from the person. Up to 4 calls per step; the results come back to you.'
)

CONFIRM = {"rename_folder", "move_folder", "move_files", "rename_files"}


def _confirm_key(tool: str, args: dict) -> str:
    return tool + ":" + json.dumps(args, sort_keys=True)


class Ctx:
    def __init__(self, api: Api, db: Session, approved: List[str]):
        self.api, self.db, self.approved = api, db, set(approved)
        self.pending: List[dict] = []
        self.actions: List[dict] = []
        self.log: List[str] = []


def _folder_dict(f: IndexedFolder, n: int = 0) -> dict:
    return {"id": f.id, "path": f.path, "name": f.name, "files": n}


def t_find_folders(c: Ctx, a: dict):
    q = str(a.get("query") or "").strip().lower()
    words = [w for w in re.split(r"\s+", q) if w]
    rows = c.db.query(IndexedFolder).all()
    scored = []
    for f in rows:
        hay = f"{f.name or ''} {f.path or ''}".lower()
        hit = sum(1 for w in words if w in hay)
        if words and hit:
            scored.append((hit, len(f.path or ""), f))
    scored.sort(key=lambda x: (-x[0], x[1]))
    from sqlalchemy import func
    out = []
    for _, _, f in scored[:20]:
        n = c.db.query(func.count(Video.id)).filter(Video.folder_id == f.id, Video.is_active.isnot(False)).scalar() or 0
        out.append(_folder_dict(f, n))
    return {"folders": out}


def t_list_folder(c: Ctx, a: dict):
    fid = int(a["folder_id"])
    f = c.db.query(IndexedFolder).filter(IndexedFolder.id == fid).first()
    if not f:
        raise ToolError("No such folder")
    subs = c.db.query(IndexedFolder).filter(IndexedFolder.parent_id == fid).all()
    files = (c.db.query(Video).filter(Video.folder_id == fid, Video.is_active.isnot(False))
             .order_by(Video.filename.asc()).limit(80).all())
    total = c.db.query(Video).filter(Video.folder_id == fid, Video.is_active.isnot(False)).count()
    return {"folder": _folder_dict(f, total), "subfolders": [_folder_dict(s) for s in subs],
            "files": [{"id": v.id, "name": v.filename, "type": v.media_type} for v in files], "total_files": total}


def t_find_files(c: Ctx, a: dict):
    q = c.db.query(Video).filter(Video.is_active.isnot(False))
    if a.get("folder_id"):
        q = q.filter(Video.folder_id == int(a["folder_id"]))
    if a.get("type") in ("photo", "video", "audio"):
        q = q.filter(Video.media_type == a["type"])
    for w in [w for w in re.split(r"\s+", str(a.get("query") or "")) if w][:5]:
        q = q.filter(Video.filename.ilike(f"%{w}%"))
    rows = q.order_by(Video.filename.asc()).limit(100).all()
    return {"files": [{"id": v.id, "name": v.filename, "type": v.media_type, "folder_id": v.folder_id} for v in rows]}


def t_find_properties(c: Ctx, a: dict):
    d = c.api.call("GET", "/api/shoots", query={"q": a.get("query") or "", "limit": 20})
    rows = d.get("shoots", d) if isinstance(d, dict) else d
    return {"properties": [{"id": s["id"], "address": s["address"], "suburb": s.get("suburb"), "status": s.get("status"),
                            "folders": s.get("folders"), "folder_ids": s.get("folder_ids"), "project_id": s.get("project_id")}
                           for s in rows[:20]]}


def t_create_folder(c: Ctx, a: dict):
    d = c.api.call("POST", "/api/folders", {"name": a["name"], "parent_id": a.get("parent_folder_id")})
    c.log.append(f"Made the folder {d.get('name')}")
    return d


def t_rename_folder(c: Ctx, a: dict):
    d = c.api.call("PUT", f"/api/folders/{int(a['folder_id'])}", {"name": a["name"]})
    c.log.append(f"Renamed a folder to {a['name']}")
    return d


def t_move_folder(c: Ctx, a: dict):
    d = c.api.call("POST", f"/api/assistant/move-folder", {"folder_id": int(a["folder_id"]), "into_folder_id": int(a["into_folder_id"])})
    c.log.append(f"Moved the folder into {d.get('path')}")
    return d


def t_move_files(c: Ctx, a: dict):
    ids = [int(i) for i in a.get("file_ids") or []]
    if not ids and a.get("from_folder_id"):
        ids = [v.id for v in c.db.query(Video.id).filter(Video.folder_id == int(a["from_folder_id"]), Video.is_active.isnot(False)).all()]
    if not ids:
        raise ToolError("No files to move")
    ok = 0
    errors = []
    for i in ids[:3000]:
        try:
            c.api.call("POST", f"/api/videos/{i}/folder", {"folder_id": int(a["into_folder_id"])})
            ok += 1
        except ToolError as e:
            errors.append(str(e))
    c.log.append(f"Moved {ok} file{'s' if ok != 1 else ''}")
    return {"moved": ok, "errors": errors[:5]}


def t_rename_files(c: Ctx, a: dict):
    d = c.api.call("POST", "/api/videos/rename-batch", {"video_ids": [int(i) for i in a["file_ids"]], "pattern": a.get("pattern") or "{name}",
                                                         "dry_run": False})
    c.log.append(f"Renamed {len(a['file_ids'])} files")
    return d


def t_find_address(c: Ctx, a: dict):
    d = c.api.call("GET", "/api/shoots/geocode/search", query={"q": a["text"]}, timeout=30)
    return {"results": (d.get("results") or [])[:5]}


def t_create_property(c: Ctx, a: dict):
    body = {k: a.get(k) for k in ("address", "suburb", "agent", "lat", "lng", "shoot_date", "note") if a.get(k) not in (None, "")}
    try:
        d = c.api.call("POST", "/api/shoots", body)
        c.log.append(f"Added the property {d.get('address')}")
    except ToolError as e:
        if "409" not in str(e):
            raise
        found = t_find_properties(c, {"query": a["address"]})["properties"]
        if not found:
            raise
        d = found[0]
        d["existed"] = True
    return {"id": d["id"], "address": d.get("address"), "existed": d.get("existed", False)}


def t_update_property(c: Ctx, a: dict):
    pid = int(a.pop("property_id"))
    d = c.api.call("PATCH", f"/api/shoots/{pid}", {k: v for k, v in a.items() if v not in (None, "")})
    c.log.append("Updated the property")
    return {"id": d.get("id"), "status": d.get("status")}


def t_create_project(c: Ctx, a: dict):
    ids = [int(i) for i in a.get("file_ids") or []]
    move = bool(a.get("move_files", True))
    d = c.api.call("POST", "/api/projects", {"name": a["name"], "parent_folder_id": a.get("parent_folder_id"),
                                              "video_ids": ids, "move_files": move}, timeout=600)
    c.log.append(f"Made the project {d.get('name')}" + (f" with {len(ids)} files {'moved' if move else 'copied'} in" if ids else ""))
    if a.get("property_id"):
        try:
            c.api.call("POST", f"/api/shoots/{int(a['property_id'])}/folders", {"path": d["path"]})
        except ToolError as e:
            c.log.append(f"Could not link the project to the property ({e})")
    st = d.get("stages") or {}
    return {"id": d.get("id"), "name": d.get("name"), "path": d.get("path"),
            "stage_folders": {k: v.get("folder_id") for k, v in st.items()}}


def t_hdr(c: Ctx, a: dict):
    body = {"mode": a.get("mode") or "hdr"}
    if a.get("file_ids"):
        body["video_ids"] = [int(i) for i in a["file_ids"]]
    elif a.get("folder_id"):
        body["folder_id"] = int(a["folder_id"])
    else:
        raise ToolError("Give a folder_id or file_ids")
    scan = c.api.call("POST", "/api/hdr/scan", body, timeout=300)
    groups = [[i["id"] for i in g["items"]] for g in scan.get("groups", [])]
    if not groups:
        return {"message": scan.get("message") or "No brackets found", "brackets": 0}
    job = c.api.call("POST", "/api/hdr/merge", {"groups": groups, "mode": body["mode"], "fmt": "jpg"})
    c.log.append(f"Started merging {len(groups)} bracket{'s' if len(groups) != 1 else ''}")
    return {"brackets": len(groups), "job_id": job.get("job_id"), "running": True}


def t_job_status(c: Ctx, a: dict):
    if a.get("kind", "hdr") == "hdr":
        d = c.api.call("GET", f"/api/hdr/jobs/{a['job_id']}")
        return {k: d.get(k) for k in ("running", "done", "total", "current", "errors")}
    raise ToolError("Unknown job kind")


def t_open(c: Ctx, a: dict):
    page, i = a.get("page"), a.get("id")
    to = {"folder": f"/library?f={i}", "property": f"/properties?p={i}", "editor": f"/edit/{i}", "properties": "/properties",
          "library": "/library", "projects": "/library"}.get(page)
    if not to:
        raise ToolError("Unknown page")
    c.actions.append({"navigate": to})
    return {"opened": to}


TOOLS = {
    "find_folders": t_find_folders, "list_folder": t_list_folder, "find_files": t_find_files, "find_properties": t_find_properties,
    "create_folder": t_create_folder, "rename_folder": t_rename_folder, "move_folder": t_move_folder, "move_files": t_move_files,
    "rename_files": t_rename_files, "find_address": t_find_address, "create_property": t_create_property,
    "update_property": t_update_property, "create_project": t_create_project, "hdr": t_hdr, "job_status": t_job_status,
    "open": t_open,
}


def _needs_ok(tool: str, args: dict) -> bool:
    if tool in CONFIRM:
        return True
    return tool == "create_project" and bool(args.get("file_ids")) and args.get("move_files", True)


def _describe(c: Ctx, tool: str, a: dict) -> str:
    def fname(fid):
        f = c.db.query(IndexedFolder).filter(IndexedFolder.id == int(fid)).first() if fid else None
        return f.path if f else f"folder {fid}"
    if tool == "rename_folder":
        return f"Rename {fname(a.get('folder_id'))} to \"{a.get('name')}\""
    if tool == "move_folder":
        return f"Move {fname(a.get('folder_id'))} into {fname(a.get('into_folder_id'))}"
    if tool == "move_files":
        n = len(a.get("file_ids") or []) or c.db.query(Video).filter(Video.folder_id == int(a.get("from_folder_id") or 0)).count()
        src = f" from {fname(a['from_folder_id'])}" if a.get("from_folder_id") else ""
        return f"Move {n} file{'s' if n != 1 else ''}{src} into {fname(a.get('into_folder_id'))}"
    if tool == "rename_files":
        return f"Rename {len(a.get('file_ids') or [])} files as \"{a.get('pattern')}\""
    if tool == "create_project":
        return f"Make the project \"{a.get('name')}\" and move {len(a.get('file_ids') or [])} files into its 01 Originals"
    return f"{tool} {json.dumps(a)}"


# --------------------------------------------------------------------------
# the conversation
# --------------------------------------------------------------------------

class Msg(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    text: str = Field(..., max_length=8000)
    # what that reply did (tool calls and results, trimmed) - so the next
    # turn carries on from there instead of starting again
    memo: str = Field("", max_length=20000)


class ChatBody(BaseModel):
    messages: List[Msg] = Field(..., min_length=1, max_length=60)
    context: Dict[str, Any] = Field(default_factory=dict)      # where the person is: page, folder, photo...
    approved: List[str] = Field(default_factory=list)          # confirm keys the person said Yes to


def _transcript(messages: List[Msg], context: dict, steps: List[dict]) -> str:
    out = []
    if context:
        out.append("Where the person is in Zerko right now: " + json.dumps(context)[:1500])
    out.append("Conversation:")
    for m in messages[-24:]:
        out.append(("Person: " if m.role == "user" else "You: ") + m.text)
        if m.role == "assistant" and m.memo:
            out.append("  (what you did then: " + m.memo + ")")
    if steps:
        out.append("\nWhat you have done so far this turn (tool calls and their results):")
        for s in steps:
            out.append(json.dumps(s)[:6000])
    out.append("\nNext step - JSON only.")
    return "\n".join(out)


@router.post("/chat")
def chat(body: ChatBody, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not ai.enabled():
        raise ai.AiOff("AI is off: an administrator adds an Anthropic API key in Manage > AI.")
    tok = (request.headers.get("Authorization") or "")[7:]
    c = Ctx(Api(tok), db, body.approved)
    steps: List[dict] = []
    says: List[str] = []
    # what the person just said Yes to runs first, exactly as it was asked for
    for k in body.approved:
        tool, _, raw = k.partition(":")
        try:
            args = json.loads(raw)
        except ValueError:
            continue
        if tool not in TOOLS:
            continue
        try:
            steps.append({"tool": tool, "args": args, "result": TOOLS[tool](c, dict(args)), "approved": True})
        except ToolError as e:
            steps.append({"tool": tool, "args": args, "error": str(e), "approved": True})
    for _ in range(MAX_STEPS):
        d = ai.ask_json(_transcript(body.messages, body.context, steps), None, SYSTEM, max_tokens=2000, temperature=0.2)
        if isinstance(d, list):
            d = {"calls": d}
        say = str(d.get("say") or "").strip() if isinstance(d, dict) else ""
        calls = d.get("calls") or [] if isinstance(d, dict) else []
        if say:
            says.append(say)
        if not calls:
            break
        stop = False
        for call in calls[:4]:
            tool = str(call.get("tool") or "")
            args = call.get("args") or {}
            if tool not in TOOLS:
                steps.append({"tool": tool, "error": "no such tool"})
                continue
            if _needs_ok(tool, args) and any(s.get("approved") and s.get("tool") == tool and s.get("args") == args for s in steps):
                continue        # already done above
            if _needs_ok(tool, args) and _confirm_key(tool, args) not in c.approved:
                c.pending.append({"key": _confirm_key(tool, args), "text": _describe(c, tool, args)})
                steps.append({"tool": tool, "args": args, "result": "waiting for the person to say Yes in the chat"})
                stop = True
                continue
            try:
                res = TOOLS[tool](c, dict(args))
                steps.append({"tool": tool, "args": args, "result": res})
            except ToolError as e:
                steps.append({"tool": tool, "args": args, "error": str(e)})
            except Exception as e:
                steps.append({"tool": tool, "args": args, "error": f"{type(e).__name__}: {e}"})
        if stop:
            if not says or not says[-1].endswith("?"):
                says.append("This needs your OK first:")
            break
    return {"reply": (says[-1] if says else "") or ("Done." if c.log else "I could not work that out - can you say it another way?"),
            "done": c.log, "pending": c.pending, "actions": c.actions,
            "memo": json.dumps([{k: v for k, v in s.items() if k != "approved"} for s in steps])[:18000],
            "steps": [{"tool": s.get("tool"), "ok": "error" not in s} for s in steps]}


# --------------------------------------------------------------------------
# a folder moved into another one (the library has rename, not move)
# --------------------------------------------------------------------------

class MoveFolder(BaseModel):
    folder_id: int
    into_folder_id: int


def install(app, upload_root, resolve_media_path, ensure_folder_row):
    from pathlib import Path

    @router.post("/move-folder")
    def move_folder(body: MoveFolder, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
        import permissions
        if not permissions.can(current_user.role, permissions.ORGANISE):
            raise HTTPException(status_code=403, detail="Your account cannot move files.")
        f = db.query(IndexedFolder).filter(IndexedFolder.id == body.folder_id).first()
        dest = db.query(IndexedFolder).filter(IndexedFolder.id == body.into_folder_id).first()
        if not f or not dest:
            raise HTTPException(status_code=404, detail="Folder not found")
        src = Path(resolve_media_path(f.path)).resolve()
        into = Path(resolve_media_path(dest.path)).resolve()
        root = Path(upload_root).resolve()
        for p in (src, into):
            if not (p == root or root in p.parents):
                raise HTTPException(status_code=400, detail="Folder is outside the media root")
        if into == src or src in into.parents:
            raise HTTPException(status_code=400, detail="A folder cannot go inside itself")
        target = into / src.name
        if target.exists():
            raise HTTPException(status_code=409, detail=f"'{src.name}' already exists there")
        os.rename(src, target)
        old_s, new_s = str(src), str(target)
        inside = lambda col: (col == old_s) | col.like(old_s + os.sep + "%")
        for row in db.query(IndexedFolder).filter(inside(IndexedFolder.path)).all():
            row.path = new_s + row.path[len(old_s):]
            try:
                row.relative_path = str(Path(row.path).relative_to(root)).replace(os.sep, "/")
            except Exception:
                pass
        for v in db.query(Video).filter(inside(Video.filepath)).all():
            v.filepath = new_s + v.filepath[len(old_s):]
        f.parent_id = dest.id
        db.commit()
        return {"id": f.id, "path": f.path}

    app.include_router(router)
