import os
import cv2
import json
import subprocess
from pathlib import Path
from datetime import datetime
from sqlalchemy.orm import Session
from database import Video, IndexedFolder, Tag
from PIL import Image

# How a camera identifies itself in a container. These files carry no
# make/model tags at all - which is why camera_model sat empty on all 1,456
# videos - but they do leave a fingerprint in `encoder`, `major_brand` and the
# stream handler names. Checked in order; first match wins.
CAMERA_SIGNATURES = [
    # (make, model, encoder substr, major_brand, handler substr, tag key)
    ("DJI",              "Mavic 3",            "djimavic3",   None,     "dji meta",  None),
    ("DJI",              "Drone",              "dji",         None,     "dji ",      None),
    ("Sony",             "XAVC camera",        None,          "xavc",   None,        None),
    ("GoPro",            "GoPro",              "gopro",       None,     "gopro",     None),
    ("Nikon",            "Nikon",              "nikon",       None,     None,        None),
    ("Canon",            "Canon",              "canon",       None,     None,        None),
    ("Blackmagic Design", "DaVinci Resolve render", "davinci resolve", None, None,   None),
    ("Apple",            "iPhone",             None,          None,     None,        "com.apple.quicktime.model"),
    ("Android",          "Android device",     None,          None,     None,        "com.android.version"),
]


def _identify_camera(fmt_tags: dict, streams: list) -> tuple:
    """Best guess at (make, model) from container fingerprints."""
    lower = {str(k).lower(): str(v) for k, v in (fmt_tags or {}).items()}

    # A real make/model tag always wins if one is present.
    make = lower.get("make") or lower.get("com.apple.quicktime.make")
    model = lower.get("model") or lower.get("com.apple.quicktime.model")
    if make or model:
        return make, model

    encoder = lower.get("encoder", "").lower()
    brand = lower.get("major_brand", "").strip().lower()
    handlers = " ".join(
        str((s.get("tags") or {}).get("handler_name", "")).lower() for s in (streams or [])
    )

    for mk, md, enc_sub, brand_eq, handler_sub, tag_key in CAMERA_SIGNATURES:
        if enc_sub and enc_sub in encoder:
            return mk, md
        if brand_eq and brand == brand_eq:
            return mk, md
        if handler_sub and handler_sub in handlers:
            return mk, md
        if tag_key and tag_key in lower:
            return mk, lower.get(tag_key) or md
    return None, None


def get_technical_metadata(video_path: str) -> dict:
    """Extract technical metadata using ffprobe."""
    metadata = {}
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(result.stdout)

        fmt = data.get('format', {})
        tags = fmt.get('tags', {})
        streams = data.get('streams', [])

        make, model = _identify_camera(tags, streams)
        metadata['camera_make'] = make
        metadata['camera_model'] = model

        v_stream = next((s for s in streams if s.get('codec_type') == 'video'), {})
        metadata['video_codec'] = v_stream.get('codec_name')
        w, h = v_stream.get('width'), v_stream.get('height')
        if w and h:
            metadata['resolution'] = f"{w}x{h}"

        # ffprobe gives frame rate as a fraction; guard against 0/0 on files
        # with no real video stream rather than dying on ZeroDivisionError.
        fps_str = v_stream.get('r_frame_rate')
        if fps_str and '/' in fps_str:
            try:
                num, den = (int(x) for x in fps_str.split('/'))
                if den:
                    metadata['frame_rate'] = f"{round(num / den)}fps"
            except (ValueError, ZeroDivisionError):
                pass

        a_stream = next((s for s in streams if s.get('codec_type') == 'audio'), {})
        metadata['audio_codec'] = a_stream.get('codec_name')

    except Exception as e:
        print(f"Error extracting metadata for {video_path}: {e}")
    return metadata


VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
                    '.mxf', '.r3d', '.braw', '.mts', '.m2ts', '.insv', '.avchd'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif', '.svg',
                    '.dng', '.arw', '.cr2', '.cr3', '.nef', '.raf', '.orf', '.rw2', '.heic', '.heif'}
