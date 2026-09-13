"""MCP server exposing OpenRouter image and video generation to Claude Code and other clients."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import AcceptedElicitation, Context, MCPServer
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, Field

from . import __version__, imaging, media, video
from .catalog import (
    ModelCatalog,
    ModelNotFoundError,
    endpoint_slug,
    format_endpoints,
    format_model_details,
    format_model_table,
    format_video_model_details,
    format_video_model_table,
    max_reference_images,
    model_slug,
)
from .client import OpenRouterClient, OpenRouterError
from .config import ConfigError, Settings
from .generation import ImageRequest, generate
from .imaging import ImageFileError
from .results import ResponseParseError

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Generate and edit images and videos through OpenRouter (Nano Banana, GPT Image, Seedream, Flux,
Veo, Kling, Seedance, Sora, Wan, ...).

Images:
1. `list_image_models` shows which models can produce images; `list_providers` shows which
   upstream providers serve a given model (e.g. Google Vertex vs Google AI Studio).
2. Ask the user which model and provider they want when it matters, then pass `model` and
   `providers` to `generate_image` or `edit_image`.
3. Images are written to the output directory and the file path is returned, so they can be
   re-read, committed to a repo, or referenced in later edits.

Videos:
1. `list_video_models` / `describe_video_model` show durations, resolutions, aspect ratios,
   which frame images a model accepts and its model-specific `provider_options`.
2. `generate_video` covers text-to-video, image-to-video (`first_frame` / `last_frame`) and
   reference-to-video (`reference_images`, plus `reference_videos` / `reference_audios` on
   models that honour them, e.g. Seedance 2.x). `edit_video` edits, restyles or upscales a clip.
3. Video jobs are paid and take 30s to several minutes. The tool waits up to a limit; if it
   returns a job id instead of a file, call `check_video_job` later. Never resubmit a job that
   is still running - that pays twice.

If no model is given and no default is configured, the tools return a list of candidates
instead of guessing - present them to the user and ask.
"""

SELECTION_HINT = (
    "No model was selected. Pick one of the models below, ask the user which one they want, "
    'and call the tool again with `model="<slug>"`. '
    "You can also set {env} in the server environment to define a default."
)

server = MCPServer(
    name="openrouter-image",
    title="OpenRouter Image & Video Generation",
    instructions=INSTRUCTIONS,
    version=__version__,
)

_settings: Settings | None = None
_client: OpenRouterClient | None = None
_catalog: ModelCatalog | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def get_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient(get_settings())
    return _client


def get_catalog() -> ModelCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ModelCatalog(get_client())
    return _catalog


def configure(
    settings: Settings, client: OpenRouterClient | None = None
) -> None:  # pragma: no cover - used by tests and __main__
    """Inject settings/client, mostly so tests can point the server at a mock transport."""
    global _settings, _client, _catalog
    _settings = settings
    _client = client or OpenRouterClient(settings)
    _catalog = ModelCatalog(_client)


class ModelChoice(BaseModel):
    model: str = Field(description="OpenRouter model slug, e.g. google/gemini-3.1-flash-image")


class ProviderChoice(BaseModel):
    provider: str = Field(
        default="",
        description="Provider slug to pin, e.g. google-vertex. Leave empty for automatic routing.",
    )


