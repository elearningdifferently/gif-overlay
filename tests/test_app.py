"""HTTP contracts for the editor, uploads, and download responses."""

import io
import json
import runpy
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from PIL import Image, ImageSequence
from werkzeug.test import EnvironBuilder

import app as app_module
from test_gif_overlay import DURATIONS, frames_of, make_gif, make_png, pixels


class EditorHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.scripts = []
        self.stylesheets = []
        self.config_chunks = []
        self.in_config = False
        self.config_count = 0
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            if attrs.get("src"):
                self.scripts.append(attrs["src"])
            self.in_config = attrs.get("id") == "editor-config"
            if self.in_config:
                self.config_count += 1
                assert attrs.get("type") == "application/json"
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs.get("href"))
        elif tag == "input":
            self.inputs[attrs.get("id")] = attrs

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_config = False

    def handle_data(self, data):
        if self.in_config:
            self.config_chunks.append(data)

    @property
    def config(self):
        assert self.config_count == 1
        return json.loads("".join(self.config_chunks))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    return app_module.app.test_client()


def upload_data(**options):
    return {
        "gif": (io.BytesIO(make_gif()), "my animation.gif"),
        "overlay": (io.BytesIO(make_png()), "logo.png"),
        **options,
    }


def assert_user_error(response, message, status=400):
    assert response.status_code == status
    assert response.mimetype == "application/json"
    assert message.lower() in response.get_json()["error"].lower()


@pytest.mark.parametrize("upload_mb,pixels", [(None, None), ("7", "12345")])
def test_configured_limits(monkeypatch, upload_mb, pixels):
    for name, value in (("MAX_UPLOAD_MB", upload_mb), ("MAX_PIXELS", pixels)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    configured = runpy.run_path(app_module.__file__)
    expected_upload_mb = 100 if upload_mb is None else int(upload_mb)
    expected_pixels = 950_000_000 if pixels is None else int(pixels)
    assert configured["MAX_UPLOAD_MB"] == expected_upload_mb
    assert configured["MAX_PIXELS"] == expected_pixels
    assert configured["app"].config["MAX_CONTENT_LENGTH"] == expected_upload_mb * 1024 * 1024


@pytest.mark.parametrize("route", ["/analyze", "/overlay"])
def test_default_upload_limit_error(monkeypatch, route):
    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)
    configured = runpy.run_path(app_module.__file__)
    response = configured["app"].test_client().post(
        route, data=b"x",
        environ_overrides={"CONTENT_LENGTH": str(100 * 1024 * 1024 + 1)},
        content_type="multipart/form-data; boundary=test",
    )
    assert response.status_code == 413
    assert "total request limit 100 MiB" in response.get_json()["error"]
    assert "multipart" in response.get_json()["error"]


def test_editor_config_and_static_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    page = EditorHTML(response.get_data(as_text=True))
    assert page.config == {
        "maxUploadMb": app_module.MAX_UPLOAD_MB,
        "maxBatchUploadMb": app_module.MAX_BATCH_UPLOAD_MB,
        "maxFileMb": app_module.MAX_FILE_MB,
        "maxBatchFiles": 100,
        "analyzeUrl": "/analyze",
        "overlayUrl": "/overlay",
        "jobsUrl": "/jobs",
        "batchesUrl": "/batches",
    }
    script_urls = [url for url in page.scripts if urlsplit(url).path.endswith("/editor.js")]
    style_urls = [url for url in page.stylesheets if urlsplit(url).path.endswith("/editor.css")]
    assert script_urls
    assert style_urls
    assert page.inputs["batch-mode"]["type"] == "checkbox"
    assert "checked" not in page.inputs["batch-mode"]
    assert "multiple" not in page.inputs["gif"]
    for url in script_urls + style_urls:
        asset = client.get(url)
        assert asset.status_code == 200
        assert asset.data


def test_editor_config_cannot_break_out_of_json_script(client, monkeypatch):
    injected = '</script><script>alert("unsafe")</script>&\''
    monkeypatch.setattr(app_module, "MAX_UPLOAD_MB", injected)
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert injected not in html
    assert EditorHTML(html).config["maxUploadMb"] == injected


