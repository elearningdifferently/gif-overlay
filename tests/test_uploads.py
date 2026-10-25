"""Small fixtures exercise real multipart limits and independent disk staging."""

import io
from pathlib import Path
import runpy
from threading import Event, Thread

import pytest
from werkzeug.test import EnvironBuilder

import app as app_module
import jobs as jobs_module
import uploads
from gif_overlay import GifOverlayError, OverlayOptions
from jobs import JobQueueFull, JobRegistry


@pytest.fixture
def registry(monkeypatch, tmp_path):
    registry = JobRegistry(app_module.MAX_PIXELS)
    # Keep the worker idle so tests can inspect independent staged files.
    monkeypatch.setattr(registry, "_start", lambda: None)
    monkeypatch.setattr(app_module, "job_registry", registry)
    original = jobs_module.tempfile.mkdtemp
    monkeypatch.setattr(jobs_module.tempfile, "mkdtemp",
                        lambda **kwargs: original(dir=tmp_path, **kwargs))
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 2048)
    monkeypatch.setitem(app_module.app.config, "MAX_BATCH_CONTENT_LENGTH", 16384)
    monkeypatch.setitem(app_module.app.config, "MAX_FILE_LENGTH", 4096)
    yield registry
    registry.close()
    assert not list(tmp_path.iterdir())


@pytest.fixture
def client(registry):
    return app_module.app.test_client()


def data(base_size=1024, overlay_size=100, count=1):
    return {
        "gifs": [(io.BytesIO(b"g" * base_size), f"{i}.gif") for i in range(count)],
        "overlay": (io.BytesIO(b"o" * overlay_size), "overlay.png"),
    }


@pytest.mark.parametrize("base_size", [4095, 4096, 4097])
def test_per_file_boundary(client, registry, base_size):
    response = client.post("/batches", data=data(base_size))
    assert response.status_code == (413 if base_size > 4096 else 202)
    if base_size > 4096:
        assert "Each uploaded file" in response.json["error"]
        assert not registry.jobs
    else:
        job = registry.jobs[response.json["job_id"]]
        assert job.files[0].gif_path.stat().st_size == base_size


def test_overlay_limit_is_independent(client, registry):
    response = client.post("/batches", data=data(100, 4097))
    assert response.status_code == 413
    assert "overlay" in response.json["error"]
    assert not registry.jobs


def test_batch_exceeds_old_single_budget(client, registry):
    response = client.post("/batches", data=data(3000, 3000, count=3))
    assert response.status_code == 202
    job = registry.jobs[response.json["job_id"]]
    assert len(job.files) == 3
    assert job.overlay_path.read_bytes() == b"o" * 3000
    assert not registry.staging


@pytest.mark.parametrize("route", ["/analyze", "/jobs", "/overlay"])
def test_single_routes_keep_small_budget(client, route):
    response = client.post(route, data={
        "gif": (io.BytesIO(b"g" * 2200), "base.gif"),
        "overlay": (io.BytesIO(b"o"), "overlay.png"),
    })
    assert response.status_code == 413
    assert "total request" in response.json["error"]


@pytest.mark.parametrize("route,field", [
    ("/analyze", "gif"), ("/overlay", "overlay"), ("/jobs", "gif"),
])
def test_single_routes_also_enforce_per_file_cap(client, monkeypatch, route, field):
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 16384)
    payload = {
        "gif": (io.BytesIO(b"g"), "base.gif"),
        "overlay": (io.BytesIO(b"o"), "overlay.png"),
    }
    payload[field] = (io.BytesIO(b"x" * 4097), "oversize.gif")
    response = client.post(route, data=payload)
    assert response.status_code == 413
    assert "Each uploaded file" in response.json["error"]


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("no_length", [False, True])
def test_total_includes_overhead(client, monkeypatch, delta, no_length):
    builder = EnvironBuilder(path="/batches", method="POST", data=data())
    try:
        environ = builder.get_environ()
        length = int(environ["CONTENT_LENGTH"])
        assert length > 1124
        monkeypatch.setitem(app_module.app.config, "MAX_BATCH_CONTENT_LENGTH", length + delta)
        if no_length:
            environ.pop("CONTENT_LENGTH")
            environ["wsgi.input_terminated"] = True
        response = client.open(environ)
        assert response.status_code == (413 if delta < 0 else 202)
    finally:
        builder.close()


