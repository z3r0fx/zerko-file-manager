"""Who may do what.

One table, read by one middleware. The alternative - a role check pasted into
each of ninety-odd endpoints - is how an endpoint ends up unprotected: nobody
notices the one that was missed, because nothing lists them side by side.

The rules below are matched in order and the first match wins, so put the
specific ones above the general ones.
"""

import re
from typing import Optional

# --- capabilities ----------------------------------------------------------

READ = "media.read"
DOWNLOAD = "media.download"
UPLOAD = "media.upload"
ORGANISE = "media.organise"        # move, rename, folders
ANNOTATE = "media.annotate"        # tags, notes, rating, status
TRASH = "media.trash"              # reversible delete
HARD_DELETE = "media.harddelete"   # destroys the file on disk
TRANSCRIBE = "jobs.transcribe"
PROXY = "jobs.proxy"               # generating and cancelling proxies
SHARES = "shares.manage"
CREATE_CLIENTS = "users.create_clients"
ADMIN = "admin"

_EDITOR = {READ, DOWNLOAD, UPLOAD, ORGANISE, ANNOTATE, TRASH,
           TRANSCRIBE, SHARES, CREATE_CLIENTS}
_CLIENT = {READ, DOWNLOAD, UPLOAD, ORGANISE, ANNOTATE, TRASH}

ROLE_CAPS = {
    # Everything, including the things that cannot be undone.
    "admin": {READ, DOWNLOAD, UPLOAD, ORGANISE, ANNOTATE, TRASH, HARD_DELETE,
              TRANSCRIBE, PROXY, SHARES, CREATE_CLIENTS, ADMIN},
    # Runs the library day to day. No proxy control (it saturates the GPU and
    # the queue is shared), no permanent deletes, no admin dashboard.
    "editor": _EDITOR,
    # An agent or client: brings footage in, takes deliverables out, keeps
    # their own work tidy. Nothing that costs machine time, nothing permanent.
    "client": _CLIENT,
    # Look, don't touch.
    "viewer": {READ, DOWNLOAD},
    # Legacy accounts created before roles existed. Treated as editors.
    "user": _EDITOR,
}

# Roles an editor is allowed to hand out. Deliberately excludes editor itself:
# an account that can mint its own peers is an account that can mint admins one
# social-engineering step later.
EDITOR_MAY_CREATE = ("client", "viewer")

VALID_ROLES = ("admin", "editor", "client", "viewer")

ROLE_LABELS = {
    "admin": "Administrator",
    "editor": "Editor",
    "client": "Client / agent",
    "viewer": "View only",
    "user": "Editor (legacy)",
}


# Capabilities every role has. A request needing only these cannot be refused
# by anyone who is signed in, so the middleware skips the token lookup - which
# keeps the read path (grids, thumbnails, the event stream) off the database.
UNIVERSAL_CAPS = set.intersection(*(set(v) for v in ROLE_CAPS.values()))


def caps_for(role: Optional[str]) -> set:
    return set(ROLE_CAPS.get((role or "").lower(), ROLE_CAPS["viewer"]))


def can(role: Optional[str], capability: str) -> bool:
    return capability in caps_for(role)


# --- path rules ------------------------------------------------------------

ANY = ("GET", "POST", "PUT", "PATCH", "DELETE")
WRITES = ("POST", "PUT", "PATCH", "DELETE")