def test_stretch_checkbox_defaults_to_unchecked(client):
    html = client.get("/").get_data(as_text=True)
    checkbox = EditorHTML(html).inputs["stretch_overlay"]
    assert checkbox["type"] == "checkbox"
    assert checkbox["name"] == "stretch_overlay"
    assert checkbox["value"] == "true"
    assert "checked" not in checkbox
    assert "Stretch overlay to fit full GIF" in html


def test_crop_controls_are_optional_pixel_inputs(client):
    html = client.get("/").get_data(as_text=True)
    inputs = EditorHTML(html).inputs
    for field in ("crop_x", "crop_y", "crop_width", "crop_height"):
        control = inputs[field]
        assert control["name"] == field
        assert control["type"] == "number"
        assert control["step"] == "1"
        assert "required" not in control
        assert control["aria-describedby"] == "crop-help"
        if field in ("crop_x", "crop_y"):
            assert control["min"] == control["value"] == "0"
        else:
            assert control["min"] == "1"
            assert "value" not in control
    assert "Pixel-based crop of the base and overlay together" in html


def test_crop_request_is_applied_to_download(client):
    original = client.post("/overlay", data=upload_data())
    response = client.post("/overlay", data=upload_data(
        crop_x="25", crop_y="15", crop_width="999", crop_height="30",
    ))
    assert response.status_code == 200
    assert response.mimetype == "image/gif"
    output_frames = frames_of(response.data)
    assert len(output_frames) == len(DURATIONS)
    for full, output in zip(frames_of(original.data), output_frames):
        assert output.size == (55, 30)
        assert pixels(output) == pixels(full.crop((25, 15, 80, 45)))


def test_blank_crop_request_keeps_output_unchanged(client):
    original = client.post("/overlay", data=upload_data())
    response = client.post("/overlay", data=upload_data(
        **{field: "" for field in ("crop_x", "crop_y", "crop_width", "crop_height")}
    ))
    assert response.status_code == 200
    assert response.data == original.data


@pytest.mark.parametrize("flag", [None, "false", "true", "on", "1"])
def test_overlay_stretch_request_option(client, flag):
    options = {} if flag is None else {"stretch_overlay": flag}
    data = upload_data(**options)
    data["overlay"] = (io.BytesIO(make_png(color=(0, 255, 0, 128))), "logo.png")
    response = client.post("/overlay", data=data)
    assert response.status_code == 200
    stretched = flag in ("true", "on", "1")
    size = (80, 60) if stretched else (20, 20)
    position = (0, 0) if stretched else (30, 20)
    green = Image.new("RGBA", size, (0, 255, 0, 128))
    output_frames = frames_of(response.data)
    assert len(output_frames) == len(DURATIONS)
    for source, output in zip(frames_of(make_gif()), output_frames):
        source.alpha_composite(green, position)
        assert pixels(output) == pixels(source)


