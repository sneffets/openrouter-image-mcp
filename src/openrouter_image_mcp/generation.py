"""Building OpenRouter image requests and turning responses into files on disk."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import imaging
from .client import OpenRouterClient
from .config import Settings
from .results import GenerationResult, parse_generation

PROVIDER_SORTS = frozenset({"price", "throughput", "latency"})


@dataclass
class ImageRequest:
    """A single image generation / edit request in server terms."""

    prompt: str
    model: str
    providers: tuple[str, ...] = ()
    allow_fallbacks: bool | None = None
    provider_sort: str | None = None
    n: int = 1
    aspect_ratio: str | None = None
    resolution: str | None = None
    quality: str | None = None
    output_format: str | None = None
    background: str | None = None
    seed: int | None = None
    reference_images: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SavedImage:
    path: Path
    sidecar: Path
    data: bytes
    media_type: str
    size: tuple[int, int] | None


def build_provider_preferences(request: ImageRequest, settings: Settings) -> dict[str, Any] | None:
    """Translate provider choices into OpenRouter's ``provider`` routing object."""
    providers = request.providers or settings.default_providers
    allow_fallbacks = (
        settings.allow_fallbacks if request.allow_fallbacks is None else request.allow_fallbacks
    )
    preferences: dict[str, Any] = {}
    if providers:
        preferences["order"] = list(providers)
        preferences["allow_fallbacks"] = allow_fallbacks
        if not allow_fallbacks:
            # Belt and braces: `only` guarantees no other provider is used.
            preferences["only"] = list(providers)
    if request.provider_sort:
        sort = request.provider_sort.strip().lower()
        if sort not in PROVIDER_SORTS:
            raise ValueError(
                f"provider_sort must be one of {sorted(PROVIDER_SORTS)}, "
                f"got {request.provider_sort!r}"
            )
        preferences["sort"] = sort
    return preferences or None


def build_payload(request: ImageRequest, settings: Settings) -> dict[str, Any]:
    """Assemble the JSON body for ``POST /api/v1/images``."""
    if not request.prompt.strip():
        raise ValueError("prompt must not be empty.")
    if request.n < 1 or request.n > 8:
        raise ValueError("n must be between 1 and 8.")

    payload: dict[str, Any] = {"model": request.model, "prompt": request.prompt.strip()}
    if request.n > 1:
        payload["n"] = request.n
    for key, value in (
        ("aspect_ratio", request.aspect_ratio),
        ("resolution", request.resolution),
        ("quality", request.quality),
        ("output_format", request.output_format),
        ("background", request.background),
    ):
        if value:
            payload[key] = value
    if request.seed is not None:
        payload["seed"] = request.seed
    if request.reference_images:
        payload["input_references"] = [
            {"type": "image_url", "image_url": {"url": imaging.load_reference(ref)}}
            for ref in request.reference_images
        ]
    preferences = build_provider_preferences(request, settings)
    if preferences:
        payload["provider"] = preferences
    if request.extra:
        payload.update(request.extra)
    return payload


async def materialise(
    result: GenerationResult, client: OpenRouterClient
) -> list[tuple[bytes, str]]:
    """Make sure every returned image is available as raw bytes."""
    images: list[tuple[bytes, str]] = []
    for image in result.images:
        if image.data is not None:
            images.append((image.data, image.media_type))
        elif image.url:
            data, media_type = await client.download(image.url)
            images.append((data, media_type))
    return images


def save_images(
    images: Sequence[tuple[bytes, str]],
    *,
    directory: Path,
    prefix: str,
    metadata: dict[str, Any],
) -> list[SavedImage]:
    """Write images plus a JSON sidecar describing how they were made."""
    saved: list[SavedImage] = []
    timestamp = datetime.now(timezone.utc)
    for index, (data, media_type) in enumerate(images):
        path = imaging.save_image(
            data, media_type, directory, prefix=prefix, index=index, timestamp=timestamp
        )
        size = imaging.image_dimensions(data)
        sidecar = imaging.write_sidecar(
            path,
            {
                **metadata,
                "created_at": timestamp.isoformat(),
                "file": path.name,
                "media_type": media_type,
                "bytes": len(data),
                "width": size[0] if size else None,
                "height": size[1] if size else None,
            },
        )
        saved.append(SavedImage(path, sidecar, data, media_type, size))
    return saved


async def generate(
    client: OpenRouterClient,
    settings: Settings,
    request: ImageRequest,
    *,
    output_dir: Path | None = None,
    name_prefix: str | None = None,
) -> tuple[GenerationResult, list[SavedImage]]:
    """Run one generation and persist the results."""
    payload = build_payload(request, settings)
    endpoint, body = await client.create_image(payload)
    result = parse_generation(body, endpoint=endpoint)
    result.model = result.model or request.model
    images = await materialise(result, client)

    directory = output_dir or settings.output_dir
    prefix = name_prefix or request.prompt
    metadata = {
        "prompt": request.prompt,
        "model": result.model,
        "requested_model": request.model,
        "provider": result.provider,
        "requested_providers": list(request.providers or settings.default_providers),
        "endpoint": endpoint,
        "generation_id": result.generation_id,
        "aspect_ratio": request.aspect_ratio,
        "resolution": request.resolution,
        "quality": request.quality,
        "seed": request.seed,
        "reference_images": list(request.reference_images),
        "usage": result.usage,
    }
    return result, save_images(images, directory=directory, prefix=prefix, metadata=metadata)
