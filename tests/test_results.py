from __future__ import annotations

import base64

import pytest

from openrouter_image_mcp.results import ResponseParseError, decode_data_url, parse_generation


def test_parses_images_endpoint_response(png_b64):
    result = parse_generation(
        {
            "created": 1,
            "model": "google/gemini-3.1-flash-image",
            "data": [{"b64_json": png_b64, "media_type": "image/png"}],
            "usage": {"cost": 0.0031},
        }
    )
    assert len(result.images) == 1
    assert result.images[0].data == base64.b64decode(png_b64)
    assert result.images[0].media_type == "image/png"
    assert result.cost == pytest.approx(0.0031)


def test_parses_chat_completions_response(png_b64):
    result = parse_generation(
        {
            "model": "google/gemini-3.1-flash-image",
            "provider": "Google Vertex",
            "choices": [
                {
                    "message": {
                        "content": "Here you go",
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/webp;base64,{png_b64}"},
                            }
                        ],
                    }
                }
            ],
        },
        endpoint="/chat/completions",
    )
    assert result.text == "Here you go"
    assert result.provider == "Google Vertex"
    assert result.images[0].media_type == "image/webp"


def test_keeps_remote_urls_for_later_download():
    result = parse_generation({"data": [{"url": "https://cdn.example/a.png"}]})
    assert result.images[0].url == "https://cdn.example/a.png"
    assert result.images[0].data is None


def test_raises_with_model_text_when_no_image():
    with pytest.raises(ResponseParseError, match="cannot draw that"):
        parse_generation({"choices": [{"message": {"content": "I cannot draw that"}}]})


def test_decode_data_url_rejects_plain_text():
    assert decode_data_url("not a data url") is None
