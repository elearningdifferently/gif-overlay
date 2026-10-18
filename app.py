"""Flask web app: upload a GIF and an overlay image, download the result."""

from __future__ import annotations

import io
import math
import os

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from gif_overlay import (
    POSITIONS,
    GifOverlayError,
    OverlayOptions,
    analyze_gif,
    overlay_gif,
)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
# Upper bound on width * height * frames to protect the server's memory.
MAX_PIXELS = int(os.environ.get("MAX_PIXELS", str(100_000_000)))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def _read_upload(name: str) -> bytes:
    file = request.files.get(name)
    if file is None or not file.filename:
        raise GifOverlayError(f"Please upload a file for '{name}'.")
    data = file.read()
    if not data:
        raise GifOverlayError(f"The uploaded '{name}' file is empty.")
    return data


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
def _handle_too_large(_exc):
    return jsonify(error=f"Upload too large (limit {MAX_UPLOAD_MB} MB)."), 413


@app.get("/")
def index():
    return render_template("index.html", positions=POSITIONS, max_upload_mb=MAX_UPLOAD_MB)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/analyze")
def analyze():
    info = analyze_gif(_read_upload("gif"), max_pixels=MAX_PIXELS)
    return jsonify(info.to_dict())


@app.post("/overlay")
def overlay():
    gif_file = request.files.get("gif")
    gif_data = _read_upload("gif")
    overlay_data = _read_upload("overlay")
    opts = OverlayOptions(
        position=request.form.get("position", "center"),
        x=_number("x", 0, int),
        y=_number("y", 0, int),
        scale=_number("scale", 0.0),
        opacity=_number("opacity", 100.0),
        margin=_number("margin", 0, int),
    )
    result, info = overlay_gif(gif_data, overlay_data, opts, max_pixels=MAX_PIXELS)

    base_name = os.path.splitext(secure_filename(gif_file.filename))[0] or "image"
    response = send_file(
        io.BytesIO(result),
        mimetype="image/gif",
        as_attachment=True,
        download_name=f"{base_name}-overlay.gif",
    )
    response.headers["X-Gif-Frames"] = str(info.frame_count)
    response.headers["X-Gif-Size"] = f"{info.width}x{info.height}"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
