"""MCP server exposing OpenRouter image generation to Claude Code and other clients."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import AcceptedElicitation, Context, MCPServer
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, Field

from . import imaging
from .catalog import (
    ModelCatalog,
    ModelNotFoundError,
    endpoint_slug,
    format_endpoints,
    format_model_details,
    format_model_table,
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
Generate and edit images through OpenRouter (Nano Banana, GPT Image, Seedream, Flux, ...).

Workflow:
1. `list_image_models` shows which models can produce images; `list_providers` shows which
   upstream providers serve a given model (e.g. Google Vertex vs Google AI Studio).
2. Ask the user which model and provider they want when it matters, then pass `model` and
   `providers` to `generate_image` or `edit_image`.
3. Images are written to the output directory and the file path is returned, so they can be
   re-read, committed to a repo, or referenced in later edits.

If no model is given and no default is configured, the tools return a list of candidates
instead of guessing - present them to the user and ask.
"""

SELECTION_HINT = (
    "No model was selected. Pick one of the models below, ask the user which one they want, "
    'and call the tool again with `model="<slug>"`. '
    "You can also set OPENROUTER_IMAGE_MODEL in the server environment to define a default."
)

server = MCPServer(
    name="openrouter-image",
    title="OpenRouter Image Generation",
    instructions=INSTRUCTIONS,
    version="0.1.0",
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


async def _ask_for_model(ctx: Context | None, candidates: list[dict[str, Any]]) -> str | None:
    """Try MCP elicitation; returns None when the client does not support it."""
    if ctx is None:
        return None
    preview = ", ".join(model_slug(m) for m in candidates[:8])
    try:
        result = await ctx.elicit(
            message=f"Which OpenRouter image model should be used? Candidates: {preview}",
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


async def _resolve_model(model: str, ctx: Context | None) -> str:
    """Turn user input (slug, friendly name or nothing) into a concrete model slug."""
    catalog = get_catalog()
    settings = get_settings()
    query = (model or "").strip() or (settings.default_model or "")

    if not query:
        candidates = await catalog.image_models()
        chosen = await _ask_for_model(ctx, candidates)
        if not chosen:
            raise NeedsSelection(f"{SELECTION_HINT}\n\n{format_model_table(candidates, limit=25)}")
        query = chosen

    try:
        return model_slug(await catalog.resolve(query))
    except ModelNotFoundError as exc:
        raise NeedsSelection(
            f"No image model matches `{query}`. Pick one of these and call the tool again:\n\n"
            f"{format_model_table(exc.candidates, limit=25)}"
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
    import base64

    return ImageContent(
        type="image",
        data=base64.b64encode(data).decode("ascii"),
        mime_type=media_type,
    )


def _error_text(exc: Exception) -> list[TextContent]:
    return [TextContent(type="text", text=f"Image generation failed: {exc}")]


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
            resolved = model_slug(await catalog.resolve(model))
            return format_endpoints(resolved, await catalog.endpoints(resolved, refresh=refresh))
        providers = await get_client().list_providers()
    except ModelNotFoundError as exc:
        return (
            f"No image model matches `{model}`.\n\n{format_model_table(exc.candidates, limit=25)}"
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
    import base64

    return [
        header,
        ImageContent(
            type="image",
            data=base64.b64encode(preview[0]).decode("ascii"),
            mime_type=preview[1],
        ),
    ]


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
