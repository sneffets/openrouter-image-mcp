from __future__ import annotations

import base64
import io
from pathlib import Path

import httpx
import pytest

from openrouter_image_mcp.client import OpenRouterClient
from openrouter_image_mcp.config import Settings


def make_png(size: tuple[int, int] = (64, 48), color: str = "red") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    return make_png()


@pytest.fixture
def png_b64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode("ascii")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="test-key",
        base_url="https://openrouter.test/api/v1",
        output_dir=tmp_path / "images",
        timeout=5.0,
        video_poll_interval=0.0,
    )


def build_client(settings: Settings, handler) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    return OpenRouterClient(settings, httpx.AsyncClient(transport=transport))
