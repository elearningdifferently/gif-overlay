"""Deterministic contracts for ephemeral background rendering."""

import io
from pathlib import Path
from threading import Event
from uuid import UUID
from zipfile import ZipFile

import pytest
from PIL import Image

import app as app_module
import jobs as jobs_module
from gif_overlay import GifOverlayError
from jobs import JobRegistry
from test_app import upload_data
from test_gif_overlay import DURATIONS


@pytest.fixture
def registry(monkeypatch):
    registry = JobRegistry(app_module.MAX_PIXELS)
    monkeypatch.setattr(app_module, "job_registry", registry)
    yield registry
    registry.close()
    registry.pending.join()


@pytest.fixture
def client(registry):
    return app_module.app.test_client()


def test_enqueue_processing_fifo_and_download(client, registry, monkeypatch):
    started, release = Event(), Event()
    render = jobs_module.overlay_gif
    calls = []

    def blocked_render(gif, overlay, options, **kwargs):
        calls.append(options.x)
        started.set()
        assert release.wait(5)
        return render(gif, overlay, options, **kwargs)

    monkeypatch.setattr(jobs_module, "overlay_gif", blocked_render)
    try:
        response = client.post("/jobs", data=upload_data(x="1"))
        assert response.status_code == 202
        status = response.get_json()
        job_id = status["job_id"]
        assert str(UUID(job_id)) == job_id
        assert status["state"] == "queued"
        assert started.wait(5)
        active = client.get(f"/jobs/{job_id}")
        assert active.get_json()["state"] == "processing"
        assert active.headers["Cache-Control"] == "no-store"
        assert not active.get_json()["result_available"]
        assert client.get(f"/jobs/{job_id}/result").status_code == 409
        second = client.post("/jobs", data=upload_data(x="2")).get_json()["job_id"]
        assert client.get(f"/jobs/{second}").get_json()["state"] == "queued"
        directory = Path(registry.jobs[job_id].directory)
        assert (directory / "input.gif").exists()
    finally:
        release.set()
    registry.pending.join()
    assert calls == [1, 2]
    done = client.get(f"/jobs/{job_id}").get_json()
    assert done["state"] == "done"
    assert done["result_available"]
    assert done["updated_at"] >= done["created_at"]
    assert not (directory / "input.gif").exists()
    assert not (directory / "overlay").exists()
    response = client.get(f"/jobs/{job_id}/result")
    assert response.status_code == 200
    assert response.mimetype == "image/gif"
    assert "my_animation-overlay.gif" in response.headers["Content-Disposition"]
    assert response.headers["X-Gif-Frames"] == str(len(DURATIONS))
    assert Image.open(io.BytesIO(response.data)).n_frames == len(DURATIONS)
    response.close()


@pytest.mark.parametrize("failure,message", [
    (GifOverlayError("Invalid overlay image."), "Invalid overlay image."),
    (RuntimeError("/private/path: internal detail"), "GIF generation failed. Please try again."),
])
def test_error_and_expiry(client, registry, monkeypatch, failure, message):
    now = [1000]
    registry.clock = lambda: now[0]

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(jobs_module, "overlay_gif", fail)
    job_id = client.post("/jobs", data=upload_data()).get_json()["job_id"]
    registry.pending.join()
    directory = Path(registry.jobs[job_id].directory)
    status = client.get(f"/jobs/{job_id}").get_json()
    assert status["state"] == "error"
    assert status["error"] == message
    assert not status["result_available"]
    assert not list(directory.iterdir())
    response = client.get(f"/jobs/{job_id}/result")
    assert response.status_code == 409
    assert response.get_json()["error"] == message
    now[0] += registry.ttl
    for route in (f"/jobs/{job_id}", f"/jobs/{job_id}/result"):
        assert client.get(route).status_code == 404
    assert not directory.exists()
    assert job_id not in registry.jobs


def test_completed_expiry_keeps_open_download_valid(client, registry):
    now = [1000]
    registry.clock = lambda: now[0]
    job_id = client.post("/jobs", data=upload_data()).get_json()["job_id"]
    registry.pending.join()
    job, output = registry.result(job_id)
    directory = Path(job.directory)
    now[0] += registry.ttl
    registry.cleanup()
    assert not directory.exists()
    assert output.read().startswith(b"GIF")
    output.close()
    assert client.get(f"/jobs/{job_id}").status_code == 404


@pytest.mark.parametrize("suffix", ["", "/result"])
def test_unknown_job(client, suffix):
    response = client.get("/jobs/unknown" + suffix)
    assert response.status_code == 404
    assert "expired" in response.get_json()["error"]


