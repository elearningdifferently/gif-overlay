"""Ephemeral, process-local GIF jobs; run with one web worker and one instance."""

from __future__ import annotations

import atexit
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Queue
import shutil
import tempfile
from threading import Event, Lock, Thread
import time
from uuid import uuid4

from gif_overlay import GifOverlayError, OverlayOptions, overlay_gif

MAX_BATCH_FILES = 100


@dataclass
class BatchFile:
    gif_path: Path
    download_name: str
    state: str = "queued"
    progress: str = "Waiting in queue."
    output_path: Path | None = None
    error: str | None = None
    frame_count: int | None = None
    size: str | None = None

    def status(self):
        return {
            "download_name": self.download_name,
            "state": self.state,
            "progress": self.progress,
            "error": self.error,
            "result_available": self.state == "done",
        }


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
    files: list[BatchFile] | None = None

    def status(self):
        status = {
            "job_id": self.id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress,
            "error": self.error,
            "result_available": self.state == "done",
        }
        if self.files is not None:
            completed = sum(file.state in ("done", "error") for file in self.files)
            succeeded = sum(file.state == "done" for file in self.files)
            status.update(
                files=[dict(file.status(), file_id=index) for index, file in enumerate(self.files)],
                total=len(self.files), completed=completed, succeeded=succeeded,
                failed=completed - succeeded,
                percentage=completed * 100 // len(self.files),
                progress=f"{completed} of {len(self.files)} completed",
                result_available=self.state == "done" and succeeded > 0,
            )
        return status


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
        return self._submit([(gif_data, download_name)], overlay_data, options, batch=False)

    def submit_batch(self, files, overlay_data, options):
        if not 1 <= len(files) <= MAX_BATCH_FILES:
            raise GifOverlayError(f"Choose between 1 and {MAX_BATCH_FILES} GIF files.")
        return self._submit(files, overlay_data, options, batch=True)

    def _submit(self, files, overlay_data, options, *, batch):
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
                    options, files[0][1],
                )
                if batch:
                    job.files = []
                    for index, (data, name) in enumerate(files):
                        path = Path(directory) / f"input-{index}.gif"
                        path.write_bytes(data)
                        job.files.append(BatchFile(path, name))
                else:
                    job.gif_path.write_bytes(files[0][0])
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

    def result(self, job_id, file_id=None):
        self.cleanup()
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None, None
            if file_id is not None:
                if job.files is None or not 0 <= file_id < len(job.files):
                    return None, None
                job = job.files[file_id]
            elif job.files is not None:
                return replace(job), None
            # Open under the same lock as expiry; an active download keeps its
            # descriptor even if cleanup subsequently unlinks the temporary file.
            output = job.output_path.open("rb") if job.state == "done" else None
            return replace(job), output

    @contextmanager
    def archive_sources(self, job_id):
        self.cleanup()
        with ExitStack() as stack:
            with self.lock:
                job = self.jobs.get(job_id)
                sources = []
                if job is not None and job.files is not None and job.state == "done":
                    sources = [
                        (file.download_name, stack.enter_context(file.output_path.open("rb")))
                        for file in job.files if file.state == "done"
                    ]
                snapshot = replace(job) if job else None
            yield snapshot, sources

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
                for index, file in enumerate(job.files if job.files is not None else [job]):
                    with self.lock:
                        if self.stopped.is_set():
                            break
                        file.state = "processing"
                        file.progress = "Rendering GIF."
                        job.updated_at = self.clock()
                    try:
                        result, info = overlay_gif(
                            file.gif_path.read_bytes(), job.overlay_path.read_bytes(),
                            job.options, max_pixels=self.max_pixels,
                        )
                        with self.lock:
                            if self.stopped.is_set():
                                break
                            file.output_path = Path(job.directory) / f"result-{index}.gif"
                            file.output_path.write_bytes(result)
                            file.frame_count = info.frame_count
                            file.size = f"{info.width}x{info.height}"
                            file.state = "done"
                            file.progress = "Export complete."
                    except Exception as exc:
                        with self.lock:
                            file.state = "error"
                            file.progress = "Export failed."
                            file.error = (str(exc) if isinstance(exc, GifOverlayError)
                                          else "GIF generation failed. Please try again.")
                            if file.output_path is not None:
                                file.output_path.unlink(missing_ok=True)
                                file.output_path = None
                    finally:
                        result = None
                        with self.lock:
                            file.gif_path.unlink(missing_ok=True)
                            job.updated_at = self.clock()
                with self.lock:
                    if job.files is not None:
                        job.state = "done" if any(file.state == "done" for file in job.files) else "error"
                        if job.state == "error":
                            job.error = "No GIFs could be processed. See individual file errors."
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
