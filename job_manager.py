import threading
import queue
import logging
import time
from datetime import datetime
from sqlalchemy.orm import Session
from database import SessionLocal, Video, TranscriptionSegment
from sqlalchemy import func

logger = logging.getLogger(__name__)

class JobManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(JobManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        
        self.queue = queue.Queue()
        self.active_jobs = {} # job_id -> status_dict

        # Cancellation. The DB status alone was never enough: clearing
        # proxy_status only changed a row, while the in-memory queue still
        # held every job and the worker kept going. These three carry the
        # actual stop signal.
        self.cancelled_types = set()   # e.g. {"proxy"} - drop queued work
        self.current_proc = None       # the ffmpeg subprocess, so it can be killed
        self.current_job = None
        self._cancel_lock = threading.Lock()
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()
        
        # One queue per connected browser. Previously there was a single
        # shared queue, so an event was delivered to whichever client polled
        # first and every other open tab silently missed it.
        self.events_queue = queue.Queue()   # kept for backwards compatibility
        self._subscribers = []
        self._sub_lock = threading.Lock()
        self._initialized = True
        logger.info("JobManager initialized")

    def subscribe(self):
        """Register a listener. Returns a queue the caller should drain."""
        q = queue.Queue(maxsize=1000)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def get_event(self, q=None):
        """Non-blocking poll.

        This used to be queue.get(timeout=1.0), called from inside the async
        SSE handler - a full second of blocking the event loop, per connected
        browser, forever. Never block here.
        """
        source = q if q is not None else self.events_queue
        try:
            return source.get_nowait()
        except queue.Empty:
            return None

    def _notify_listeners(self, data):
        self.events_queue.put(data)
        with self._sub_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # Queueing 800+ jobs at once used to overflow this and the
                    # client was dropped for good - its Jobs indicator then
                    # stayed empty forever. Discard the OLDEST event instead;
                    # progress updates are disposable, the listener is not.
                    try:
                        q.get_nowait()
                        q.put_nowait(data)
                    except Exception:
                        pass

    def resume_pending(self):
        """Re-queue work the database still thinks is outstanding.

        The queue lives in memory, so before this a restart stranded every
        queued clip forever: the DB kept saying 'queued' and nothing was left
        to process it. Called once at startup.
        """
        db = SessionLocal()
        requeued = {"proxy": 0, "transcribe": 0}
        try:
            pending_proxy = db.query(Video).filter(
                Video.proxy_status.in_(["queued", "processing"]),
                Video.media_type == "video").all()
            pending_trans = db.query(Video).filter(
                Video.transcription_status.in_(["queued", "processing"]),
                Video.media_type.in_(["video", "audio"])).all()
            for v in pending_proxy:
                self.queue.put((v.id, "proxy", f"proxy_{v.id}"))
                self.active_jobs[f"proxy_{v.id}"] = {
                    "job_id": f"proxy_{v.id}", "video_id": v.id, "type": "proxy",
                    "status": "queued", "progress": 0, "filename": v.filename,
                    "queued_at": datetime.utcnow().isoformat()}
                requeued["proxy"] += 1
            for v in pending_trans:
                self.queue.put((v.id, "transcribe", f"transcribe_{v.id}"))
                self.active_jobs[f"transcribe_{v.id}"] = {
                    "job_id": f"transcribe_{v.id}", "video_id": v.id, "type": "transcribe",
                    "status": "queued", "progress": 0, "filename": v.filename,
                    "queued_at": datetime.utcnow().isoformat()}
                requeued["transcribe"] += 1
        except Exception as e:
            logger.error(f"Could not resume pending jobs: {e}")
        finally:
            db.close()
        if requeued["proxy"] or requeued["transcribe"]:
            logger.info(f"Resumed {requeued['transcribe']} transcription and "
                        f"{requeued['proxy']} proxy jobs from the database")
            print(f"  Resumed {requeued['transcribe']} transcription and "
                  f"{requeued['proxy']} proxy jobs from the database", flush=True)
        return requeued

    def add_job(self, video_id, task_type):
        """Add a job (proxy or transcribe) to the queue."""
        job_id = f"{task_type}_{video_id}"
        
        # Check if already queued or processing
        if job_id in self.active_jobs:
            logger.info(f"Job {job_id} already active")
            return

        status = {
            "job_id": job_id,
            "video_id": video_id,
            "type": task_type,
            "status": "queued",
            "progress": 0,
            "filename": None,
            "queued_at": datetime.utcnow().isoformat()
        }
        
        # Get filename for status
        db = SessionLocal()
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video:
                status["filename"] = video.filename
                # Update DB status
                if task_type == "proxy":
                    video.proxy_status = "queued"
                elif task_type == "transcribe":
                    video.transcription_status = "queued"
                db.commit()
        finally:
            db.close()

        self.active_jobs[job_id] = status
        self.queue.put((video_id, task_type, job_id))
        self._notify_listeners({"type": "job_added", "job": status})
        logger.info(f"Job {job_id} added to queue")

    def _worker(self):
        while True:
            try:
                video_id, task_type, job_id = self.queue.get()

                # Drain, do not run: a cancelled batch still has hundreds of
                # items sitting in this queue and every one of them must be
                # thrown away rather than processed.
                if task_type in self.cancelled_types:
                    self.active_jobs.pop(job_id, None)
                    self.queue.task_done()
                    continue

                self.current_job = job_id
                self._process_job(video_id, task_type, job_id)
                self.current_job = None
                self.queue.task_done()
            except Exception as e:
                logger.error(f"Worker error: {e}")
                time.sleep(1)

    def _process_job(self, video_id, task_type, job_id):
        self.active_jobs[job_id]["status"] = "processing"
        self._notify_listeners({"type": "job_status", "job": self.active_jobs[job_id]})
        
        db = SessionLocal()
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if not video:
                logger.error(f"Video {video_id} not found for job {job_id}")
                del self.active_jobs[job_id]
                return

            if task_type == "proxy":
                self._run_proxy_gen(db, video, job_id)
            elif task_type == "transcribe":
                self._run_transcription(db, video, job_id)
            
            db.commit()
        except Exception as e:
            logger.error(f"Error processing job {job_id}: {e}")
            if job_id in self.active_jobs:
                self.active_jobs[job_id]["status"] = "failed"
                self.active_jobs[job_id]["error"] = str(e)
                self._notify_listeners({"type": "job_status", "job": self.active_jobs[job_id]})
        finally:
            db.close()

    def _run_proxy_gen(self, db, video, job_id):
        from video_processor import generate_proxy
        import os
        from pathlib import Path

        # A proxy only means something for video. Project files, stills and
        # documents used to reach ffmpeg here and come back as a hard error
        # ("FFmpeg failed with return code 183") that sat in the Jobs panel
        # forever - the file was never a candidate in the first place.
        if video.media_type != "video":
            video.proxy_status = "not_applicable"
            video.proxy_error = None
            db.commit()
            self.active_jobs[job_id]["status"] = "skipped"
            self.active_jobs[job_id]["progress"] = 100
            self.active_jobs[job_id]["message"] = (
                f"Not a video file ({video.media_type or 'unknown'}) - nothing to proxy"
            )
            self._notify_listeners({
                "type": "job_complete", "job_id": job_id,
                "video_id": video.id, "status": "skipped",
            })
            return

        # Check if already completed
        if video.proxy_status == "completed" and video.proxy_path and os.path.exists(video.proxy_path):
            self.active_jobs[job_id]["status"] = "completed"
            self.active_jobs[job_id]["progress"] = 100
            return

        video.proxy_status = "processing"
        db.commit()

        try:
            # Proxy path
            # Keep this in step with main.py: MEDIA_ROOT is the one to set,
            # MEDIA_UPLOAD_DIR is kept for backwards compatibility.
            _default_root = r"E:\Shared" if os.name == "nt" else "/mnt/e/Shared"
            UPLOAD_ROOT = Path(
                os.environ.get("MEDIA_ROOT")
                or os.environ.get("MEDIA_UPLOAD_DIR")
                or _default_root
            )
            proxy_dir = UPLOAD_ROOT / "proxies"
            proxy_dir.mkdir(parents=True, exist_ok=True)
            proxy_filename = f"proxy_{video.id}_{os.path.splitext(video.filename)[0]}.mp4"
            proxy_path = proxy_dir / proxy_filename

            def progress_callback(p):
                self.active_jobs[job_id]["progress"] = p
                self._notify_listeners({"type": "job_progress", "job_id": job_id, "progress": p})

            def _track(proc):
                self.current_proc = proc

            try:
                generate_proxy(video.filepath, str(proxy_path), progress_callback,
                               on_start=_track)
            finally:
                self.current_proc = None

            # A killed encode is a cancellation, not a failure - do not leave
            # it sitting in the Jobs panel looking like something went wrong.
            if "proxy" in self.cancelled_types:
                video.proxy_status = "not_generated"
                video.proxy_error = None
                db.commit()
                self.active_jobs.pop(job_id, None)
                try:
                    os.remove(proxy_path)      # half-written file is useless
                except OSError:
                    pass
                return
            
            video.proxy_path = str(proxy_path)
            video.proxy_status = "completed"
            video.proxy_created_at = datetime.utcnow()
            video.proxy_error = None
            
            self.active_jobs[job_id]["status"] = "completed"
            self.active_jobs[job_id]["progress"] = 100
            self._notify_listeners({"type": "job_status", "job": self.active_jobs[job_id]})
            logger.info(f"Proxy generated for {video.filename}")
        except Exception as e:
            video.proxy_status = "failed"
            video.proxy_error = str(e)
            raise e

    def _get_whisper_model(self, name="small"):
        """Load the model ONCE and keep it.

        This used to run whisper.load_model() inside every job - reloading the
        model from disk and back onto the GPU 1,100+ times.
        """
        cached = getattr(self, "_whisper_model", None)
        if cached is not None and getattr(self, "_whisper_name", None) == name:
            return cached
        import whisper
        logger.info(f"Loading whisper model '{name}' (first use)")
        self._whisper_model = whisper.load_model(name)
        self._whisper_name = name
        return self._whisper_model

    @staticmethod
    def _has_audio(path):
        """Does this file actually contain an audio stream?"""
        import subprocess, json as _json
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams',
                 '-select_streams', 'a', path],
                capture_output=True, text=True, timeout=30).stdout
            return bool(_json.loads(out).get("streams"))
        except Exception:
            return True   # if we cannot tell, let whisper try

    def _run_transcription(self, db, video, job_id):
        import os
        # Check if already completed
        if video.transcription_status == "completed" and video.transcription:
             self.active_jobs[job_id]["status"] = "completed"
             self.active_jobs[job_id]["progress"] = 100
             return

        # Photos and other non-AV files have nothing to transcribe. Queueing
        # them produced a wall of "Failed to load audio" errors.
        if video.media_type not in ("video", "audio"):
            video.transcription_status = "not_applicable"
            db.commit()
            self.active_jobs[job_id]["status"] = "completed"
            self.active_jobs[job_id]["progress"] = 100
            self.active_jobs[job_id]["note"] = "not audio or video"
            self._notify_listeners({"type": "job_status", "job": self.active_jobs[job_id]})
            return

        # Plenty of drone clips are recorded with no audio track at all.
        if not self._has_audio(video.filepath):
            video.transcription_status = "no_audio"
            db.commit()
            self.active_jobs[job_id]["status"] = "completed"
            self.active_jobs[job_id]["progress"] = 100
            self.active_jobs[job_id]["note"] = "no audio track"
            self._notify_listeners({"type": "job_status", "job": self.active_jobs[job_id]})
            logger.info(f"Skipped {video.filename}: no audio track")
            return

        video.transcription_status = "processing"
        db.commit()

        try:
            model = self._get_whisper_model(os.environ.get("WHISPER_MODEL", "small"))

            # Transcription with segments
            result = model.transcribe(video.filepath, verbose=False)
            
            video.transcription = result["text"]
            video.transcription_status = "completed"
            
            # Save segments
            # Clear old segments if any
            db.query(TranscriptionSegment).filter(TranscriptionSegment.video_id == video.id).delete()
            
            for seg in result.get("segments", []):
                new_seg = TranscriptionSegment(
                    video_id=video.id,
                    text=seg["text"].strip(),
                    start_time=seg["start"],
                    end_time=seg["end"]
                )
                db.add(new_seg)

            # Tag straight from what was just said. Doing it here means the
            # library files itself as it transcribes, instead of waiting for
            # someone to run a script that nobody remembers to run.
            try:
                from transcript_tags import tags_for
                from database import Tag
                names = tags_for(result["text"])
                if names:
                    existing = {(t.name or "").lower() for t in video.tags}
                    for name in names:
                        if name.lower() in existing:
                            continue
                        tag = db.query(Tag).filter(
                            func.lower(Tag.name) == name.lower()).first()
                        if tag is None:
                            tag = Tag(name=name)
                            db.add(tag)
                            db.flush()
                        video.tags.append(tag)
                    logger.info(f"Auto-tagged {video.filename}: {', '.join(names)}")
            except Exception as e:
                logger.warning(f"Auto-tagging failed for {video.filename}: {e}")

            self.active_jobs[job_id]["status"] = "completed"
            self.active_jobs[job_id]["progress"] = 100
            self._notify_listeners({"type": "job_status", "job": self.active_jobs[job_id]})
            logger.info(f"Transcription complete for {video.filename}")
        except Exception as e:
            video.transcription_status = "failed"
            raise e

    def cancel_type(self, task_type):
        """Stop a whole batch: no more queued work, and kill what is running.

        Called by /api/proxies/cancel. Without the subprocess kill, pressing
        Stop still left a 4K transcode running to completion - which on a long
        clip is minutes of the machine being busy after you asked it to stop.
        """
        with self._cancel_lock:
            self.cancelled_types.add(task_type)

        # Empty the pending queue of this type, keeping anything else.
        keep, dropped = [], 0
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item[1] == task_type:
                self.active_jobs.pop(item[2], None)
                dropped += 1
            else:
                keep.append(item)
            self.queue.task_done()
        for item in keep:
            self.queue.put(item)

        # Kill the encode that is in flight right now.
        killed = False
        proc = self.current_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                killed = True
            except Exception as e:
                logger.warning(f"Could not stop the running job: {e}")

        with self._cancel_lock:
            self.cancelled_types.discard(task_type)

        logger.info(f"Cancelled {dropped} queued {task_type} job(s); "
                    f"running job killed: {killed}")
        self._notify_listeners({"type": "batch_cancelled", "task_type": task_type,
                                "dropped": dropped, "killed": killed})
        return {"dropped": dropped, "killed": killed}


# Global instance
job_manager = JobManager()
