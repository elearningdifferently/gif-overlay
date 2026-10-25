"""Flask web app: upload a GIF and an overlay image, download the result."""

from __future__ import annotations

import io
import math
import os

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
from archives import create_zip
from jobs import MAX_BATCH_FILES, JobQueueFull, JobRegistry
from uploads import DiskUploadRequest, FileTooLarge, copy_upload

from gif_overlay import (
    POSITIONS,
    GifOverlayError,
    OverlayOptions,
    analyze_gif,
    overlay_gif,
)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
MAX_BATCH_UPLOAD_MB = int(os.environ.get("MAX_BATCH_UPLOAD_MB", "1024"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "100"))
# Upper bound on width * height * frames to protect the server's memory.
MAX_PIXELS = int(os.environ.get("MAX_PIXELS", str(950_000_000)))

app = Flask(__name__)
app.request_class = DiskUploadRequest
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["MAX_BATCH_CONTENT_LENGTH"] = MAX_BATCH_UPLOAD_MB * 1024 * 1024
app.config["MAX_FILE_LENGTH"] = MAX_FILE_MB * 1024 * 1024
# 100 GIFs, one overlay and settings fit below Werkzeug's 1000-part default.
app.config["MAX_FORM_PARTS"] = 1000
job_registry = JobRegistry(MAX_PIXELS)


@app.before_request
def _request_limit():
    if request.endpoint == "create_batch":
        request.max_content_length = app.config["MAX_BATCH_CONTENT_LENGTH"]


def _upload(name):
    file = request.files.get(name)
    if file is None or not file.filename:
        raise GifOverlayError(f"Please upload a file for '{name}'.")
    return file


def _read_upload(name: str) -> bytes:
    data = io.BytesIO()
    copy_upload(_upload(name).stream, data, app.config["MAX_FILE_LENGTH"])
    return data.getvalue()


def _number(name: str, default: float, cast=float):
    raw = request.form.get(name, "")
    if raw.strip() == "":
        return default
    try:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(raw)
        return cast(value)
    except (ValueError, OverflowError) as exc:
        raise GifOverlayError(f"'{name}' must be a number.") from exc


@app.errorhandler(GifOverlayError)
def _handle_user_error(exc: GifOverlayError):
    return jsonify(error=str(exc)), 400


@app.errorhandler(RequestEntityTooLarge)
def _handle_too_large(exc):
    if isinstance(exc, FileTooLarge):
        return jsonify(error=f"Each uploaded file must be at most {app.config['MAX_FILE_LENGTH'] / 1024**2:g} MiB (GIFs and overlay)."), 413
    limit = request.max_content_length / 1024**2
    return jsonify(error=f"Upload too large: total request limit {limit:g} MiB, including all files and multipart form data. Multipart field/part limits also apply."), 413


@app.errorhandler(OSError)
def _handle_storage_error(_exc):
    return jsonify(error="Temporary upload storage unavailable. Please try again later."), 503


