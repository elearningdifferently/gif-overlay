"""Deterministic contracts for ephemeral background rendering."""

import io
from pathlib import Path
from threading import Event
from uuid import UUID

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