# (methods, compiled pattern, capability or None for "signed in is enough")
RULES = [
    # Things every signed-in account must be able to do, including a viewer:
    # sign in and out, change their own password, and mint the short-lived
    # tokens their browser needs to play back what they can already see.
    (ANY, r"^/api/(login|logout|me|me/password|video-access-token)$", None),
    (ANY, r"^/api/videos/\d+/download-token$", DOWNLOAD),
    # Public share links carry their own token and are not user sessions.
    (ANY, r"^/api/public/", None),

    # Admin surface.
    (ANY, r"^/api/(admin|updates|duplicates)(/|$)", ADMIN),
    (ANY, r"^/api/(security-check|server-stats)$", ADMIN),
    (ANY, r"^/api/tags/(rules|auto-generate)$", ADMIN),
    (ANY, r"^/api/videos/\d+/reveal$", ADMIN),
    (("POST",), r"^/api/thumbnails/repair$", ADMIN),
    (ANY, r"^/api/videos/\d+/location$", ORGANISE),

    # Accounts. Creating one is checked again inside the endpoint, which is
    # what stops an editor minting an editor.
    (("POST",), r"^/api/register$", CREATE_CLIENTS),
    (ANY, r"^/api/users(/|$)", ADMIN),

    # Machine time.
    (ANY, r"^/api/proxies(/|$)", PROXY),
    (("POST",), r"^/api/videos/\d+/transcribe$", TRANSCRIBE),
    (("POST",), r"^/api/videos/batch-transcribe$", TRANSCRIBE),

    # Destroying things.
    (("POST",), r"^/api/trash/empty$", HARD_DELETE),
    (("DELETE",), r"^/api/videos/\d+$", TRASH),
    (("POST",), r"^/api/videos/\d+/restore$", TRASH),

    # Moving things about.
    (WRITES, r"^/api/folders(/|$)", ORGANISE),
    (("POST",), r"^/api/rescan$", ORGANISE),
    (("POST",), r"^/api/videos/\d+/(rename|folder)$", ORGANISE),
    (WRITES, r"^/api/upload", UPLOAD),

    # Sharing.
    (WRITES, r"^/api/shares(/|$)", SHARES),

    # Files: one shared storage. Browsing is reading; the URLs a browser opens
    # directly (downloads, zips) need the download permission; anything that
    # changes the storage needs upload; emptying the trash for good is an
    # administrator's call; share links need the sharing permission.
    (("GET",), r"^/api/files/(download|zip)$", DOWNLOAD),
    (("POST",), r"^/api/files/trash/(purge|empty)$", HARD_DELETE),
    (WRITES, r"^/api/files/shares(/|$)", SHARES),
    (("POST",), r"^/api/files/star$", None),
    (WRITES, r"^/api/files(/|$)", UPLOAD),

    # Photo export: making a 16:9 ZIP is a download; its status and the ZIP itself are too.
    (("POST",), r"^/api/photo-export/generate$", DOWNLOAD),
    (("GET",), r"^/api/photo-export/jobs/[\w-]+/download$", DOWNLOAD),

    # Reading is reading.
    (("GET",), r"^/api/", READ),
    # Anything else that writes is ordinary library housekeeping: tags, notes,
    # ratings, status. If a new endpoint appears and nobody adds a rule, it
    # lands here - a write capability, not an open door.
    (WRITES, r"^/api/", ANNOTATE),
]

COMPILED = [(methods, re.compile(pattern), cap) for methods, pattern, cap in RULES]


def required_capability(method: str, path: str):
    """(matched, capability). matched=False means no rule applied, which for a
    path under /api/ should not happen - the last two rules are catch-alls."""
    for methods, pattern, cap in COMPILED:
        if method in methods and pattern.match(path):
            return True, cap
    return False, None


def check(role: Optional[str], method: str, path: str):
    """None when allowed, otherwise a sentence to send back with the 403."""
    matched, cap = required_capability(method, path)
    if not matched or cap is None:
        return None
    if can(role, cap):
        return None
    return DENIALS.get(cap, "Your account does not have access to that.")


DENIALS = {
    ADMIN: "That is an administrator setting.",
    HARD_DELETE: "Only an administrator can delete files permanently. "
                 "Move them to the trash instead.",
    PROXY: "Only an administrator can start or stop proxy generation.",
    TRANSCRIBE: "Your account cannot start transcriptions.",
    UPLOAD: "Your account cannot upload files.",
    ORGANISE: "Your account cannot move or rename files.",
    TRASH: "Your account cannot delete files.",
    SHARES: "Your account cannot create share links.",
    CREATE_CLIENTS: "Your account cannot create other accounts.",
    ANNOTATE: "Your account has view-only access.",
    READ: "Your account does not have access to that.",
    DOWNLOAD: "Your account cannot download files.",
}