@pytest.mark.parametrize("part_length", [None, "0", "1", "99999999"])
def test_part_lengths_and_mime_are_not_trusted(client, part_length):
    header = "" if part_length is None else f"Content-Length: {part_length}\r\n"
    body = (
        b'--test\r\nContent-Disposition: form-data; name="gifs"; filename="a.gif"\r\n'
        b"Content-Type: text/plain\r\n" + header.encode() + b"\r\n" + b"x" * 4097 +
        b'\r\n--test\r\nContent-Disposition: form-data; name="overlay"; filename="a.png"'
        b"\r\n\r\no\r\n--test--\r\n"
    )
    response = client.post("/batches", data=body,
                           content_type="multipart/form-data; boundary=test")
    assert response.status_code == 413
    assert "Each uploaded file" in response.json["error"]


def test_one_hundred_files_plus_overlay_and_settings(client, registry, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "MAX_BATCH_CONTENT_LENGTH", 100000)
    response = client.post("/batches", data={**data(1, 1, count=100), "opacity": "50"})
    assert response.status_code == 202
    assert len(registry.jobs[response.json["job_id"]].files) == 100


def test_multipart_streams_are_disk_backed_and_closed(client, monkeypatch):
    streams = []
    original = uploads.UploadStream.__init__

    def capture(self, limit):
        original(self, limit)
        streams.append(self)
        assert self.file.fileno() >= 0

    def bounded_read(self, size=-1):
        assert 0 < size <= uploads.CHUNK_SIZE
        return self.file.read(size)

    monkeypatch.setattr(uploads.UploadStream, "__init__", capture)
    monkeypatch.setattr(uploads.UploadStream, "read", bounded_read, raising=False)
    assert client.post("/batches", data=data()).status_code == 202
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("failure", ["oversize", "options", "empty", "malformed"])
def test_parser_failure_closes_all_streams(client, registry, monkeypatch, failure):
    streams = []
    original = uploads.UploadStream.__init__

    def capture(self, limit):
        original(self, limit)
        streams.append(self)

    monkeypatch.setattr(uploads.UploadStream, "__init__", capture)
    payload = data(100, 4097 if failure == "oversize" else 100)
    if failure == "options":
        payload["opacity"] = "101"
    if failure == "empty":
        payload["gifs"].append((io.BytesIO(), "empty.gif"))
    if failure == "malformed":
        response = client.post(
            "/batches", data=b'--x\r\nContent-Disposition: form-data; name="gifs"; filename="a.gif"\r\n\r\npartial',
            content_type="multipart/form-data; boundary=x",
        )
    else:
        response = client.post("/batches", data=payload)
    assert response.status_code in (400, 413)
    assert streams and all(stream.closed for stream in streams)
    assert not registry.jobs
    assert not registry.staging


