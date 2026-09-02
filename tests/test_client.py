from __future__ import annotations

import httpx
import pytest

from openrouter_image_mcp.client import OpenRouterError, to_chat_payload
from openrouter_image_mcp.config import Settings

from .conftest import build_client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("openrouter_image_mcp.client.asyncio.sleep", instant)


async def test_retries_on_429_then_succeeds(settings: Settings):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"data": [{"id": "a"}]})

    async with build_client(settings, handler) as client:
        assert await client.list_all_models() == [{"id": "a"}]
    assert calls["n"] == 3


async def test_surfaces_openrouter_error_message(settings: Settings):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    async with build_client(settings, handler) as client:
        with pytest.raises(OpenRouterError, match="Insufficient credits") as excinfo:
            await client.list_all_models()
    assert excinfo.value.status_code == 402


async def test_image_models_falls_back_to_filtered_model_list(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/models"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "text/only", "architecture": {"output_modalities": ["text"]}},
                    {
                        "id": "google/gemini-3.1-flash-image",
                        "architecture": {"output_modalities": ["text", "image"]},
                    },
                ]
            },
        )

    async with build_client(settings, handler) as client:
        models = await client.list_image_models()
    assert [m["id"] for m in models] == ["google/gemini-3.1-flash-image"]


async def test_create_image_falls_back_to_chat_completions(settings: Settings, png_b64):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/images"):
            return httpx.Response(405, json={"error": {"message": "method not allowed"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": f"data:image/png;base64,{png_b64}"}}]
                        }
                    }
                ]
            },
        )

    async with build_client(settings, handler) as client:
        endpoint, body = await client.create_image(
            {"model": "m", "prompt": "a cat", "provider": {"order": ["google-vertex"]}}
        )
    assert endpoint == "/chat/completions"
    assert seen[-1].endswith("/chat/completions")
    assert body["choices"]


def test_to_chat_payload_maps_references_and_config():
    chat = to_chat_payload(
        {
            "model": "google/gemini-3.1-flash-image",
            "prompt": "night version",
            "aspect_ratio": "16:9",
            "seed": 7,
            "provider": {"order": ["google-vertex"]},
            "input_references": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
            ],
        }
    )
    assert chat["modalities"] == ["image", "text"]
    assert chat["image_config"] == {"aspect_ratio": "16:9"}
    assert chat["seed"] == 7
    assert chat["provider"] == {"order": ["google-vertex"]}
    content = chat["messages"][0]["content"]
    assert content[0]["text"] == "night version"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
