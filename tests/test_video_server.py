from __future__ import annotations

import dataclasses
import json

import httpx

from openrouter_image_mcp import server as srv
from openrouter_image_mcp import video
from openrouter_image_mcp.config import Settings

from .conftest import build_client
from .video_models import VIDEO_MODELS

ENDPOINTS = {"data": {"endpoints": [{"tag": "google-vertex", "provider_name": "Google"}]}}
COMPLETED = {
    "status": "completed",
    "generation_id": "gen-1",
    "unsigned_urls": ["https://openrouter.test/api/v1/videos/job-1/content?index=0"],
    "usage": {"cost": 3.2},
}


class FakeOpenRouter:
    """Serves the video endpoints; job polls walk through ``statuses``, the last one sticks."""

    def __init__(self, *statuses: dict) -> None:
        self.statuses = list(statuses) or [COMPLETED]
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/videos/models"):
            return httpx.Response(200, json=VIDEO_MODELS)
        if path.endswith("/endpoints"):
            return httpx.Response(200, json=ENDPOINTS)
        if path.endswith("/content"):
            return httpx.Response(200, content=b"fake-mp4", headers={"content-type": "video/mp4"})
        if request.method == "POST" and path.endswith("/videos"):
            job = {"id": "job-1", "polling_url": "/api/v1/videos/job-1", "status": "pending"}
            return httpx.Response(202, json=job)
        if path.endswith("/videos/job-1"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={"id": "job-1", **status})
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})

    def posts(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def count(self, suffix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(suffix))


def install(settings: Settings, *statuses: dict) -> FakeOpenRouter:
    api = FakeOpenRouter(*statuses)
    srv.configure(settings, build_client(settings, api.handler))
    return api


async def test_list_video_models_shows_capabilities(settings: Settings):
    install(settings)
    text = await srv.list_video_models(query="kling")
    assert "`kwaivgi/kling-v3.0-pro`" in text and "3-15s" in text
    assert "veo" not in text.lower()


async def test_describe_video_model_lists_passthrough_and_providers(settings: Settings):
    install(settings)
    text = await srv.describe_video_model("google/veo-3.1")
    assert "`negativePrompt`" in text and "google-vertex" in text


async def test_generate_video_waits_downloads_and_records(settings: Settings, tmp_path, png_bytes):
    api = install(settings, {"status": "pending"}, {"status": "in_progress"}, COMPLETED)
    frame = tmp_path / "first.png"
    frame.write_bytes(png_bytes)

    content = await srv.generate_video(
        prompt="A fox runs through snow",
        model="google/veo-3.1",
        duration=8,
        resolution="1080p",
        first_frame=str(frame),
        negative_prompt="blurry",
    )
    text = content[0].text
    assert "Generated 1 video" in text and "$3.2000" in text

    videos = list(settings.output_dir.glob("*.mp4"))
    assert len(videos) == 1 and str(videos[0]) in text
    assert videos[0].read_bytes() == b"fake-mp4"
    sidecar = json.loads(videos[0].with_suffix(".mp4.json").read_text())
    assert sidecar["prompt"] == "A fox runs through snow"
    assert sidecar["generation_id"] == "gen-1"

    payload = api.posts()[0]
    assert payload["duration"] == 8 and payload["resolution"] == "1080p"
    assert payload["frame_images"][0]["frame_type"] == "first_frame"
    assert payload["provider"]["options"]["google-vertex"]["parameters"] == {
        "negativePrompt": "blurry"
    }
    download = [r for r in api.requests if r.url.path.endswith("/content")][0]
    assert download.headers["authorization"] == "Bearer test-key"
    assert video.load_job_record(settings, "job-1")["status"] == "completed"


async def test_invalid_parameters_never_submit_a_job(settings: Settings):
    api = install(settings)
    content = await srv.generate_video(prompt="x", model="google/veo-3.1", duration=5)
    assert "supported: 4, 6, 8" in content[0].text
    assert api.posts() == []


async def test_submit_without_waiting_then_resume(settings: Settings):
    api = install(settings, COMPLETED)
    content = await srv.generate_video(prompt="x", model="google/veo-3.1", wait=False)
    assert "`job-1`" in content[0].text and "check_video_job" in content[0].text
    assert api.count("/videos/job-1") == 0

    first = await srv.check_video_job("job-1")
    assert "Generated 1 video" in first[0].text
    again = await srv.check_video_job("job-1")
    assert "Already downloaded" in again[0].text
    assert api.count("/content") == 1


async def test_long_running_job_returns_its_id(settings: Settings):
    quick = dataclasses.replace(settings, video_max_wait=0.0)
    install(quick, {"status": "in_progress"})
    content = await srv.generate_video(prompt="x", model="google/veo-3.1")
    assert "still `in_progress`" in content[0].text
    assert 'check_video_job(job_id="job-1")' in content[0].text

    listing = await srv.list_generated_videos()
    assert "`job-1`" in listing and "in_progress" in listing


async def test_failed_job_reports_the_reason(settings: Settings):
    install(settings, {"status": "failed", "error": "Content policy violation"})
    content = await srv.generate_video(prompt="x", model="google/veo-3.1")
    assert "ended as `failed`: Content policy violation" in content[0].text
    assert not list(settings.output_dir.glob("*.mp4"))


async def test_edit_video_sends_the_source_clip_first(settings: Settings, tmp_path):
    api = install(settings, COMPLETED)
    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"source-clip")
    content = await srv.edit_video(
        source_video=str(clip),
        prompt="make it night",
        model="black-forest-labs/flux-video-edit",
    )
    assert "Generated 1 video" in content[0].text
    reference = api.posts()[0]["input_references"][0]
    assert reference["type"] == "video_url"
    assert reference["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert list(settings.output_dir.glob("*-edit.mp4"))


async def test_generate_video_without_model_lists_video_models(settings: Settings):
    install(settings)
    content = await srv.generate_video(prompt="x")
    assert "No model was selected" in content[0].text
    assert "OPENROUTER_VIDEO_MODEL" in content[0].text
    assert "`google/veo-3.1`" in content[0].text


async def test_status_mentions_video_settings(settings: Settings):
    install(settings)
    text = await srv.openrouter_status()
    assert "default video model" in text and "ffmpeg" in text
