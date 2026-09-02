from __future__ import annotations

import pytest

from openrouter_image_mcp.config import ConfigError, Settings


def test_defaults_when_env_is_empty():
    settings = Settings.from_env({})
    assert settings.api_key is None
    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.default_providers == ()
    assert settings.allow_fallbacks is True
    with pytest.raises(ConfigError):
        settings.require_api_key()


def test_reads_and_normalises_env():
    settings = Settings.from_env(
        {
            "OPENROUTER_API_KEY": " sk-or-test ",
            "OPENROUTER_BASE_URL": "https://proxy.example/api/v1/",
            "OPENROUTER_IMAGE_MODEL": "google/gemini-3.1-flash-image",
            "OPENROUTER_IMAGE_PROVIDER": "google-vertex, google-ai-studio ,",
            "OPENROUTER_IMAGE_ALLOW_FALLBACKS": "no",
            "OPENROUTER_IMAGE_PREVIEW_MAX_PX": "512",
        }
    )
    assert settings.api_key == "sk-or-test"
    assert settings.base_url == "https://proxy.example/api/v1"
    assert settings.default_providers == ("google-vertex", "google-ai-studio")
    assert settings.allow_fallbacks is False
    assert settings.preview_max_px == 512
    headers = settings.headers()
    assert headers["Authorization"] == "Bearer sk-or-test"
    assert headers["X-Title"]


def test_rejects_bad_boolean():
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENROUTER_IMAGE_INLINE_PREVIEW": "maybe"})