# Sidecars that sit next to real media and should never become library items
SIDECAR_EXTENSIONS = {'.xml', '.lrf', '.srt', '.dat', '.thm', '.cpi', '.mpl', '.bdm', '.sec',
                      '.wpl', '.modd', '.moff', '.rmd', '.zip', '.txt', '.ini', '.db'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.aac', '.flac', '.ogg', '.aiff', '.m4a', '.wma', '.opus'}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
# ... (rest of file)
def get_duration_fast(video_path: str):
    """Duration via ffprobe - reads the container header only.

    The old get_video_duration() used cv2.VideoCapture + CAP_PROP_FRAME_COUNT,
    which walks a large part of the file. On a multi-GB clip that took seconds
    and it was being called inline inside the upload request. ffprobe returns
    in milliseconds regardless of file size.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, text=True, timeout=30
        )
        value = (result.stdout or '').strip()
        if value:
            return float(value)
    except Exception as e:
        print(f"ffprobe duration failed for {video_path}: {e}")
    # Fall back to the slow path rather than losing the duration entirely.
    try:
        return get_video_duration(video_path)
    except Exception:
        return None


def get_video_duration(video_path: str) -> float:
    """Get video duration in seconds"""
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        if fps > 0:
            return frame_count / fps
    except Exception as e:
        print(f"Error getting duration for {video_path}: {e}")
    return None

def generate_thumbnail(video_path: str, output_path: str, frame_time: float = 1.0) -> bool:
    """Generate thumbnail from video or image at specified time"""
    file_ext = Path(video_path).suffix.lower()
    
    # Handle images - copy and resize
    if file_ext in IMAGE_EXTENSIONS:
        try:
            with Image.open(video_path) as img:
                # Convert RGBA to RGB if necessary
                if img.mode == 'RGBA':
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # Resize to max 320px width
                max_width = 320
                if img.width > max_width:
                    img.thumbnail((max_width, max_width * img.height // img.width), Image.Resampling.LANCZOS)
                
                img.save(output_path, 'JPEG', quality=85)
                return True
        except Exception as e:
            print(f"Error generating thumbnail for {video_path}: {e}")
        return False
    
    # Handle videos - use cv2
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps > 0:
            frame_number = int(frame_time * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ret, frame = cap.read()
            if ret:
                # Resize to thumbnail size
                height, width = frame.shape[:2]
                max_width = 320
                if width > max_width:
                    ratio = max_width / width
                    new_size = (max_width, int(height * ratio))
                    frame = cv2.resize(frame, new_size)
                
                cv2.imwrite(output_path, frame)
                cap.release()
                return True
        cap.release()
    except Exception as e:
        print(f"Error generating thumbnail for {video_path}: {e}")
    return False

_ENCODER_CACHE = {}


def _best_h264_encoder():
    """Pick the fastest H.264 encoder this machine actually has.

    Probed once and cached - `ffmpeg -encoders` is cheap but not free, and this
    is called for every clip in a long queue.
    """
    if 'h264' in _ENCODER_CACHE:
        return _ENCODER_CACHE['h264']
    choice = 'libx264'
    try:
        out = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                             capture_output=True, text=True, timeout=20).stdout
        for candidate in ('h264_nvenc', 'h264_qsv', 'h264_amf'):
            if candidate in out:
                choice = candidate
                break
    except Exception:
        pass
    _ENCODER_CACHE['h264'] = choice
    print(f"  Proxy encoder: {choice}")
    return choice


def generate_proxy(input_path: str, output_path: str, progress_callback=None,
                   on_start=None):
    """Generate a 720p H.264/AAC proxy using FFmpeg with progress tracking."""
    import re
    
    # Get total duration first for progress calculation
    duration_cmd = [
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', input_path
    ]
    try:
        duration_res = subprocess.run(duration_cmd, capture_output=True, text=True)
        total_duration = float(duration_res.stdout.strip())
    except:
        total_duration = 0

    # Hardware encoding where the machine has it. libx264 at 'fast' runs at
    # roughly real time on 4K source, which for a thousand-clip library is
    # measured in days; NVENC turns that into hours. Falls back automatically,
    # so this is safe on a machine with no NVIDIA card.
    encoder = _best_h264_encoder()
    if encoder == 'h264_nvenc':
        vcodec = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '26', '-b:v', '0']
    else:
        vcodec = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '24']

    def build(codec_args):
        return [
            'ffmpeg', '-y', '-i', input_path,
            # Long edge to 1280: keeps vertical 4K (most of this library)
            # readable instead of squashing it to a sliver.
            '-vf', "scale='if(gt(iw,ih),1280,-2)':'if(gt(iw,ih),-2,1280)'",
            *codec_args,
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',  # Good for web streaming
            '-progress', 'pipe:1',
            output_path,
        ]

    time_regex = re.compile(r'out_time_ms=(\d+)')

    def run(codec_args):
        """Run one encode, streaming progress. Returns (returncode, tail)."""
        # Background work: run below normal priority so browsing, previews
        # and the portal stay quick while a backlog encodes.
        kw = {}
        if os.name == "posix":
            kw["preexec_fn"] = lambda: os.nice(10)
        elif os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        proc = subprocess.Popen(build(codec_args), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, **kw)
        # Hand the process out so a Stop request can terminate it. Without
        # this, cancelling a batch still left the current 4K encode running
        # to completion.
        if on_start:
            try:
                on_start(proc)
            except Exception:
                pass
        tail = []
        for line in proc.stdout:
            tail.append(line)
            if len(tail) > 25:
                tail.pop(0)
            if progress_callback and total_duration > 0:
                match = time_regex.search(line)
                if match:
                    # out_time_ms is actually microseconds, despite the name.
                    current_time = int(match.group(1)) / 1_000_000.0
                    progress_callback(min(99, int((current_time / total_duration) * 100)))
        proc.wait()
        return proc.returncode, "".join(tail)

    code, tail = run(vcodec)

    # `ffmpeg -encoders` lists NVENC whenever ffmpeg was BUILT with it, which
    # says nothing about whether this machine has an NVIDIA card. So a hardware
    # failure is expected, not exceptional: fall back to software and remember
    # the answer so the rest of the queue does not retry the same dead end.
    # code < 0 means the encode was killed (Stop), not that NVENC is missing -
    # falling back then would start a whole new encode after being told to stop.
    if code > 0 and encoder != 'libx264':
        print(f"  {encoder} failed (rc={code}); falling back to libx264")
        _ENCODER_CACHE['h264'] = 'libx264'
        if progress_callback:
            progress_callback(0)
        code, tail = run(['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '24'])

    if code != 0:
        raise Exception(f"FFmpeg failed with return code {code}: {tail[-400:]}")

    if progress_callback:
        progress_callback(100)

def scan_folder(folder_id: int, db: Session) -> dict:
    """Scan a folder and index all video files"""
    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
    if not folder:
        return {"error": "Folder not found"}
    
    if not os.path.exists(folder.path):
        return {"error": f"Path does not exist: {folder.path}"}
    
    stats = {"added": 0, "updated": 0, "skipped": 0, "errors": 0}
    
    # Create thumbnails directory - use env var or default
    thumbnails_dir = Path(os.environ.get("MEDIA_UPLOAD_DIR", str(Path(__file__).parent.resolve() / "uploads"))) / "thumbnails"
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    
    # Walk through directory
    for root, dirs, files in os.walk(folder.path):
        for filename in files:
            file_ext = Path(filename).suffix.lower()
            if file_ext not in MEDIA_EXTENSIONS:
                continue
            
            filepath = os.path.join(root, filename)
            
            try:
                # Check if video already exists
                existing_video = db.query(Video).filter(Video.filepath == filepath).first()
                
                file_size = os.path.getsize(filepath)
                duration = get_video_duration(filepath)
                
                # Generate thumbnail
                thumbnail_filename = f"{hash(filepath)}.jpg"
                thumbnail_path = thumbnails_dir / thumbnail_filename
                thumbnail_generated = generate_thumbnail(filepath, str(thumbnail_path))
                
                if existing_video:
                    # Update existing
                    existing_video.file_size = file_size
                    existing_video.duration = duration
                    if thumbnail_generated:
                        existing_video.thumbnail_path = f"/thumbnails/{thumbnail_filename}"
                    stats["updated"] += 1
                else:
                    # Create new video entry
                    video = Video(
                        filename=filename,
                        filepath=filepath,
                        file_size=file_size,
                        duration=duration,
                        thumbnail_path=f"/thumbnails/{thumbnail_filename}" if thumbnail_generated else None,
                        folder_id=folder_id,
                        status="raw"
                    )
                    db.add(video)
                    stats["added"] += 1
                
                db.commit()
                
            except Exception as e:
                print(f"Error processing {filepath}: {e}")
                stats["errors"] += 1
                continue
    
    # Update last scanned time
    folder.last_scanned = datetime.utcnow()
    db.commit()
    
    return stats

def format_file_size(bytes: int) -> str:
    """Format bytes to human readable size"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.2f} PB"

def format_duration(seconds: float) -> str:
    """Format seconds to HH:MM:SS"""
    if seconds is None:
        return "Unknown"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
