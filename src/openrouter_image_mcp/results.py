"""Normalise the different response shapes OpenRouter can return for images."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any

DATA_URL_RE = re.compile(
    r"^data:(?P<media_type>[^;,]+)?(?P<base64>;base64)?,(?P<payload>.*)$", re.S
)


class ResponseParseError(RuntimeError):
    """Raised when a response contains no usable image data."""


@dataclass
class ImagePayload:
    """One image returned by a model, either inline or as a remote URL."""

    data: bytes | None = None
    url: str | None = None
    media_type: str = "image/png"
    revised_prompt: str | None = None


@dataclass
class GenerationResult:
    """Everything worth reporting back about one generation call."""

    images: list[ImagePayload] = field(default_factory=list)
    text: str = ""
    model: str | None = None
    provider: str | None = None
    generation_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    endpoint: str = "/images"

    @property
    def cost(self) -> float | None:
        for key in ("cost", "total_cost"):
            value = self.usage.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None


def decode_data_url(value: str) -> tuple[bytes, str] | None:
    """Decode a ``data:image/png;base64,...`` URL into bytes plus media type."""
    match = DATA_URL_RE.match(value.strip())
    if not match:
        return None
    media_type = (match.group("media_type") or "image/png").strip() or "image/png"
    payload = match.group("payload")
    try:
        if match.group("base64"):
            return base64.b64decode(payload, validate=False), media_type
        from urllib.parse import unquote_to_bytes

        return unquote_to_bytes(payload), media_type
    except (binascii.Error, ValueError):
        return None


def _decode_b64(value: str, media_type: str) -> ImagePayload | None:
    if not value:
        return None
    if value.startswith("data:"):
        decoded = decode_data_url(value)
        if decoded is None:
            return None
        return ImagePayload(data=decoded[0], media_type=decoded[1])
    try:
        return ImagePayload(data=base64.b64decode(value, validate=False), media_type=media_type)
    except (binascii.Error, ValueError):
        return None


def _from_image_entry(entry: Any) -> ImagePayload | None:
    """Parse a single element of ``data[]`` or ``message.images[]``."""
    if isinstance(entry, str):
        if entry.startswith("data:"):
            return _decode_b64(entry, "image/png")
        if entry.startswith(("http://", "https://")):
            return ImagePayload(url=entry)
        return _decode_b64(entry, "image/png")
    if not isinstance(entry, dict):
        return None

    media_type = str(
        entry.get("media_type") or entry.get("mime_type") or entry.get("mimeType") or "image/png"
    )
    revised = entry.get("revised_prompt") or entry.get("revisedPrompt")

    for key in ("b64_json", "b64", "base64", "image_base64", "data"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            payload = _decode_b64(value, media_type)
            if payload is not None:
                payload.revised_prompt = revised
                return payload

    image_url = entry.get("image_url") or entry.get("imageUrl")
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    url = image_url or entry.get("url")
    if isinstance(url, str) and url:
        if url.startswith("data:"):
            payload = _decode_b64(url, media_type)
            if payload is not None:
                payload.revised_prompt = revised
                return payload
        return ImagePayload(url=url, media_type=media_type, revised_prompt=revised)
    return None


def parse_generation(body: dict[str, Any], *, endpoint: str = "/images") -> GenerationResult:
    """Extract images, text, usage and routing info from a generation response."""
    result = GenerationResult(endpoint=endpoint)
    result.model = body.get("model")
    result.provider = body.get("provider") or body.get("provider_name")
    result.generation_id = body.get("id")
    usage = body.get("usage")
    if isinstance(usage, dict):
        result.usage = usage

    entries = body.get("data")
    if isinstance(entries, list):
        for entry in entries:
            payload = _from_image_entry(entry)
            if payload is not None:
                result.images.append(payload)

    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or choice.get("delta") or {}
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            result.text = f"{result.text}\n{content.strip()}".strip()
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    result.text = f"{result.text}\n{part['text'].strip()}".strip()
                elif isinstance(part, dict) and part.get("type") in {"image_url", "image"}:
                    payload = _from_image_entry(part)
                    if payload is not None:
                        result.images.append(payload)
        for entry in message.get("images") or []:
            payload = _from_image_entry(entry)
            if payload is not None:
                result.images.append(payload)

    if not result.images:
        reason = result.text.strip() or "no image data in the response"
        raise ResponseParseError(
            f"The model returned no image ({reason}). "
            "Check that the model supports image output and that the prompt was not refused."
        )
    return result
