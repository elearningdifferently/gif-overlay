"""Ephemeral, process-local GIF jobs; run with one web worker and one instance."""

from __future__ import annotations

import atexit
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Queue
import shutil
import tempfile
from threading import Event, Lock, Thread
import time
from uuid import uuid4

from gif_overlay import GifOverlayError, OverlayOptions, overlay_gif


@dataclass
class Job:
    id: str
    created_at: float
    updated_at: float
    directory: str
    gif_path: Path
    overlay_path: Path
    options: OverlayOptions
    download_name: str
    state: str = "queued"
    progress: str = "Waiting in queue."
    output_path: Path | None = None
    error: str | None = None
    frame_count: int | None = None
    size: str | None = None

    def status(self):
        return {
            "job_id": self.id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress,
            "error": self.error,
            "result_available": self.state == "done",
        }


class JobQueueFull(Exception):
    pass


class JobRegistry:
    def __init__(self, max_pixels, *, ttl=15 * 60, max_jobs=16, clock=time.time):
        self.max_pixels = max_pixels
        self.ttl = ttl
        self.max_jobs = max_jobs
        self.clock = clock
        self.jobs = {}
        self.lock = Lock()
        self.pending = Queue()
        self.stopped = Event()
        self.started = False

    def _start(self):
        # Start lazily, after Gunicorn forks; never start threads at import time.
        if not self.started:
            self.started = True
            atexit.register(self.close)
            Thread(target=self._work, name="gif-renderer", daemon=True).start()
            Thread(target=self._reap, name="gif-cleanup", daemon=True).start()

    def submit(self, gif_data, overlay_data, options, download_name):
        self.cleanup()
        with self.lock:
            if self.stopped.is_set() or len(self.jobs) >= self.max_jobs:
                raise JobQueueFull()
            directory = tempfile.mkdtemp(prefix="gif-overlay-")
            try:
                now = self.clock()
                job = Job(
                    str(uuid4()), now, now, directory,
                    Path(directory) / "input.gif", Path(directory) / "overlay",
                    options, download_name,
                )
                job.gif_path.write_bytes(gif_data)
                job.overlay_path.write_bytes(overlay_data)
                self._start()
                self.jobs[job.id] = job
                status = job.status()
                self.pending.put(job)
                return status
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def status(self, job_id):
        self.cleanup()
        with self.lock:
            job = self.jobs.get(job_id)
            return job.status() if job else None

    def result(self, job_id):
        self.cleanup()
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None, None
            # Open under the same lock as expiry; an active download keeps its
            # descriptor even if cleanup subsequently unlinks the temporary file.
            output = job.output_path.open("rb") if job.state == "done" else None
            return replace(job), output

    def _work(self):
        while True:
            job = self.pending.get()
            try:
                if job is None:
                    return
                with self.lock:
                    if self.stopped.is_set():
                        continue
                    job.state = "processing"
                    job.progress = "Rendering GIF."
                    job.updated_at = self.clock()
                try:
                    result, info = overlay_gif(
                        job.gif_path.read_bytes(), job.overlay_path.read_bytes(),
                        job.options, max_pixels=self.max_pixels,
                    )
                    with self.lock:
                        if self.stopped.is_set():
                            continue
                        job.output_path = Path(job.directory) / "result.gif"
                        job.output_path.write_bytes(result)
                        job.frame_count = info.frame_count
                        job.size = f"{info.width}x{info.height}"
                        job.state = "done"
                        job.progress = "Export complete."
                except Exception as exc:
                    with self.lock:
                        job.state = "error"
                        job.progress = "Export failed."
                        job.error = (str(exc) if isinstance(exc, GifOverlayError)
                                     else "GIF generation failed. Please try again.")
                        if job.output_path is not None:
                            job.output_path.unlink(missing_ok=True)
                            job.output_path = None
                finally:
                    result = None
                    with self.lock:
                        job.gif_path.unlink(missing_ok=True)
                        job.overlay_path.unlink(missing_ok=True)
                        job.updated_at = self.clock()
            finally:
                self.pending.task_done()

    def cleanup(self):
        with self.lock:
            now = self.clock()
            for job_id, job in list(self.jobs.items()):
                if job.state in ("done", "error") and now - job.updated_at >= self.ttl:
                    job.state = "expired"
                    shutil.rmtree(job.directory, ignore_errors=True)
                    del self.jobs[job_id]

    def _reap(self):
        while not self.stopped.wait(30):
            self.cleanup()

    def close(self):
        with self.lock:
            if self.stopped.is_set():
                return
            self.stopped.set()
            for job in self.jobs.values():
                shutil.rmtree(job.directory, ignore_errors=True)
            self.jobs.clear()
            if self.started:
                self.pending.put(None)
