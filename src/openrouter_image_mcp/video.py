"""Video jobs: request building, validation against model capabilities, polling, saving."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import imaging, media
from .catalog import model_slug
from .client import OpenRouterClient
from .config import Settings

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
NEGATIVE_PROMPT_KEYS = ("negative_prompt", "negativePrompt")
JOBS_DIRNAME = ".video-jobs"
BYPASS_HINT = " Pass it via `extra_body` to send it anyway."

ProgressCallback = Callable[[dict[str, Any], float], Awaitable[None]]


@dataclass
class VideoRequest:
    """One video generation / edit / upscale request in server terms."""

    model: str
    prompt: str = ""
    duration: int | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    size: str | None = None
    generate_audio: bool | None = None
    seed: int | None = None
    first_frame: str | None = None
    last_frame: str | None = None
    reference_images: tuple[str, ...] = ()
    reference_videos: tuple[str, ...] = ()
    reference_audios: tuple[str, ...] = ()
    negative_prompt: str | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)
    upscale_factor: float | None = None
    creativity: int | None = None
    callback_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_provider_slug(self) -> bool:
        return bool(self.provider_options or self.negative_prompt)

    def metadata(self) -> dict[str, Any]:
        """What goes into the job record and sidecar (inline data URLs are elided)."""
        return {
            "prompt": self.prompt,
            "requested_model": self.model,
            "duration": self.duration,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "size": self.size,
            "generate_audio": self.generate_audio,
            "seed": self.seed,
            "first_frame": _describe_input(self.first_frame),
            "last_frame": _describe_input(self.last_frame),
            "reference_images": [_describe_input(r) for r in self.reference_images],
            "reference_videos": [_describe_input(r) for r in self.reference_videos],
            "reference_audios": [_describe_input(r) for r in self.reference_audios],
            "negative_prompt": self.negative_prompt,
            "provider_options": self.provider_options,
            "upscale_factor": self.upscale_factor,
            "creativity": self.creativity,
        }


def _describe_input(value: str | None) -> str | None:
    if value and value.startswith("data:"):
        return value.split(",", 1)[0] + ",... (inline)"
    return value


def _choice(model: dict[str, Any], name: str, value: Any, catalog_key: str) -> Any:
    """Validate ``value`` against the model's advertised options.

    Returns the catalogue's spelling, so ``"4k"`` becomes ``"4K"``.
    """
    if value is None:
        return None
    slug = model_slug(model)
    allowed = model.get(catalog_key)
    if not isinstance(allowed, list) or not allowed:
        raise ValueError(f"`{slug}` does not support `{name}`.{BYPASS_HINT}")
    for option in allowed:
        if option == value or (
            isinstance(option, str) and isinstance(value, str) and option.lower() == value.lower()
        ):
            return option
    raise ValueError(
        f"`{slug}` does not support {name}={value!r}; supported: "
        f"{', '.join(map(str, allowed))}.{BYPASS_HINT}"
    )


def build_video_payload(
    request: VideoRequest,
    model: dict[str, Any],
    provider_slugs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assemble and validate the JSON body for ``POST /api/v1/videos``.

    Validation happens client side because OpenRouter answers out-of-set values
    with a 400 anyway - this way the error names the values that would work.
    """
    slug = request.model
    prompt = request.prompt.strip()
    driven_by_input = bool(request.first_frame or request.last_frame or request.reference_videos)
    if not prompt and not driven_by_input:
        raise ValueError(
            "prompt must not be empty (it may only be omitted when a first/last frame or a "
            "source video is given)."
        )

    payload: dict[str, Any] = {"model": slug}
    if prompt:
        payload["prompt"] = prompt
    for name, value, key in (
        ("duration", request.duration, "supported_durations"),
        ("resolution", request.resolution, "supported_resolutions"),
        ("aspect_ratio", request.aspect_ratio, "supported_aspect_ratios"),
        ("size", request.size, "supported_sizes"),
    ):
        chosen = _choice(model, name, value, key)
        if chosen is not None:
            payload[name] = chosen

    if request.seed is not None:
        if model.get("seed") is False:
            raise ValueError(f"`{slug}` does not support a seed.{BYPASS_HINT}")
        payload["seed"] = request.seed
    if request.generate_audio is not None:
        if request.generate_audio and model.get("generate_audio") is False:
            raise ValueError(f"`{slug}` cannot generate audio.{BYPASS_HINT}")
        payload["generate_audio"] = request.generate_audio

    frames = []
    supported_frames = model.get("supported_frame_images") or []
    for frame_type, reference in (
        ("first_frame", request.first_frame),
        ("last_frame", request.last_frame),
    ):
        if not reference:
            continue
        if frame_type not in supported_frames:
            raise ValueError(
                f"`{slug}` does not accept a {frame_type.replace('_', ' ')} image; supported "
                f"frame images: {', '.join(supported_frames) or 'none'}. "
                "Use `reference_images` for loose visual guidance instead."
            )
        frames.append(
            {
                "type": "image_url",
                "image_url": {"url": media.load_input(reference, "image")},
                "frame_type": frame_type,
            }
        )
    if frames:
        payload["frame_images"] = frames

    references = []
    for kind, part, values in (
        ("video", "video_url", request.reference_videos),
        ("image", "image_url", request.reference_images),
        ("audio", "audio_url", request.reference_audios),
    ):
        for value in values:
            references.append({"type": part, part: {"url": media.load_input(value, kind)}})
    if references:
        payload["input_references"] = references

    if request.upscale_factor is not None:
        bounds = model.get("upscale_factor")
        if not isinstance(bounds, dict):
            raise ValueError(f"`{slug}` is not an upscaling model; drop `upscale_factor`.")
        low, high = bounds.get("min"), bounds.get("max")
        if (low is not None and request.upscale_factor < low) or (
            high is not None and request.upscale_factor > high
        ):
            raise ValueError(f"upscale_factor for `{slug}` must be between {low} and {high}.")
        payload["upscale_factor"] = request.upscale_factor
    creativity = _choice(model, "creativity", request.creativity, "creativity")
    if creativity is not None:
        payload["creativity"] = creativity

    if request.callback_url:
        if not request.callback_url.startswith("https://"):
            raise ValueError("callback_url must be an https:// URL.")
        payload["callback_url"] = request.callback_url

    parameters = dict(request.provider_options)
    allowed = [str(p) for p in model.get("allowed_passthrough_parameters") or []]
    if request.negative_prompt:
        key = next((k for k in NEGATIVE_PROMPT_KEYS if k in allowed), None)
        if key is None:
            raise ValueError(f"`{slug}` does not accept a negative prompt.")
        parameters.setdefault(key, request.negative_prompt)
    unknown = sorted(k for k in parameters if k not in allowed)
    if unknown:
        raise ValueError(
            f"`{slug}` does not accept provider_options {', '.join(unknown)}; allowed: "
            f"{', '.join(allowed) or 'none'}."
        )
    if parameters:
        if not provider_slugs:
            raise ValueError(
                f"Could not determine which provider serves `{slug}`, so provider_options have "
                "nowhere to go. Pass `provider` (see `list_providers`)."
            )
        # Only the matched provider's block is forwarded, so listing every
        # endpoint keeps the options working whichever one OpenRouter picks.
        payload["provider"] = {
            "options": {provider: {"parameters": parameters} for provider in provider_slugs}
        }

    if request.extra:
        payload.update(request.extra)
    return payload