class BoundedStream(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= uploads.CHUNK_SIZE
        return super().read(size)


def submit(registry, source=None, overlay=None):
    return registry.submit_batch(
        [(source if source is not None else BoundedStream(b"base"), "base.gif")],
        overlay if overlay is not None else BoundedStream(b"overlay"),
        OverlayOptions(), max_file_bytes=200000,
    )


def test_staging_is_chunked_private_and_request_independent(registry):
    source, overlay = BoundedStream(b"x" * 150000), BoundedStream(b"logo")
    status = submit(registry, source, overlay)
    source.close()
    overlay.close()
    job = registry.jobs[status["job_id"]]
    assert Path(job.directory).stat().st_mode & 0o777 == 0o700
    assert job.files[0].gif_path.read_bytes() == b"x" * 150000
    assert job.overlay_path.read_bytes() == b"logo"


@pytest.mark.parametrize("failure", ["empty", "oversize", "read", "write", "mkdir"])
def test_staging_failures_release_capacity_and_remove_directory(registry, tmp_path, monkeypatch, failure):
    class BrokenStream(BoundedStream):
        def read(self, size=-1):
            raise OSError("private disk detail")

    source = BoundedStream(b"")
    error = GifOverlayError
    if failure == "oversize":
        source, error = BoundedStream(b"x" * 200001), uploads.FileTooLarge
    elif failure == "read":
        source, error = BrokenStream(), OSError
    elif failure in ("write", "mkdir"):
        source, error = BoundedStream(b"base"), OSError

        def fail(*args, **kwargs):
            raise OSError("disk full")

        if failure == "write":
            monkeypatch.setattr(Path, "open", fail)
        else:
            monkeypatch.setattr(jobs_module.tempfile, "mkdtemp", fail)
    # Failure occurs after a valid base has already been staged.
    with pytest.raises(error):
        submit(registry, overlay=source)
    assert not registry.jobs
    assert not registry.staging
    assert not list(tmp_path.iterdir())


def test_staging_reservation_race_and_shutdown(registry, tmp_path):
    entered, release, finished = Event(), Event(), Event()
    errors = []
    registry.max_jobs = 1

    class BlockingStream(BoundedStream):
        def read(self, size=-1):
            entered.set()
            assert release.wait(5)
            return super().read(size)

    def stage():
        try:
            submit(registry, BlockingStream(b"base"))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=stage)
    thread.start()
    try:
        assert entered.wait(5)
        assert registry.status("unknown") is None
        registry.cleanup()
        with pytest.raises(JobQueueFull):
            submit(registry)
        assert len(registry.staging) == 1
        registry.close()
    finally:
        release.set()
        thread.join(5)
    assert finished.is_set()
    assert len(errors) == 1 and isinstance(errors[0], (JobQueueFull, OSError))
    assert not registry.jobs
    assert not registry.staging
    assert not list(tmp_path.iterdir())


def test_staging_does_not_block_downloads_or_lose_capacity(registry):
    first = submit(registry)["job_id"]
    job = registry.jobs[first]
    output = Path(job.directory) / "result.gif"
    output.write_bytes(b"GIF")
    job.files[0].output_path = output
    job.files[0].state = job.state = "done"
    registry.max_jobs = 2
    entered, release = Event(), Event()
    results = []

    class BlockingStream(BoundedStream):
        def read(self, size=-1):
            entered.set()
            assert release.wait(5)
            return super().read(size)

    thread = Thread(target=lambda: results.append(submit(registry, BlockingStream(b"base"))))
    thread.start()
    try:
        assert entered.wait(5)
        assert registry.status(first)["state"] == "done"
        with registry.result(first, 0)[1] as download:
            assert download.read() == b"GIF"
        with registry.archive_sources(first) as (_, sources):
            assert sources[0][1].read() == b"GIF"
        with pytest.raises(JobQueueFull):
            submit(registry)
    finally:
        release.set()
        thread.join(5)
    assert len(results) == 1
    assert len(registry.jobs) == 2
    assert not registry.staging


def test_disk_error_returns_safe_json(client, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("/private/path: disk full")

    monkeypatch.setattr(uploads.tempfile, "TemporaryFile", fail)
    response = client.post("/batches", data=data())
    assert response.status_code == 503
    assert "storage unavailable" in response.json["error"]
    assert "/private" not in response.json["error"]


def test_independent_environment_limits(monkeypatch):
    for name, value in (("MAX_UPLOAD_MB", "7"), ("MAX_BATCH_UPLOAD_MB", "11"),
                        ("MAX_FILE_MB", "3")):
        monkeypatch.setenv(name, value)
    configured = runpy.run_path(app_module.__file__)
    config = configured["app"].config
    assert config["MAX_CONTENT_LENGTH"] == 7 * 1024**2
    assert config["MAX_BATCH_CONTENT_LENGTH"] == 11 * 1024**2
    assert config["MAX_FILE_LENGTH"] == 3 * 1024**2