class NeedsSelection(Exception):
    """Signals that the caller has to ask the user which model to use."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


async def _ask_for_model(
    ctx: Context | None, candidates: list[dict[str, Any]], kind: str = "image"
) -> str | None:
    """Try MCP elicitation; returns None when the client does not support it."""
    if ctx is None:
        return None
    preview = ", ".join(model_slug(m) for m in candidates[:8])
    try:
        result = await ctx.elicit(
            message=f"Which OpenRouter {kind} model should be used? Candidates: {preview}",
            schema=ModelChoice,
        )
    except Exception as exc:  # client without elicitation support, or user closed it
        logger.debug("elicitation unavailable: %s", exc)
        return None
    if isinstance(result, AcceptedElicitation) and result.data.model.strip():
        return result.data.model.strip()
    return None


async def _ask_for_provider(ctx: Context | None, model: str, endpoints: list[str]) -> str | None:
    if ctx is None or len(endpoints) < 2:
        return None
    try:
        result = await ctx.elicit(
            message=(
                f"`{model}` is served by several providers: {', '.join(endpoints)}. "
                "Which one should be used?"
            ),
            schema=ProviderChoice,
        )
    except Exception as exc:
        logger.debug("elicitation unavailable: %s", exc)
        return None
    if isinstance(result, AcceptedElicitation) and result.data.provider.strip():
        return result.data.provider.strip()
    return None


async def _resolve_model(model: str, ctx: Context | None, kind: str = "image") -> str:
    """Turn user input (slug, friendly name or nothing) into a concrete model slug."""
    catalog = get_catalog()
    settings = get_settings()
    if kind == "video":
        default, load, resolve = (
            settings.default_video_model,
            catalog.video_models,
            catalog.resolve_video,
        )
        table, env = format_video_model_table, "OPENROUTER_VIDEO_MODEL"
    else:
        default, load, resolve = settings.default_model, catalog.image_models, catalog.resolve
        table, env = format_model_table, "OPENROUTER_IMAGE_MODEL"
    query = (model or "").strip() or (default or "")

    if not query:
        candidates = await load()
        chosen = await _ask_for_model(ctx, candidates, kind)
        if not chosen:
            raise NeedsSelection(
                f"{SELECTION_HINT.format(env=env)}\n\n{table(candidates, limit=25)}"
            )
        query = chosen

    try:
        return model_slug(await resolve(query))
    except ModelNotFoundError as exc:
        raise NeedsSelection(
            f"No {kind} model matches `{query}`. Pick one of these and call the tool again:\n\n"
            f"{table(exc.candidates, limit=25)}"
        ) from exc


async def _resolve_providers(
    model: str, providers: list[str] | None, ctx: Context | None
) -> tuple[str, ...]:
    chosen = tuple(p.strip() for p in (providers or []) if p and p.strip())
    if chosen:
        return chosen
    settings = get_settings()
    if settings.default_providers or not settings.ask_for_provider:
        return ()
    try:
        data = await get_catalog().endpoints(model)
    except OpenRouterError:
        return ()
    slugs = [endpoint_slug(e) for e in (data.get("endpoints") or []) if isinstance(e, dict)]
    picked = await _ask_for_provider(ctx, model, slugs)
    return (picked,) if picked else ()


def _preview_content(saved: Any, settings: Settings, inline: bool) -> ImageContent | None:
    if not inline:
        return None
    preview = imaging.make_preview(saved.data, saved.media_type, settings.preview_max_px)
    if preview is None:
        return None
    data, media_type = preview
    return _image_content(data, media_type)


def _image_content(data: bytes, media_type: str) -> ImageContent:
    return ImageContent(
        type="image",
        data=base64.b64encode(data).decode("ascii"),
        mime_type=media_type,
    )


def _error_text(exc: Exception, what: str = "Image generation") -> list[TextContent]:
    return [TextContent(type="text", text=f"{what} failed: {exc}")]


# --------------------------------------------------------------------- tools


@server.tool(
    title="List image models",
    description=(
        "List OpenRouter models that can generate images. Use `query` to filter by name or "
        "slug, e.g. 'nano banana', 'google', 'seedream'. Returns the slugs to pass as `model`."
    ),
)
async def list_image_models(query: str = "", limit: int = 20, refresh: bool = False) -> str:
    try:
        models = await get_catalog().search(query, refresh=refresh)
    except (OpenRouterError, ConfigError) as exc:
        return f"Could not load the model list: {exc}"
    header = (
        f"{len(models)} image model(s) matching `{query}`:"
        if query
        else f"{len(models)} image models:"
    )
    return f"{header}\n\n{format_model_table(models, limit=max(1, limit))}"


@server.tool(
    title="Describe image model",
    description=(
        "Show details for one image model: pricing, supported parameters, how many reference "
        "images it accepts, and which providers serve it."
    ),
)
async def describe_image_model(model: str) -> str:
    catalog = get_catalog()
    try:
        resolved = await catalog.resolve(model)
    except ModelNotFoundError as exc:
        return (
            f"No image model matches `{model}`.\n\n{format_model_table(exc.candidates, limit=25)}"
        )
    except (OpenRouterError, ConfigError) as exc:
        return f"Could not load model details: {exc}"

    slug = model_slug(resolved)
    text = format_model_details(resolved)
    try:
        endpoints = await catalog.endpoints(slug)
        text = f"{text}\n\n{format_endpoints(slug, endpoints)}"
    except OpenRouterError as exc:
        text = f"{text}\n\n(Provider list unavailable: {exc})"
    return text


@server.tool(
    title="List providers",
    description=(
        "List the upstream providers for a model (pass `model`), or every provider OpenRouter "
        "knows about (leave `model` empty). The provider slug goes into `providers` when "
        "generating an image."
    ),
)
async def list_providers(model: str = "", refresh: bool = False) -> str:
    catalog = get_catalog()
    try:
        if model.strip():
            try:
                resolved = model_slug(await catalog.resolve(model))
            except ModelNotFoundError:
                resolved = model_slug(await catalog.resolve_video(model))
            return format_endpoints(resolved, await catalog.endpoints(resolved, refresh=refresh))
        providers = await get_client().list_providers()
    except ModelNotFoundError as exc:
        return f"No image or video model matches `{model}`.\n\n" + format_video_model_table(
            exc.candidates, limit=25
        )
    except (OpenRouterError, ConfigError) as exc:
        return f"Could not load providers: {exc}"

    lines = ["| provider slug | name |", "| --- | --- |"]
    for provider in providers:
        lines.append(f"| `{endpoint_slug(provider)}` | {provider.get('name') or '-'} |")
    return "\n".join(lines)


@server.tool(
    title="Generate image",
    description=(
        "Generate an image with an OpenRouter image model and save it to disk. "
        "`model` takes a slug from `list_image_models` (e.g. google/gemini-3.1-flash-image for "
        "Nano Banana 2); `providers` pins the upstream provider(s) from `list_providers` "
        "(e.g. ['google-vertex']). Pass `reference_images` (local paths, http URLs or data URLs) "
        "to edit or vary existing images. Returns the saved file path plus a downscaled preview."
    ),
    structured_output=False,
)
async def generate_image(
    prompt: str,
    model: str = "",
    providers: list[str] | None = None,
    allow_fallbacks: bool | None = None,
    provider_sort: str = "",
    n: int = 1,
    aspect_ratio: str = "",
    resolution: str = "",
    quality: str = "",
    output_format: str = "",
    background: str = "",
    seed: int | None = None,
    reference_images: list[str] | None = None,
    save_dir: str = "",
    name_prefix: str = "",
    inline_preview: bool | None = None,
    extra_body: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> list[TextContent | ImageContent]:
    settings = get_settings()
    try:
        resolved_model = await _resolve_model(model, ctx)
    except NeedsSelection as exc:
        return [TextContent(type="text", text=exc.text)]
    except (OpenRouterError, ConfigError) as exc:
        return _error_text(exc)

    try:
        chosen_providers = await _resolve_providers(resolved_model, providers, ctx)
        request = ImageRequest(
            prompt=prompt,
            model=resolved_model,
            providers=chosen_providers,
            allow_fallbacks=allow_fallbacks,
            provider_sort=provider_sort or None,
            n=n,
            aspect_ratio=aspect_ratio or None,
            resolution=resolution or None,
            quality=quality or None,
            output_format=output_format or None,
            background=background or None,
            seed=seed,
            reference_images=tuple(reference_images or ()),
            extra=extra_body or {},
        )
        result, saved = await generate(
            get_client(),
            settings,
            request,
            output_dir=Path(save_dir).expanduser() if save_dir else None,
            name_prefix=name_prefix or None,
        )
    except (OpenRouterError, ConfigError, ResponseParseError, ImageFileError, ValueError) as exc:
        return _error_text(exc)

    show_inline = settings.inline_preview if inline_preview is None else inline_preview
    lines = [
        f"Generated {len(saved)} image(s) with `{result.model or resolved_model}`"
        + (f" via `{result.provider}`" if result.provider else "")
        + ".",
    ]
    if chosen_providers:
        lines.append(f"Requested provider(s): {', '.join(chosen_providers)}")
    for item in saved:
        size = f"{item.size[0]}x{item.size[1]}" if item.size else "unknown size"
        lines.append(
            f"- `{item.path}` ({size}, {imaging.human_size(len(item.data))}, {item.media_type})"
        )
    if result.cost is not None:
        lines.append(f"Cost: ${result.cost:.4f}")
    if result.text:
        lines.append(f"\nModel note: {result.text[:500]}")
    lines.append(f"\nMetadata sidecars: {saved[0].sidecar.name} (and one per image).")

    content: list[TextContent | ImageContent] = [TextContent(type="text", text="\n".join(lines))]
    for item in saved:
        preview = _preview_content(item, settings, show_inline)
        if preview is not None:
            content.append(preview)
    return content


@server.tool(
    title="Edit image",
    description=(
        "Edit or vary existing images: pass one or more `reference_images` (local paths, http "
        "URLs or data URLs) plus an instruction. Uses the same models as `generate_image` - "
        "make sure the chosen model accepts reference images (see `describe_image_model`)."
    ),
    structured_output=False,
)
async def edit_image(
    prompt: str,
    reference_images: list[str],
    model: str = "",
    providers: list[str] | None = None,
    aspect_ratio: str = "",
    resolution: str = "",
    output_format: str = "",
    save_dir: str = "",
    name_prefix: str = "",
    inline_preview: bool | None = None,
    ctx: Context | None = None,
) -> list[TextContent | ImageContent]:
    if not reference_images:
        return [
            TextContent(
                type="text",
                text="edit_image needs at least one reference image; use generate_image instead.",
            )
        ]
    try:
        resolved_model = await _resolve_model(model, ctx)
    except NeedsSelection as exc:
        return [TextContent(type="text", text=exc.text)]
    except (OpenRouterError, ConfigError) as exc:
        return _error_text(exc)

    try:
        model_info = await get_catalog().resolve(resolved_model)
        limit = max_reference_images(model_info)
        if limit and len(reference_images) > limit:
            return [
                TextContent(
                    type="text",
                    text=f"`{resolved_model}` accepts at most {limit} reference image(s), "
                    f"but {len(reference_images)} were given.",
                )
            ]
    except (ModelNotFoundError, OpenRouterError, ConfigError):
        pass

    return await generate_image(
        prompt=prompt,
        model=resolved_model,
        providers=providers,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        output_format=output_format,
        reference_images=reference_images,
        save_dir=save_dir,
        name_prefix=name_prefix or "edit",
        inline_preview=inline_preview,
        ctx=ctx,
    )


@server.tool(
    title="Show image",
    description=(
        "Return a local image file as an inline (downscaled) preview so it can be looked at "
        "during a design session. Works for any image on disk, not only generated ones."
    ),
    structured_output=False,
)
async def show_image(path: str, max_pixels: int = 0) -> list[TextContent | ImageContent]:
    settings = get_settings()
    try:
        data, media_type = imaging.read_local_image(path)
    except ImageFileError as exc:
        return [TextContent(type="text", text=str(exc))]

    size = imaging.image_dimensions(data)
    preview = imaging.make_preview(data, media_type, max_pixels or settings.preview_max_px)
    header = TextContent(
        type="text",
        text=f"`{path}` - {media_type}, {imaging.human_size(len(data))}"
        + (f", {size[0]}x{size[1]}" if size else ""),
    )
    if preview is None:
        return [
            header,
            TextContent(type="text", text="(no inline preview available for this format)"),
        ]
    return [header, _image_content(*preview)]


@server.tool(
    title="List generated images",
    description="List the most recently generated images in the output directory.",
)
async def list_generated_images(limit: int = 10, save_dir: str = "") -> str:
    settings = get_settings()
    directory = Path(save_dir).expanduser() if save_dir else settings.output_dir
    if not directory.is_dir():
        return f"No images yet - `{directory}` does not exist."
    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in imaging.EXTENSIONS.values()
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return f"No images in `{directory}`."
    lines = [f"Most recent images in `{directory}`:"]
    for path in files[: max(1, limit)]:
        lines.append(f"- `{path}` ({imaging.human_size(path.stat().st_size)})")
    return "\n".join(lines)


# ------------------------------------------------------------------ video tools


def _resume_hint(job_id: str) -> str:
    return (
        f'Call `check_video_job(job_id="{job_id}")` to keep waiting and download the result - '
        "do not resubmit, the job is already paid for."
    )


def _store_record(record: dict[str, Any]) -> None:
    try:
        video.save_job_record(get_settings(), record)
    except OSError as exc:  # a read-only output dir must not lose the result
        logger.warning("could not store video job record: %s", exc)


async def _video_provider_slugs(model: str, provider: str) -> tuple[str, ...]:
    """Which provider blocks `provider_options` go into."""
    if provider.strip():
        return (provider.strip(),)
    try:
        data = await get_catalog().endpoints(model)
    except OpenRouterError:
        return ()
    return tuple(endpoint_slug(e) for e in data.get("endpoints") or [] if isinstance(e, dict))


async def _video_result(
    job_id: str,
    record: dict[str, Any],
    items: list[tuple[Path, int, str, media.VideoInfo | None]],
    *,
    cost: float | None,
    show_inline: bool,
    verb: str = "Generated",
) -> list[TextContent | ImageContent]:
    settings = get_settings()
    previews: list[ImageContent] = []
    if show_inline:
        for path, _size, _media_type, info in items:
            sheet = await asyncio.to_thread(
                media.contact_sheet,
                path,
                settings.preview_max_px,
                duration=info.duration if info else None,
            )
            if sheet is not None:
                previews.append(_image_content(*sheet))

    lines = [f"{verb} {len(items)} video(s) with `{record.get('model') or '?'}` (job `{job_id}`)."]
    for path, size, media_type, info in items:
        details = ([info.describe()] if info else []) + [imaging.human_size(size), media_type]
        lines.append(f"- `{path}` ({', '.join(details)})")
    if cost is not None:
        lines.append(f"Cost: ${cost:.4f}")
    lines.append(f"\nMetadata sidecars: {items[0][0].name}.json (one per video).")
    if previews:
        lines.append("Preview: 4 evenly spaced frames per video, left-to-right, top-to-bottom.")
    elif show_inline and not media.ffmpeg_available():
        lines.append("(Install ffmpeg to get an inline frame preview.)")
    return [TextContent(type="text", text="\n".join(lines)), *previews]


async def _finish_video_job(
    job_id: str,
    record: dict[str, Any],
    *,
    max_wait: float,
    show_inline: bool,
    ctx: Context | None,
) -> list[TextContent | ImageContent]:
    """Poll a submitted job, then download and describe its outputs."""
    settings = get_settings()
    client = get_client()

    async def report(status: dict[str, Any], elapsed: float) -> None:
        if ctx is None:
            return
        try:
            await ctx.report_progress(
                min(elapsed, max_wait),
                max_wait,
                f"video job {status.get('status') or '?'} after {elapsed:.0f}s",
            )
        except Exception as exc:  # progress is best effort
            logger.debug("progress notification failed: %s", exc)

    try:
        status, elapsed = await video.wait_for_job(
            client,
            job_id,
            interval=settings.video_poll_interval,
            max_wait=max_wait,
            on_progress=report,
        )
    except (OpenRouterError, ConfigError) as exc:
        text = f"Could not poll video job `{job_id}`: {exc}\n{_resume_hint(job_id)}"
        return [TextContent(type="text", text=text)]

    state = str(status.get("status") or "unknown")
    model = record.get("model") or "?"
    record["status"] = state
    if status.get("generation_id"):
        record["generation_id"] = status["generation_id"]
    if status.get("error"):
        record["error"] = status["error"]

    if state not in video.TERMINAL_STATUSES:
        _store_record(record)
        text = (
            f"Video job `{job_id}` (`{model}`) is still `{state}` after {elapsed:.0f}s; it keeps "
            f"running on OpenRouter.\n{_resume_hint(job_id)}"
        )
        return [TextContent(type="text", text=text)]

    cost = video.job_cost(status)
    if state != "completed":
        _store_record(record)
        text = f"Video job `{job_id}` (`{model}`) ended as `{state}`: " + (
            f"{status.get('error') or 'no reason given'}."
        )
        if cost is not None:
            text += f"\nCost: ${cost:.4f}"
        return [TextContent(type="text", text=text)]

    record["usage"] = status.get("usage")
    try:
        saved = await video.download_outputs(client, settings, status, record)
    except (OpenRouterError, ConfigError, OSError) as exc:
        _store_record(record)
        text = (
            f"Video job `{job_id}` completed, but saving it failed: {exc}\n"
            f"{_resume_hint(job_id)}"
        )
        return [TextContent(type="text", text=text)]

    record["files"] = [str(item.path) for item in saved]
    _store_record(record)
    items = [(s.path, s.size_bytes, s.media_type, s.info) for s in saved]
    return await _video_result(job_id, record, items, cost=cost, show_inline=show_inline)


@server.tool(
    title="List video models",
    description=(
        "List OpenRouter video generation models with price, durations, resolutions, accepted "
        "frame images and audio support. Use `query` to filter, e.g. 'veo', 'kling', 'seedance'."
    ),
)
async def list_video_models(query: str = "", limit: int = 30, refresh: bool = False) -> str:
    try:
        models = await get_catalog().search_videos(query, refresh=refresh)
    except (OpenRouterError, ConfigError) as exc:
        return f"Could not load the video model list: {exc}"
    header = (
        f"{len(models)} video model(s) matching `{query}`:"
        if query
        else f"{len(models)} video models:"
    )
    return f"{header}\n\n{format_video_model_table(models, limit=max(1, limit))}"


@server.tool(
    title="Describe video model",
    description=(
        "Show everything one video model supports: durations, resolutions, aspect ratios, sizes, "
        "first/last frame images, audio, seed, upscaling, pricing SKUs, the model-specific "
        "`provider_options` it accepts (e.g. negativePrompt, cfg_scale) and its providers."
    ),
)
async def describe_video_model(model: str) -> str:
    catalog = get_catalog()
    try:
        resolved = await catalog.resolve_video(model)
    except ModelNotFoundError as exc:
        return (
            f"No video model matches `{model}`.\n\n"
            f"{format_video_model_table(exc.candidates, limit=30)}"
        )
    except (OpenRouterError, ConfigError) as exc:
        return f"Could not load model details: {exc}"

    slug = model_slug(resolved)
    text = format_video_model_details(resolved)
    try:
        endpoints = await catalog.endpoints(slug)
        text = (
            f"{text}\n\n{format_endpoints(slug, endpoints)}\n\n"
            "For video, a provider slug is only needed as `provider` together with "
            "`provider_options`, and even then it is looked up automatically."
        )
    except OpenRouterError as exc:
        text = f"{text}\n\n(Provider list unavailable: {exc})"
    return text


@server.tool(
    title="Generate video",
    description=(
        "Generate a video with an OpenRouter video model (Veo 3.1, Kling 3.0, Seedance 2.x, "
        "Sora 2, Wan, Hailuo, ...) and save it to disk. Modes, combinable where the model "
        "allows: text-to-video (`prompt`); image-to-video via `first_frame` and/or "
        "`last_frame`; reference-to-video via `reference_images` (characters, products, "
        "style), `reference_videos` (motion/camera/effect templates or clips to continue) and "
        "`reference_audios` (music, sound or voice to sync to) - video and audio references are "
        "only honoured by models that support them, e.g. Seedance 2.x. All inputs take local "
        "paths, http URLs or data URLs. `duration`, `resolution`, `aspect_ratio`, `size`, "
        "`seed`, `generate_audio` are checked against `describe_video_model`; model-specific "
        "controls go into `provider_options` (e.g. {'personGeneration': 'allow'} for Veo, "
        "{'cfg_scale': 0.5} for Kling), `negative_prompt` is mapped to the model's spelling. "
        "Jobs take 30s to minutes: with `wait` the tool polls up to `max_wait_seconds` and "
        "returns the file path plus a frame preview, otherwise a job id for `check_video_job`."
    ),
    structured_output=False,
)
async def generate_video(
    prompt: str = "",
    model: str = "",
    duration: int | None = None,
    resolution: str = "",
    aspect_ratio: str = "",
    size: str = "",
    generate_audio: bool | None = None,
    seed: int | None = None,
    first_frame: str = "",
    last_frame: str = "",
    reference_images: list[str] | None = None,
    reference_videos: list[str] | None = None,
    reference_audios: list[str] | None = None,
    negative_prompt: str = "",
    provider_options: dict[str, Any] | None = None,
    provider: str = "",
    upscale_factor: float | None = None,
    creativity: int | None = None,
    callback_url: str = "",
    wait: bool = True,
    max_wait_seconds: int = 0,
    save_dir: str = "",
    name_prefix: str = "",
    inline_preview: bool | None = None,
    extra_body: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> list[TextContent | ImageContent]:
    settings = get_settings()
    try:
        resolved_model = await _resolve_model(model, ctx, kind="video")
    except NeedsSelection as exc:
        return [TextContent(type="text", text=exc.text)]
    except (OpenRouterError, ConfigError) as exc:
        return _error_text(exc, "Video generation")

    request = video.VideoRequest(
        model=resolved_model,
        prompt=prompt,
        duration=duration,
        resolution=resolution or None,
        aspect_ratio=aspect_ratio or None,
        size=size or None,
        generate_audio=generate_audio,
        seed=seed,
        first_frame=first_frame or None,
        last_frame=last_frame or None,
        reference_images=tuple(reference_images or ()),
        reference_videos=tuple(reference_videos or ()),
        reference_audios=tuple(reference_audios or ()),
        negative_prompt=negative_prompt or None,
        provider_options=dict(provider_options or {}),
        upscale_factor=upscale_factor,
        creativity=creativity,
        callback_url=callback_url or None,
        extra=dict(extra_body or {}),
    )
    output_dir = Path(save_dir).expanduser() if save_dir else None
    try:
        model_info = await get_catalog().resolve_video(resolved_model)
        slugs = (
            await _video_provider_slugs(resolved_model, provider)
            if request.needs_provider_slug
            else ()
        )
        payload = video.build_video_payload(request, model_info, slugs)
        job = await get_client().create_video(payload)
    except (
        OpenRouterError,
        ConfigError,
        ImageFileError,
        ModelNotFoundError,
        ValueError,
    ) as exc:
        return _error_text(exc, "Video generation")

    job_id = str(job["id"])
    record = video.new_job_record(
        job, request, save_dir=output_dir, name_prefix=name_prefix or None
    )
    _store_record(record)
    if not wait:
        text = (
            f"Submitted video job `{job_id}` with `{resolved_model}` (status "
            f"`{record['status']}`). Generation usually takes 30 seconds to several minutes.\n"
            f"{_resume_hint(job_id)}"
        )
        return [TextContent(type="text", text=text)]

    show_inline = settings.inline_preview if inline_preview is None else inline_preview
    max_wait = float(max_wait_seconds) if max_wait_seconds > 0 else settings.video_max_wait
    return await _finish_video_job(
        job_id, record, max_wait=max_wait, show_inline=show_inline, ctx=ctx
    )


@server.tool(
    title="Edit video",
    description=(
        "Edit, restyle, extend or upscale an existing clip: `source_video` (local path, http URL "
        "or data URL) is sent as the leading video reference, plus an instruction in `prompt`. "
        "Pick a model that takes video input - e.g. black-forest-labs/flux-video-edit, "
        "runway/aleph-2, Seedance 2.x for reference-driven remakes, or "
        "black-forest-labs/flux-video-upscale with `upscale_factor` / `creativity`. "
        "Waits and saves like `generate_video`."
    ),
    structured_output=False,
)
async def edit_video(
    source_video: str,
    prompt: str = "",
    model: str = "",
    reference_images: list[str] | None = None,
    reference_audios: list[str] | None = None,
    duration: int | None = None,
    resolution: str = "",
    aspect_ratio: str = "",
    seed: int | None = None,
    upscale_factor: float | None = None,
    creativity: int | None = None,
    negative_prompt: str = "",
    provider_options: dict[str, Any] | None = None,
    provider: str = "",
    wait: bool = True,
    max_wait_seconds: int = 0,
    save_dir: str = "",
    name_prefix: str = "",
    inline_preview: bool | None = None,
    extra_body: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> list[TextContent | ImageContent]:
    if not source_video.strip():
        return [TextContent(type="text", text="edit_video needs a `source_video`.")]
    return await generate_video(
        prompt=prompt,
        model=model,
        duration=duration,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        seed=seed,
        reference_images=reference_images,
        reference_videos=[source_video],
        reference_audios=reference_audios,
        negative_prompt=negative_prompt,
        provider_options=provider_options,
        provider=provider,
        upscale_factor=upscale_factor,
        creativity=creativity,
        wait=wait,
        max_wait_seconds=max_wait_seconds,
        save_dir=save_dir,
        name_prefix=name_prefix or "edit",
        inline_preview=inline_preview,
        extra_body=extra_body,
        ctx=ctx,
    )


@server.tool(
    title="Check video job",
    description=(
        "Resume a video job that `generate_video` / `edit_video` returned as a job id: polls "
        "its status (waiting up to `max_wait_seconds` when `wait` is true) and downloads the "
        "finished video. Calling it again for a downloaded job just returns the saved files."
    ),
    structured_output=False,
)
async def check_video_job(
    job_id: str,
    wait: bool = True,
    max_wait_seconds: int = 0,
    save_dir: str = "",
    inline_preview: bool | None = None,
    ctx: Context | None = None,
) -> list[TextContent | ImageContent]:
    settings = get_settings()
    job_id = job_id.strip()
    if not job_id:
        return [TextContent(type="text", text="`job_id` is required.")]
    record = video.load_job_record(settings, job_id) or {"job_id": job_id}
    if save_dir:
        record["save_dir"] = str(Path(save_dir).expanduser())
    show_inline = settings.inline_preview if inline_preview is None else inline_preview

    files = [Path(p) for p in record.get("files") or []]
    if files and all(p.is_file() for p in files):
        items = []
        for path in files:
            info = await asyncio.to_thread(media.probe_video, path)
            media_type = media.guess_media_type(path) or "video/mp4"
            items.append((path, path.stat().st_size, media_type, info))
        return await _video_result(
            job_id,
            record,
            items,
            cost=video.job_cost(record),
            show_inline=show_inline,
            verb="Already downloaded",
        )

    if wait:
        max_wait = float(max_wait_seconds) if max_wait_seconds > 0 else settings.video_max_wait
    else:
        max_wait = 0.0
    return await _finish_video_job(
        job_id, record, max_wait=max_wait, show_inline=show_inline, ctx=ctx
    )


@server.tool(
    title="List generated videos",
    description=(
        "List unfinished video jobs (resume them with `check_video_job`) and the most recently "
        "saved videos."
    ),
)
async def list_generated_videos(limit: int = 10, save_dir: str = "") -> str:
    settings = get_settings()
    directory = Path(save_dir).expanduser() if save_dir else settings.videos_dir
    lines: list[str] = []

    records = video.list_job_records(settings)
    pending = [r for r in records if r.get("status") not in video.TERMINAL_STATUSES]
    if pending:
        lines.append("Unfinished video jobs (resume with `check_video_job`):")
        for record in pending:
            prompt = str(record.get("prompt") or "")[:80]
            lines.append(
                f"- `{record['job_id']}` - `{record.get('model') or '?'}`, "
                f"{record.get('status')}, submitted {record.get('created_at') or '?'}: {prompt}"
            )
        lines.append("")

    extensions = set(media.VIDEO_EXTENSIONS.values())
    files = (
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in extensions]
        if directory.is_dir()
        else []
    )
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        lines.append(f"No videos in `{directory}` yet.")
        return "\n".join(lines)
    lines.append(f"Most recent videos in `{directory}`:")
    for path in files[: max(1, limit)]:
        lines.append(f"- `{path}` ({imaging.human_size(path.stat().st_size)})")
    return "\n".join(lines)


@server.tool(
    title="Show video",
    description=(
        "Inspect a local video file: duration, resolution, audio track, plus an inline preview "
        "of 4 evenly spaced frames. Works for generated clips and for reference videos. "
        "Needs ffmpeg for the preview."
    ),
    structured_output=False,
)
async def show_video(path: str, max_pixels: int = 0) -> list[TextContent | ImageContent]:
    settings = get_settings()
    file = Path(path).expanduser()
    if not file.is_file():
        return [TextContent(type="text", text=f"No such video: {file}")]
    info = await asyncio.to_thread(media.probe_video, file)
    header = (
        f"`{file}` - {media.guess_media_type(file) or 'unknown type'}, "
        f"{imaging.human_size(file.stat().st_size)}" + (f", {info.describe()}" if info else "")
    )
    sheet = await asyncio.to_thread(
        media.contact_sheet,
        file,
        max_pixels or settings.preview_max_px,
        duration=info.duration if info else None,
    )
    if sheet is None:
        reason = "the file could not be decoded" if media.ffmpeg_available() else "install ffmpeg"
        return [
            TextContent(type="text", text=header),
            TextContent(type="text", text=f"(no preview - {reason})"),
        ]
    return [TextContent(type="text", text=header), _image_content(*sheet)]


@server.tool(
    title="OpenRouter status",
    description="Show the configured defaults plus the API key's credit/limit information.",
)
async def openrouter_status() -> str:
    settings = get_settings()
    lines = [
        "**Configuration**",
        f"- base URL: `{settings.base_url}`",
        f"- default model: `{settings.default_model or '(none - the user is asked)'}`",
        f"- default providers: {', '.join(settings.default_providers) or '(automatic routing)'}",
        f"- allow provider fallbacks: {settings.allow_fallbacks}",
        f"- output directory: `{settings.output_dir}`",
        f"- inline previews: {settings.inline_preview} (max {settings.preview_max_px}px)",
        f"- default video model: `{settings.default_video_model or '(none - the user is asked)'}`",
        f"- video output directory: `{settings.videos_dir}`",
        f"- video polling: every {settings.video_poll_interval:.0f}s, "
        f"waiting up to {settings.video_max_wait:.0f}s per call",
        f"- ffmpeg (video previews): {'yes' if media.ffmpeg_available() else 'not found'}",
        f"- API key configured: {'yes' if settings.api_key else 'NO - set OPENROUTER_API_KEY'}",
    ]
    try:
        key_info = await get_client().get_key_info()
    except (OpenRouterError, ConfigError) as exc:
        lines.append(f"\nCould not read the key info: {exc}")
        return "\n".join(lines)

    if key_info:
        lines.append("\n**API key**")
        for field_name in ("label", "usage", "limit", "limit_remaining", "is_free_tier"):
            if field_name in key_info:
                lines.append(f"- {field_name}: {key_info[field_name]}")
    return "\n".join(lines)


@server.resource(
    "openrouter://image-models",
    name="OpenRouter image models",
    description="Markdown table of all image capable models with their slugs and pricing.",
    mime_type="text/markdown",
)
async def image_models_resource() -> str:
    try:
        models = await get_catalog().image_models()
    except (OpenRouterError, ConfigError) as exc:
        return f"Could not load the model list: {exc}"
    return format_model_table(models)


@server.resource(
    "openrouter://video-models",
    name="OpenRouter video models",
    description="Markdown table of all video models with capabilities and pricing.",
    mime_type="text/markdown",
)
async def video_models_resource() -> str:
    try:
        models = await get_catalog().video_models()
    except (OpenRouterError, ConfigError) as exc:
        return f"Could not load the video model list: {exc}"
    return format_video_model_table(models)
