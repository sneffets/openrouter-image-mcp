from __future__ import annotations

import httpx
import pytest

from openrouter_image_mcp.catalog import format_video_model_details, format_video_model_table
from openrouter_image_mcp.client import OpenRouterError
from openrouter_image_mcp.config import Settings
from openrouter_image_mcp.video import (
    VideoRequest,
    build_video_payload,
    list_job_records,
    load_job_record,
    save_job_record,
    wait_for_job,
)

from .conftest import build_client
from .video_models import FLUX_UPSCALE, KLING, SEEDANCE, VEO, WAN


def test_text_to_video_payload_normalises_choices():
    payload = build_video_payload(
        VideoRequest(
            model="google/veo-3.1",
            prompt=" a fox in the snow ",
            duration=8,
            resolution="4k",
            aspect_ratio="16:9",
            generate_audio=True,
            seed=7,
        ),
        VEO,
    )
    assert payload == {
        "model": "google/veo-3.1",
        "prompt": "a fox in the snow",
        "duration": 8,
        "resolution": "4K",
        "aspect_ratio": "16:9",
        "seed": 7,
        "generate_audio": True,
    }


def test_unsupported_duration_names_valid_values():
    with pytest.raises(ValueError, match="supported: 4, 6, 8"):
        build_video_payload(VideoRequest(model="google/veo-3.1", prompt="x", duration=5), VEO)


