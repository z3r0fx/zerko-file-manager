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

DOC_EXTENSIONS = {'.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.md', '.csv'}

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


def is_playable(filename):
    """True for things with a real AV stream - the only things worth
    thumbnailing, proxying or transcribing."""
    return get_media_type(filename) in ('video', 'audio')


def can_have_proxy(filename):
    return get_media_type(filename) == 'video'