def test_health_is_json(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_json() == {"status": "ok"}


def test_analyze_returns_complete_metadata(client):
    response = client.post(
        "/analyze", data={"gif": (io.BytesIO(make_gif(loop=3)), "animation.gif")}
    )
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_json() == {
        "width": 80,
        "height": 60,
        "frame_count": 5,
        "durations": DURATIONS,
        "total_duration": sum(DURATIONS),
        "loop": 3,
        "max_colors": 2,
        "has_transparency": False,
        "is_animated": True,
    }


def test_overlay_download_has_safe_filename_and_composited_frames(client):
    data = upload_data(position="bottom-right", margin="2")
    data["gif"] = (io.BytesIO(make_gif()), "../../my animation.gif")
    response = client.post("/overlay", data=data)
    assert response.status_code == 200
    assert response.mimetype == "image/gif"
    assert response.headers["Content-Disposition"] == (
        "attachment; filename=my_animation-overlay.gif"
    )
    assert response.headers["X-Gif-Frames"] == "5"
    assert response.headers["X-Gif-Size"] == "80x60"
    result = Image.open(io.BytesIO(response.data))
    assert result.format == "GIF"
    assert result.size == (80, 60)
    assert result.n_frames == 5
    assert result.info["loop"] == 0
    assert [frame.info["duration"] for frame in ImageSequence.Iterator(result)] == DURATIONS
    green = Image.new("RGBA", (20, 20), (0, 255, 0, 255))
    for source, output in zip(frames_of(make_gif()), frames_of(response.data)):
        source.alpha_composite(green, (58, 38))
        assert pixels(output) == pixels(source)


@pytest.mark.parametrize("route,field", [
    ("/analyze", "gif"), ("/overlay", "gif"), ("/overlay", "overlay"),
])
@pytest.mark.parametrize("case", ["missing", "no-filename", "empty", "invalid"])
def test_rejects_missing_empty_and_invalid_uploads(client, route, field, case):
    data = upload_data() if route == "/overlay" else {
        "gif": (io.BytesIO(make_gif()), "animation.gif")
    }
    if case == "missing":
        del data[field]
        message = f"'{field}'"
    elif case == "no-filename":
        data[field] = (io.BytesIO(b"data"), "")
        message = f"'{field}'"
    elif case == "empty":
        data[field] = (io.BytesIO(b""), "empty.gif")
        message = "empty"
    else:
        data[field] = (io.BytesIO(b"not an image"), "invalid.gif")
        message = "could not read"
    assert_user_error(client.post(route, data=data), message)


@pytest.mark.parametrize("route", ["/analyze", "/overlay"])
def test_rejects_non_gif_base_even_with_gif_extension(client, route):
    data = upload_data()
    data["gif"] = (io.BytesIO(make_png()), "pretend.gif")
    assert_user_error(client.post(route, data=data), "base image must be a GIF")


@pytest.mark.parametrize("field", [
    "x", "y", "margin", "scale", "opacity",
    "crop_x", "crop_y", "crop_width", "crop_height",
])
@pytest.mark.parametrize("value", ["not-a-number", "NaN", "inf", "-inf", "1e999"])
def test_rejects_invalid_numeric_options(client, field, value):
    response = client.post("/overlay", data=upload_data(**{field: value}))
    assert_user_error(response, f"'{field}' must be a number")


@pytest.mark.parametrize("options,message", [
    ({"position": "nowhere"}, "invalid position"),
    ({"scale": "-1"}, "scale"),
    ({"scale": "1001"}, "scale"),
    ({"opacity": "-1"}, "opacity"),
    ({"opacity": "101"}, "opacity"),
    ({"margin": "-1"}, "margin"),
])
def test_rejects_out_of_range_options(client, options, message):
    assert_user_error(client.post("/overlay", data=upload_data(**options)), message)


def test_blank_numeric_options_use_defaults(client):
    response = client.post(
        "/overlay",
        data=upload_data(**{field: "  " for field in ("x", "y", "margin", "scale", "opacity")}),
    )
    assert response.status_code == 200
    assert response.mimetype == "image/gif"


@pytest.mark.parametrize("route", ["/analyze", "/overlay"])
def test_request_limit_includes_multipart_overhead(client, monkeypatch, route):
    data = upload_data() if route == "/overlay" else {
        "gif": (io.BytesIO(make_gif()), "animation.gif")
    }
    payload_bytes = sum(len(value[0].getvalue()) for value in data.values())
    builder = EnvironBuilder(path=route, method="POST", data=data)
    try:
        environ = builder.get_environ()
        request_bytes = int(environ["CONTENT_LENGTH"])
        assert request_bytes > payload_bytes
        monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", request_bytes - 1)
        assert_user_error(client.open(environ), "upload too large", status=413)
    finally:
        builder.close()


def test_request_limit_applies_to_combined_uploads(client, monkeypatch):
    # Valid image decoders ignore trailing padding; each upload alone fits,
    # including its multipart envelope, while their combined request does not.
    gif = make_gif() + b"\0" * 4096
    overlay = make_png() + b"\0" * 4096
    limit = len(gif) + len(overlay) - 1
    for field, content in (("gif", gif), ("overlay", overlay)):
        builder = EnvironBuilder(
            method="POST", data={field: (io.BytesIO(content), "image.gif")}
        )
        try:
            assert int(builder.get_environ()["CONTENT_LENGTH"]) < limit
        finally:
            builder.close()
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", limit)
    data = {
        "gif": (io.BytesIO(gif), "animation.gif"),
        "overlay": (io.BytesIO(overlay), "logo.png"),
    }
    assert_user_error(client.post("/overlay", data=data), "upload too large", status=413)