async def wait_for_job(
    client: OpenRouterClient,
    job_id: str,
    *,
    interval: float,
    max_wait: float,
    on_progress: ProgressCallback | None = None,
) -> tuple[dict[str, Any], float]:
    """Poll until the job is terminal or ``max_wait`` seconds passed.

    Returns the last status body and the elapsed seconds.
    """
    started = time.monotonic()
    status = await client.get_video_job(job_id)
    while True:
        elapsed = time.monotonic() - started
        if on_progress is not None:
            await on_progress(status, elapsed)
        if status.get("status") in TERMINAL_STATUSES or elapsed >= max_wait:
            return status, elapsed
        await asyncio.sleep(max(0.0, min(interval, max_wait - elapsed)))
        status = await client.get_video_job(job_id)


# ----------------------------------------------------------------- job records


def _record_path(settings: Settings, job_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    return settings.videos_dir / JOBS_DIRNAME / f"{safe}.json"


def save_job_record(settings: Settings, record: dict[str, Any]) -> Path:
    """Persist a job so it can be resumed after a timeout or a server restart."""
    path = _record_path(settings, record["job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_job_record(settings: Settings, job_id: str) -> dict[str, Any] | None:
    path = _record_path(settings, job_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_job_records(settings: Settings) -> list[dict[str, Any]]:
    directory = settings.videos_dir / JOBS_DIRNAME
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("job_id"):
            records.append(data)
    records.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return records


def new_job_record(
    job: dict[str, Any],
    request: VideoRequest,
    *,
    save_dir: Path | None,
    name_prefix: str | None,
) -> dict[str, Any]:
    return {
        "job_id": job["id"],
        "status": job.get("status") or "pending",
        "model": request.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "save_dir": str(save_dir) if save_dir else None,
        "name_prefix": name_prefix,
        **request.metadata(),
    }


# ---------------------------------------------------------------- downloading


@dataclass
class SavedVideo:
    path: Path
    sidecar: Path
    media_type: str
    size_bytes: int
    info: media.VideoInfo | None


async def download_outputs(
    client: OpenRouterClient,
    settings: Settings,
    status: dict[str, Any],
    record: dict[str, Any],
) -> list[SavedVideo]:
    """Download every output of a completed job and write a sidecar for each."""
    job_id = str(status.get("id") or record["job_id"])
    urls: list[str | None] = [u for u in status.get("unsigned_urls") or [] if u] or [None]
    directory = Path(record.get("save_dir") or settings.videos_dir)
    prefix = record.get("name_prefix") or record.get("prompt") or "video"
    timestamp = datetime.now(timezone.utc)
    stem = f"{timestamp.strftime('%Y%m%d-%H%M%S')}-{imaging.slugify(prefix)}"

    saved: list[SavedVideo] = []
    for index, url in enumerate(urls):
        data, media_type = await client.download_video(job_id, index, url)
        if not media_type.startswith("video/"):
            media_type = "video/mp4"
        path = imaging.unique_path(
            directory, f"{stem}-{index + 1}" if index else stem, media.video_extension(media_type)
        )
        path.write_bytes(data)
        info = await asyncio.to_thread(media.probe_video, path)
        sidecar = imaging.write_sidecar(
            path,
            {
                **{k: v for k, v in record.items() if k not in {"files", "save_dir", "status"}},
                "generation_id": status.get("generation_id"),
                "usage": status.get("usage"),
                "downloaded_at": timestamp.isoformat(),
                "file": path.name,
                "media_type": media_type,
                "bytes": len(data),
                "video_duration": info.duration if info else None,
                "width": info.width if info else None,
                "height": info.height if info else None,
                "has_audio": info.has_audio if info else None,
            },
        )
        saved.append(SavedVideo(path, sidecar, media_type, len(data), info))
    return saved


def job_cost(status: dict[str, Any]) -> float | None:
    usage = status.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("cost"), (int, float)):
        return float(usage["cost"])
    return None
