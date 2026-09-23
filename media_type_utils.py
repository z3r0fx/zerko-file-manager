"""Single source of truth for what kind of thing a file is.

This used to keep its own short extension lists and, critically, ended with
`else: return 'video'` - so ANY unrecognised file became a video. A DaVinci
Resolve project (.drp) dropped into the library was queued for proxy
generation and came back "FFmpeg failed with return code 183", and .heic
stills were filed as video because the list here never mentioned them.

The lists below are shared with video_processor so the two can't drift again.
"""

import os

from video_processor import (
    VIDEO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    SIDECAR_EXTENSIONS,
)

DOC_EXTENSIONS = {
    # Documents and text
    '.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.md', '.csv',
    '.xls', '.xlsx', '.ods', '.ppt', '.pptx', '.odp',
    '.pages', '.numbers', '.key', '.epub', '.log', '.json', '.xml.txt',
}

# Everything else somebody might reasonably keep here: archives, installers,
# fonts, 3D, code. Not media, not a project file - just a file. They get the
# file-manager view: stored as-is, renamed, moved, downloaded, and nothing
# tries to thumbnail or transcribe them.
ARCHIVE_EXTENSIONS = {
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.iso', '.dmg',
}
APP_EXTENSIONS = {
    '.exe', '.msi', '.apk', '.appimage', '.deb', '.rpm', '.jar', '.bat', '.sh',
}
FONT_EXTENSIONS = {'.ttf', '.otf', '.woff', '.woff2'}
DESIGN_EXTENSIONS = {'.psd', '.ai', '.eps', '.indd', '.sketch', '.fig', '.afphoto', '.afdesign'}
MODEL_EXTENSIONS = {'.obj', '.fbx', '.stl', '.gltf', '.glb', '.3ds', '.dae'}

# Project / scratch files from the edit suite. They belong beside the footage
# but they are not media: never thumbnail, proxy or transcribe them.
PROJECT_EXTENSIONS = {
    '.drp', '.dra', '.drt',          # DaVinci Resolve project / archive / timeline
    '.prproj', '.aep', '.aepx',      # Premiere, After Effects
    '.fcpxml', '.xml', '.edl', '.aaf', '.otio',
    '.veg', '.wlmp', '.kdenlive', '.blend',
}

MEDIA_TYPES = ('video', 'photo', 'audio', 'document', 'project', 'other')


def get_media_type(filename):
    """Classify by extension. Never guesses 'video' for the unknown."""
    ext = os.path.splitext(filename or '')[1].lower()

    if ext in VIDEO_EXTENSIONS:
        return 'video'
    if ext in IMAGE_EXTENSIONS:
        return 'photo'
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    if ext in DOC_EXTENSIONS:
        return 'document'
    if ext in PROJECT_EXTENSIONS or ext in SIDECAR_EXTENSIONS:
        return 'project'
    return 'other'


# Anything that is not video, photo or audio is "a file" as far as the
# interface is concerned: no rating, no notes, no transcript, no proxy - just
# a name, a size, a date, and somewhere to put it.
def is_media(filename):
    return get_media_type(filename) in ('video', 'photo', 'audio')


FILE_KINDS = {
    'archive': ARCHIVE_EXTENSIONS,
    'app': APP_EXTENSIONS,
    'font': FONT_EXTENSIONS,
    'design': DESIGN_EXTENSIONS,
    'model': MODEL_EXTENSIONS,
    'project': PROJECT_EXTENSIONS,
}


def file_kind(filename):
    """A finer label for non-media, used only to choose an icon. 'document'
    covers anything textual; the rest name themselves."""
    ext = os.path.splitext(filename or '')[1].lower()
    if ext in DOC_EXTENSIONS:
        return 'document'
    for kind, exts in FILE_KINDS.items():
        if ext in exts:
            return kind
    return 'file'


def is_playable(filename):
    """True for things with a real AV stream - the only things worth
    thumbnailing, proxying or transcribing."""
    return get_media_type(filename) in ('video', 'audio')


def can_have_proxy(filename):
    return get_media_type(filename) == 'video'


def is_junk_name(name: str) -> bool:
    """Files the app (or another program) leaves while it is still writing
    something - never catalogue them as media. Hidden files are included:
    nothing a person keeps on purpose starts with a dot on a media drive."""
    n = os.path.basename(name or "")
    low = n.lower()
    return (not n or n.startswith(".") or ".edittmp-" in low or ".hdrtmp-" in low or low.startswith("~$")
            or low.endswith((".part", ".partial", ".crdownload", ".tmp", ".download")))
