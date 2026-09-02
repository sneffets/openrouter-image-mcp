from __future__ import annotations

import pytest

from openrouter_image_mcp.catalog import (
    ModelCatalog,
    ModelNotFoundError,
    endpoint_slug,
    filter_models,
    format_endpoints,
    format_model_table,
    max_reference_images,
)

MODELS = [
    {
        "id": "google/gemini-3.1-flash-image",
        "name": "Google: Nano Banana 2 (Gemini 3.1 Flash Image)",
        "pricing": {"image": "0.03"},
        "supported_parameters": {"input_references": {"min": 0, "max": 14}, "aspect_ratio": True},
    },
    {
        "id": "google/gemini-2.5-flash-image",
        "name": "Google: Nano Banana (Gemini 2.5 Flash Image)",
        "pricing": {"image": "0.02"},
    },
    {"id": "bytedance-seed/seedream-4.5", "name": "ByteDance: Seedream 4.5"},
]


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    async def list_image_models(self):
        self.calls += 1
        return MODELS

    async def list_model_endpoints(self, model):
        return {"endpoints": [{"tag": "google-vertex", "provider_name": "Google Vertex"}]}


def test_filter_matches_friendly_names():
    assert [m["id"] for m in filter_models(MODELS, "nano banana 2")] == [
        "google/gemini-3.1-flash-image"
    ]
    assert len(filter_models(MODELS, "nano banana")) == 2
    assert len(filter_models(MODELS, "")) == 3


async def test_resolve_prefers_exact_slug():
    catalog = ModelCatalog(FakeClient())
    resolved = await catalog.resolve("google/gemini-2.5-flash-image")
    assert resolved["id"] == "google/gemini-2.5-flash-image"


async def test_resolve_by_friendly_name():
    catalog = ModelCatalog(FakeClient())
    assert (await catalog.resolve("nano banana 2"))["id"] == "google/gemini-3.1-flash-image"


async def test_resolve_ambiguous_offers_candidates():
    catalog = ModelCatalog(FakeClient())
    with pytest.raises(ModelNotFoundError) as excinfo:
        await catalog.resolve("nano banana")
    assert len(excinfo.value.candidates) == 2


async def test_results_are_cached_until_refresh():
    client = FakeClient()
    catalog = ModelCatalog(client)
    await catalog.image_models()
    await catalog.image_models()
    assert client.calls == 1
    await catalog.image_models(refresh=True)
    assert client.calls == 2


def test_max_reference_images_reads_capability_descriptor():
    assert max_reference_images(MODELS[0]) == 14
    assert max_reference_images(MODELS[2]) is None


def test_table_and_endpoint_rendering():
    table = format_model_table(MODELS, limit=2)
    assert "`google/gemini-3.1-flash-image`" in table
    assert "1 more" in table
    endpoints = format_endpoints(
        "google/gemini-3.1-flash-image",
        {"endpoints": [{"tag": "google-vertex", "provider_name": "Google Vertex"}]},
    )
    assert "`google-vertex`" in endpoints


def test_endpoint_slug_falls_back_to_provider_name():
    assert endpoint_slug({"provider_name": "Google AI Studio"}) == "google-ai-studio"
