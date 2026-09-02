from __future__ import annotations

import httpx
import pytest

from openrouter_image_mcp import server as srv
from openrouter_image_mcp.config import Settings

from .conftest import build_client

IMAGE_MODELS = {
    "data": [
        {
            "id": "google/gemini-3.1-flash-image",
            "name": "Google: Nano Banana 2 (Gemini 3.1 Flash Image)",
            "pricing": {"image": "0.03"},
            "supported_parameters": {"input_references": {"min": 0, "max": 14}},
        },
        {"id": "bytedance-seed/seedream-4.5", "name": "ByteDance: Seedream 4.5"},
    ]
}
ENDPOINTS = {
    "data": {
        "id": "google/gemini-3.1-flash-image",
        "endpoints": [
            {"tag": "google-vertex", "provider_name": "Google Vertex", "uptime_last_30m": 99.5},
            {"tag": "google-ai-studio", "provider_name": "Google AI Studio"},
        ],
    }
}


@pytest.fixture
def wire(settings: Settings, png_b64):
    """Point the module level server at a mocked OpenRouter."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/images/models"):
            return httpx.Response(200, json=IMAGE_MODELS)
        if path.endswith("/endpoints"):
            return httpx.Response(200, json=ENDPOINTS)
        if path.endswith("/images"):
            return httpx.Response(
                200,
                json={
                    "model": "google/gemini-3.1-flash-image",
                    "provider": "Google Vertex",
                    "data": [{"b64_json": png_b64, "media_type": "image/png"}],
                    "usage": {"cost": 0.0042},
                },
            )
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})

    srv.configure(settings, build_client(settings, handler))
    yield requests


async def test_list_image_models_returns_slugs(wire):
    text = await srv.list_image_models(query="nano banana")
    assert "`google/gemini-3.1-flash-image`" in text
    assert "seedream" not in text.lower()


async def test_partial_name_resolves_when_unambiguous(wire):
    content = await srv.generate_image(prompt="x", model="nano banana")
    assert "google/gemini-3.1-flash-image" in content[0].text


async def test_list_providers_shows_routing_slugs(wire):
    text = await srv.list_providers(model="nano banana 2")
    assert "`google-vertex`" in text and "`google-ai-studio`" in text


async def test_describe_model_includes_providers(wire):
    text = await srv.describe_image_model("nano banana 2")
    assert "max reference images**: 14" in text
    assert "google-ai-studio" in text


async def test_generate_image_saves_and_previews(wire, settings: Settings):
    content = await srv.generate_image(
        prompt="A hero image for the landing page",
        model="nano banana 2",
        providers=["google-vertex"],
    )
    assert content[0].type == "text"
    assert "google/gemini-3.1-flash-image" in content[0].text
    assert "Google Vertex" in content[0].text
    assert "$0.0042" in content[0].text
    assert content[1].type == "image" and content[1].mime_type == "image/png"

    saved = list(settings.output_dir.glob("*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in content[0].text

    body = [r for r in wire if r.url.path.endswith("/images") and r.method == "POST"][0]
    import json

    payload = json.loads(body.content)
    assert payload["model"] == "google/gemini-3.1-flash-image"
    assert payload["provider"]["order"] == ["google-vertex"]


async def test_generate_without_model_asks_instead_of_guessing(wire):
    content = await srv.generate_image(prompt="A hero image")
    assert len(content) == 1
    assert "No model was selected" in content[0].text
    assert "`google/gemini-3.1-flash-image`" in content[0].text


async def test_unknown_model_lists_candidates(wire):
    content = await srv.generate_image(prompt="x", model="dall-e-9")
    assert "No image model matches" in content[0].text
    assert "`google/gemini-3.1-flash-image`" in content[0].text


async def test_default_model_from_settings_is_used(settings: Settings, wire):
    srv.configure(
        Settings(
            api_key="k",
            base_url=settings.base_url,
            default_model="google/gemini-3.1-flash-image",
            output_dir=settings.output_dir,
        ),
        srv.get_client(),
    )
    content = await srv.generate_image(prompt="A hero image")
    assert "Generated 1 image" in content[0].text


async def test_edit_image_rejects_too_many_references(wire, tmp_path, png_bytes):
    refs = []
    for index in range(15):
        path = tmp_path / f"ref{index}.png"
        path.write_bytes(png_bytes)
        refs.append(str(path))
    content = await srv.edit_image(prompt="night", reference_images=refs, model="nano banana 2")
    assert "at most 14" in content[0].text


async def test_edit_image_sends_references(wire, tmp_path, png_bytes):
    reference = tmp_path / "ref.png"
    reference.write_bytes(png_bytes)
    content = await srv.edit_image(
        prompt="make it night", reference_images=[str(reference)], model="nano banana 2"
    )
    assert "Generated 1 image" in content[0].text
    import json

    payload = json.loads([r for r in wire if r.method == "POST"][0].content)
    assert payload["input_references"][0]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_generate_reports_api_errors_as_text(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/models"):
            return httpx.Response(200, json=IMAGE_MODELS)
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    srv.configure(settings, build_client(settings, handler))
    content = await srv.generate_image(prompt="x", model="google/gemini-3.1-flash-image")
    assert "Insufficient credits" in content[0].text


async def test_show_image_returns_inline_preview(wire, tmp_path, png_bytes):
    path = tmp_path / "shot.png"
    path.write_bytes(png_bytes)
    content = await srv.show_image(str(path))
    assert content[0].type == "text" and "image/png" in content[0].text
    assert content[1].type == "image"


async def test_list_generated_images(wire, settings: Settings):
    await srv.generate_image(prompt="hero", model="nano banana 2")
    text = await srv.list_generated_images()
    assert ".png" in text


async def test_status_reports_missing_api_key(tmp_path):
    srv.configure(Settings(api_key=None, output_dir=tmp_path))
    text = await srv.openrouter_status()
    assert "NO - set OPENROUTER_API_KEY" in text