@app.get("/")
def index():
    return render_template(
        "index.html", positions=POSITIONS, max_upload_mb=MAX_UPLOAD_MB,
        max_batch_files=MAX_BATCH_FILES,
        max_batch_upload_mb=MAX_BATCH_UPLOAD_MB, max_file_mb=MAX_FILE_MB,
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/analyze")
def analyze():
    info = analyze_gif(_read_upload("gif"), max_pixels=MAX_PIXELS)
    return jsonify(info.to_dict())


def _overlay_options():
    opts = OverlayOptions(
        position=request.form.get("position", "center"),
        x=_number("x", 0, int),
        y=_number("y", 0, int),
        scale=_number("scale", 0.0),
        opacity=_number("opacity", 100.0),
        margin=_number("margin", 0, int),
        stretch_overlay=request.form.get("stretch_overlay", "").lower() in ("true", "on", "1"),
        crop_x=_number("crop_x", 0, int),
        crop_y=_number("crop_y", 0, int),
        crop_width=_number("crop_width", None, int),
        crop_height=_number("crop_height", None, int),
    )
    opts.validate()
    return opts


def _download_name(filename=None):
    if filename is None:
        filename = request.files["gif"].filename
    base_name = os.path.splitext(secure_filename(filename))[0] or "image"
    return f"{base_name}-overlay.gif"


@app.post("/jobs")
def create_job():
    gif = _upload("gif")
    overlay = _upload("overlay")
    opts = _overlay_options()
    try:
        status = job_registry.submit(
            gif.stream, overlay.stream, opts, _download_name(),
            max_file_bytes=app.config["MAX_FILE_LENGTH"],
        )
    except JobQueueFull:
        return jsonify(error="The job queue is full. Please try again later."), 503
    return jsonify(status), 202


@app.get("/jobs/<job_id>")
@app.get("/batches/<job_id>")
def job_status(job_id):
    status = job_registry.status(job_id)
    if status is None:
        return jsonify(error="Job not found or expired. Please submit it again."), 404
    response = jsonify(status)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/jobs/<job_id>/result")
@app.get("/batches/<job_id>/files/<int:file_id>/result")
def job_result(job_id, file_id=None):
    job, output = job_registry.result(job_id, file_id)
    if job is None:
        return jsonify(error="Job not found or expired. Please submit it again."), 404
    if output is None:
        return jsonify(**job.status()), 409
    try:
        response = send_file(
            output, mimetype="image/gif", as_attachment=True,
            download_name=job.download_name,
        )
    except Exception:
        output.close()
        raise
    response.call_on_close(output.close)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Gif-Frames"] = str(job.frame_count)
    response.headers["X-Gif-Size"] = job.size
    return response


@app.post("/batches")
def create_batch():
    uploads = request.files.getlist("gifs")
    if not 1 <= len(uploads) <= MAX_BATCH_FILES:
        raise GifOverlayError(f"Choose between 1 and {MAX_BATCH_FILES} GIF files.")
    overlay = _upload("overlay")
    opts = _overlay_options()
    files, names = [], set()
    for upload in uploads:
        if not upload.filename:
            raise GifOverlayError("Each GIF must have a filename.")
        name = _download_name(upload.filename)
        stem = name[:-4]
        suffix = 2
        while name.casefold() in names:
            name = f"{stem}-{suffix}.gif"
            suffix += 1
        names.add(name.casefold())
        files.append((upload.stream, name))
    try:
        status = job_registry.submit_batch(
            files, overlay.stream, opts, max_file_bytes=app.config["MAX_FILE_LENGTH"],
        )
    except JobQueueFull:
        return jsonify(error="The job queue is full. Please try again later."), 503
    return jsonify(status), 202


@app.get("/batches/<job_id>/archive")
def batch_archive(job_id):
    with job_registry.archive_sources(job_id) as (job, sources):
        if job is None or job.files is None:
            return jsonify(error="Batch not found or expired. Please submit it again."), 404
        if not sources:
            return jsonify(**job.status()), 409
        output = create_zip(sources)
    try:
        response = send_file(
            output, mimetype="application/zip", as_attachment=True,
            download_name="gifs-overlay.zip",
        )
    except Exception:
        output.close()
        raise
    response.call_on_close(output.close)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/overlay")
def overlay():
    gif_data = _read_upload("gif")
    overlay_data = _read_upload("overlay")
    opts = _overlay_options()
    result, info = overlay_gif(gif_data, overlay_data, opts, max_pixels=MAX_PIXELS)

    response = send_file(
        io.BytesIO(result),
        mimetype="image/gif",
        as_attachment=True,
        download_name=_download_name(),
    )
    response.headers["X-Gif-Frames"] = str(info.frame_count)
    response.headers["X-Gif-Size"] = f"{info.width}x{info.height}"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