@pytest.mark.parametrize("options", [{"opacity": "101"}, {"position": "wrong"}, {"scale": "nan"}])
def test_invalid_options_not_enqueued(client, registry, options):
    response = client.post("/jobs", data=upload_data(**options))
    assert response.status_code == 400
    assert "error" in response.get_json()
    assert not registry.jobs


def test_invalid_upload_and_size_limit(client, registry, monkeypatch):
    assert client.post("/jobs").status_code == 400
    data = upload_data()
    data["gif"] = (io.BytesIO(b""), "empty.gif")
    assert client.post("/jobs", data=data).status_code == 400
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 32)
    assert client.post("/jobs", data=upload_data()).status_code == 413
    assert not registry.jobs


def test_corrupt_image_fails_in_worker(client, registry):
    data = upload_data()
    data["gif"] = (io.BytesIO(b"not a GIF"), "base.gif")
    job_id = client.post("/jobs", data=data).get_json()["job_id"]
    registry.pending.join()
    assert client.get(f"/jobs/{job_id}").get_json()["state"] == "error"


def test_admission_limit_and_shutdown_cleanup(client, registry):
    registry.max_jobs = 1
    job_id = client.post("/jobs", data=upload_data()).get_json()["job_id"]
    registry.pending.join()
    directory = Path(registry.jobs[job_id].directory)
    response = client.post("/jobs", data=upload_data())
    assert response.status_code == 503
    assert "queue is full" in response.get_json()["error"]
    registry.close()
    assert not directory.exists()
    assert registry.status(job_id) is None


def test_download_name_is_sanitized(client, registry):
    data = upload_data()
    data["gif"] = (io.BytesIO(data["gif"][0].getvalue()), "../../evil name.gif")
    job_id = client.post("/jobs", data=data).get_json()["job_id"]
    registry.pending.join()
    response = client.get(f"/jobs/{job_id}/result")
    disposition = response.headers["Content-Disposition"]
    assert "/" not in disposition
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "evil_name-overlay.gif" in disposition
    response.close()


def batch_data(**options):
    data = upload_data(**options)
    content = data.pop("gif")[0].getvalue()
    data["gifs"] = [
        (io.BytesIO(content), "../../same name.gif"),
        (io.BytesIO(b"not a GIF"), "broken.gif"),
        (io.BytesIO(content), "same name.gif"),
    ]
    return data


def test_batch_progress_partial_failure_and_downloads(client, registry, monkeypatch):
    started, release = Event(), Event()
    render = jobs_module.overlay_gif
    calls = []

    def blocked_render(gif, overlay, options, **kwargs):
        calls.append(options)
        if len(calls) == 2:
            started.set()
            assert release.wait(5)
        return render(gif, overlay, options, **kwargs)

    monkeypatch.setattr(jobs_module, "overlay_gif", blocked_render)
    try:
        response = client.post("/batches", data=batch_data(
            position="bottom-right", margin="2", scale="25", opacity="50",
            crop_x="1", crop_width="40",
        ))
        assert response.status_code == 202
        queued = response.get_json()
        job_id = queued["job_id"]
        assert queued["state"] == "queued"
        assert queued["percentage"] == queued["completed"] == 0
        assert [file["state"] for file in queued["files"]] == ["queued"] * 3
        assert started.wait(5)
        active = client.get(f"/batches/{job_id}")
        assert active.headers["Cache-Control"] == "no-store"
        status = active.get_json()
        assert status["state"] == "processing"
        assert status["total"] == 3
        assert status["completed"] == 1
        assert status["percentage"] == 33
        assert status["progress"] == "1 of 3 completed"
        assert [file["state"] for file in status["files"]] == ["done", "processing", "queued"]
        assert client.get(f"/batches/{job_id}/archive").status_code == 409
        assert client.get(f"/batches/{job_id}/files/1/result").status_code == 409
    finally:
        release.set()
    registry.pending.join()
    status = client.get(f"/batches/{job_id}").get_json()
    assert status["state"] == "done"
    assert status["completed"] == 3
    assert status["percentage"] == 100
    assert status["succeeded"] == 2
    assert status["failed"] == 1
    assert status["result_available"]
    assert [file["state"] for file in status["files"]] == ["done", "error", "done"]
    assert status["files"][1]["error"]
    assert all(options is calls[0] for options in calls)
    assert calls[0].position == "bottom-right"
    assert calls[0].margin == 2
    assert calls[0].scale == 25
    assert calls[0].opacity == 50
    assert calls[0].crop_x == 1
    assert calls[0].crop_width == 40
    directory = Path(registry.jobs[job_id].directory)
    assert sorted(path.name for path in directory.iterdir()) == ["result-0.gif", "result-2.gif"]
    assert client.get(f"/jobs/{job_id}/result").status_code == 409
    assert client.get(f"/batches/{job_id}/files/3/result").status_code == 404
    downloads = {}
    for index in (0, 2):
        response = client.get(f"/batches/{job_id}/files/{index}/result")
        assert response.status_code == 200
        assert response.mimetype == "image/gif"
        assert response.headers["X-Gif-Size"] == "80x60"
        assert Image.open(io.BytesIO(response.data)).size == (40, 60)
        name = status["files"][index]["download_name"]
        assert name in response.headers["Content-Disposition"]
        downloads[name] = response.data
        response.close()
    assert set(downloads) == {"same_name-overlay.gif", "same_name-overlay-2.gif"}
    response = client.get(f"/batches/{job_id}/archive")
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert response.headers["Cache-Control"] == "no-store"
    with ZipFile(io.BytesIO(response.data)) as archive:
        assert set(archive.namelist()) == set(downloads)
        for name, data in downloads.items():
            assert archive.read(name) == data
    response.close()