def test_frames_and_all_reference_kinds(tmp_path, png_bytes):
    frame = tmp_path / "first.png"
    frame.write_bytes(png_bytes)
    clip = tmp_path / "motion.mp4"
    clip.write_bytes(b"fake-mp4")
    song = tmp_path / "beat.mp3"
    song.write_bytes(b"ID3")

    payload = build_video_payload(
        VideoRequest(
            model="bytedance/seedance-2.5",
            prompt="the character dances to the beat",
            first_frame=str(frame),
            last_frame="https://example.com/last.png",
            reference_images=("https://example.com/style.png",),
            reference_videos=(str(clip),),
            reference_audios=(str(song),),
        ),
        SEEDANCE,
    )
    assert [f["frame_type"] for f in payload["frame_images"]] == ["first_frame", "last_frame"]
    assert payload["frame_images"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    references = payload["input_references"]
    assert [r["type"] for r in references] == ["video_url", "image_url", "audio_url"]
    assert references[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert references[1]["image_url"]["url"] == "https://example.com/style.png"
    assert references[2]["audio_url"]["url"].startswith("data:audio/mpeg;base64,")


def test_last_frame_rejected_when_model_only_takes_first():
    with pytest.raises(ValueError, match="does not accept a last frame"):
        build_video_payload(
            VideoRequest(model="alibaba/wan-3.0", prompt="x", last_frame="https://x/l.png"), WAN
        )


def test_prompt_is_optional_for_image_driven_requests():
    payload = build_video_payload(
        VideoRequest(model="alibaba/wan-3.0", first_frame="https://x/f.png"), WAN
    )
    assert "prompt" not in payload


def test_prompt_is_required_for_text_to_video():
    with pytest.raises(ValueError, match="prompt must not be empty"):
        build_video_payload(VideoRequest(model="google/veo-3.1", prompt="  "), VEO)


def test_negative_prompt_uses_each_models_spelling():
    veo = build_video_payload(
        VideoRequest(model="google/veo-3.1", prompt="x", negative_prompt="blurry"),
        VEO,
        ("google-vertex",),
    )
    assert veo["provider"] == {
        "options": {"google-vertex": {"parameters": {"negativePrompt": "blurry"}}}
    }

    kling = build_video_payload(
        VideoRequest(
            model="kwaivgi/kling-v3.0-pro",
            prompt="x",
            negative_prompt="blurry",
            provider_options={"cfg_scale": 0.7},
        ),
        KLING,
        ("atlas-cloud",),
    )
    assert kling["provider"]["options"]["atlas-cloud"]["parameters"] == {
        "cfg_scale": 0.7,
        "negative_prompt": "blurry",
    }


def test_unknown_provider_option_lists_allowed_ones():
    with pytest.raises(ValueError, match="allowed: negative_prompt, cfg_scale"):
        build_video_payload(
            VideoRequest(
                model="kwaivgi/kling-v3.0-pro",
                prompt="x",
                provider_options={"personGeneration": "allow"},
            ),
            KLING,
            ("atlas-cloud",),
        )


def test_provider_options_need_a_provider_slug():
    with pytest.raises(ValueError, match="Pass `provider`"):
        build_video_payload(
            VideoRequest(model="google/veo-3.1", prompt="x", negative_prompt="blurry"), VEO
        )


def test_seed_rejected_when_model_has_none_but_extra_body_bypasses():
    with pytest.raises(ValueError, match="does not support a seed"):
        build_video_payload(VideoRequest(model="kwaivgi/kling-v3.0-pro", prompt="x", seed=1), KLING)
    payload = build_video_payload(
        VideoRequest(model="kwaivgi/kling-v3.0-pro", prompt="x", extra={"seed": 1}), KLING
    )
    assert payload["seed"] == 1


def test_upscale_parameters_are_range_checked():
    request = VideoRequest(
        model="black-forest-labs/flux-video-upscale",
        reference_videos=("https://example.com/clip.mp4",),
        upscale_factor=4,
    )
    with pytest.raises(ValueError, match="between 1.5 and 3"):
        build_video_payload(request, FLUX_UPSCALE)

    request.upscale_factor = 2
    request.creativity = 1
    payload = build_video_payload(request, FLUX_UPSCALE)
    assert payload["upscale_factor"] == 2 and payload["creativity"] == 1
    assert payload["input_references"][0]["type"] == "video_url"


def test_upscale_factor_rejected_for_generation_models():
    with pytest.raises(ValueError, match="not an upscaling model"):
        build_video_payload(
            VideoRequest(model="google/veo-3.1", prompt="x", upscale_factor=2), VEO
        )


async def test_wait_for_job_polls_until_terminal():
    statuses = iter(
        [{"status": "pending"}, {"status": "in_progress"}, {"status": "completed"}]
    )

    class FakeClient:
        async def get_video_job(self, job_id):
            return next(statuses)

    seen = []

    async def progress(status, _elapsed):
        seen.append(status["status"])

    status, _ = await wait_for_job(
        FakeClient(), "job", interval=0, max_wait=60, on_progress=progress
    )
    assert status["status"] == "completed"
    assert seen == ["pending", "in_progress", "completed"]


async def test_wait_for_job_gives_up_after_max_wait():
    class FakeClient:
        calls = 0

        async def get_video_job(self, job_id):
            self.calls += 1
            return {"status": "pending"}

    client = FakeClient()
    status, _ = await wait_for_job(client, "job", interval=0, max_wait=0)
    assert status["status"] == "pending" and client.calls == 1


def test_job_records_roundtrip(settings: Settings):
    save_job_record(settings, {"job_id": "a/b", "created_at": "2026-01-01"})
    save_job_record(settings, {"job_id": "c", "created_at": "2026-01-02"})
    assert load_job_record(settings, "a/b")["job_id"] == "a/b"
    assert load_job_record(settings, "missing") is None
    assert [r["job_id"] for r in list_job_records(settings)] == ["c", "a/b"]


def test_video_model_table_and_details():
    table = format_video_model_table([VEO, KLING, SEEDANCE])
    assert "`google/veo-3.1`" in table
    assert "$0.2-$0.6/s" in table
    assert "$0.112-$0.168/s" in table
    assert "3-15s" in table
    assert "/M video tokens" in table
    details = format_video_model_details(VEO)
    assert "`negativePrompt`" in details and "first_frame, last_frame" in details


async def test_create_video_is_not_retried_on_server_errors(settings: Settings):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, json={"error": {"message": "bad gateway"}})

    async with build_client(settings, handler) as client:
        with pytest.raises(OpenRouterError, match="bad gateway"):
            await client.create_video({"model": "m", "prompt": "p"})
    assert calls["n"] == 1


async def test_download_video_sends_the_key_only_to_openrouter(settings: Settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        return httpx.Response(200, content=b"mp4", headers={"content-type": "video/mp4"})

    async with build_client(settings, handler) as client:
        data, media_type = await client.download_video("job", 0)
        await client.download_video("job", 0, "https://cdn.example.com/v.mp4")
    assert (data, media_type) == (b"mp4", "video/mp4")
    assert seen["openrouter.test"] == "Bearer test-key"
    assert seen["cdn.example.com"] is None
