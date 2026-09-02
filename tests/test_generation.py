from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from openrouter_image_mcp.config import Settings
from openrouter_image_mcp.generation import ImageRequest, build_payload, generate

from .conftest import build_client


def test_provider_preferences_pin_when_fallbacks_disabled(settings: Settings):
    payload = build_payload(
        ImageRequest(
            prompt="a fox",
            model="google/gemini-3.1-flash-image",
            providers=("google-vertex",),
            allow_fallbacks=False,
        ),
        settings,
    )
    assert payload["provider"] == {
        "order": ["google-vertex"],
        "allow_fallbacks": False,
        "only": ["google-vertex"],
    }


def test_default_providers_from_settings_are_used(tmp_path: Path):
    settings = Settings(api_key="k", default_providers=("google-ai-studio",), output_dir=tmp_path)
    payload = build_payload(ImageRequest(prompt="a fox", model="m"), settings)
    assert payload["provider"]["order"] == ["google-ai-studio"]
    assert payload["provider"]["allow_fallbacks"] is True


def test_no_provider_block_when_routing_is_automatic(settings: Settings):
    assert "provider" not in build_payload(ImageRequest(prompt="a fox", model="m"), settings)


def test_optional_parameters_are_only_sent_when_set(settings: Settings):
    payload = build_payload(
        ImageRequest(prompt="a fox", model="m", aspect_ratio="16:9", n=2, seed=42), settings
    )
    assert payload["aspect_ratio"] == "16:9"
    assert payload["n"] == 2 and payload["seed"] == 42
    assert "quality" not in payload and "background" not in payload


def test_invalid_inputs_are_rejected(settings: Settings):
    with pytest.raises(ValueError, match="prompt"):
        build_payload(ImageRequest(prompt="  ", model="m"), settings)
    with pytest.raises(ValueError, match="n must be"):
        build_payload(ImageRequest(prompt="a", model="m", n=99), settings)
    with pytest.raises(ValueError, match="provider_sort"):
        build_payload(ImageRequest(prompt="a", model="m", provider_sort="cheapest"), settings)


async def test_generate_saves_file_and_sidecar(settings: Settings, png_b64):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images")
        return httpx.Response(
            200,
            json={
                "model": "google/gemini-3.1-flash-image",
                "provider": "Google Vertex",
                "data": [{"b64_json": png_b64, "media_type": "image/png"}],
                "usage": {"cost": 0.004},
            },
        )

    async with build_client(settings, handler) as client:
        result, saved = await generate(
            client,
            settings,
            ImageRequest(prompt="A red fox", model="google/gemini-3.1-flash-image"),
        )

    assert result.provider == "Google Vertex"
    assert len(saved) == 1
    assert saved[0].path.exists() and saved[0].path.parent == settings.output_dir
    assert "a-red-fox" in saved[0].path.name
    assert saved[0].size == (64, 48)
    assert "A red fox" in saved[0].sidecar.read_text(encoding="utf-8")


async def test_generate_downloads_remote_urls(settings: Settings, png_bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"})
        return httpx.Response(200, json={"data": [{"url": "https://cdn.test/a.png"}]})

    async with build_client(settings, handler) as client:
        _, saved = await generate(client, settings, ImageRequest(prompt="fox", model="m"))
    assert saved[0].path.read_bytes() == png_bytes