@pytest.mark.parametrize("case", ["missing", "empty", "no-filename", "too-many", "overlay", "options"])
def test_batch_invalid_submission_is_not_enqueued(client, registry, case):
    data = batch_data()
    if case == "missing":
        del data["gifs"]
    elif case == "empty":
        data["gifs"] = [(io.BytesIO(b""), "empty.gif")]
    elif case == "no-filename":
        data["gifs"] = [(io.BytesIO(b"GIF"), "")]
    elif case == "too-many":
        data["gifs"] = [(io.BytesIO(b"GIF"), f"{i}.gif") for i in range(101)]
    elif case == "overlay":
        del data["overlay"]
    else:
        data["opacity"] = "101"
    assert client.post("/batches", data=data).status_code == 400
    assert not registry.jobs


def test_batch_all_failed_and_expiry(client, registry):
    now = [1000]
    registry.clock = lambda: now[0]
    data = batch_data()
    data["gifs"] = [(io.BytesIO(b"invalid"), "bad.gif")]
    job_id = client.post("/batches", data=data).get_json()["job_id"]
    registry.pending.join()
    status = client.get(f"/batches/{job_id}").get_json()
    assert status["state"] == "error"
    assert status["completed"] == status["failed"] == 1
    assert status["percentage"] == 100
    assert not status["result_available"]
    assert client.get(f"/batches/{job_id}/archive").status_code == 409
    directory = Path(registry.jobs[job_id].directory)
    assert not list(directory.iterdir())
    now[0] += registry.ttl
    for suffix in ("", "/archive", "/files/0/result"):
        assert client.get(f"/batches/{job_id}{suffix}").status_code == 404
    assert not directory.exists()


def test_batch_limits_and_unique_names(client, registry, monkeypatch):
    data = batch_data()
    content = data["gifs"][0][0].getvalue()
    data["gifs"] = [(io.BytesIO(content), name) for name in (
        "A.gif", "a.gif", "a-overlay-2.gif", "a.gif", "???",
    )]
    job_id = client.post("/batches", data=data).get_json()["job_id"]
    registry.pending.join()
    status = client.get(f"/batches/{job_id}").get_json()
    names = [file["download_name"] for file in status["files"]]
    assert len({name.casefold() for name in names}) == 5
    assert names[-1] == "image-overlay.gif"
    registry.max_jobs = 1
    assert client.post("/batches", data=batch_data()).status_code == 503
    monkeypatch.setitem(app_module.app.config, "MAX_BATCH_CONTENT_LENGTH", 32)
    assert client.post("/batches", data=batch_data()).status_code == 413


def test_batch_archive_sources_survive_expiry(client, registry):
    now = [1000]
    registry.clock = lambda: now[0]
    job_id = client.post("/batches", data=batch_data()).get_json()["job_id"]
    registry.pending.join()
    with registry.archive_sources(job_id) as (job, sources):
        now[0] += registry.ttl
        registry.cleanup()
        assert not Path(job.directory).exists()
        assert len(sources) == 2
        assert all(source.read().startswith(b"GIF") for _, source in sources)
    assert all(source.closed for _, source in sources)
    assert client.get("/batches/unknown/archive").status_code == 404


def test_single_job_is_not_a_batch_download(client, registry):
    job_id = client.post("/jobs", data=upload_data()).get_json()["job_id"]
    registry.pending.join()
    assert client.get(f"/batches/{job_id}/files/0/result").status_code == 404
    assert client.get(f"/batches/{job_id}/archive").status_code == 404
