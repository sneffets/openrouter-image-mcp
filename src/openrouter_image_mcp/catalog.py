"""Model and provider discovery, caching and human readable formatting."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .client import OpenRouterClient

CACHE_TTL_SECONDS = 600.0


class ModelNotFoundError(LookupError):
    """Raised when a requested model slug cannot be matched to an image model."""

    def __init__(self, query: str, candidates: Sequence[dict[str, Any]]) -> None:
        super().__init__(f"No image model matches {query!r}.")
        self.query = query
        self.candidates = list(candidates)


def model_slug(model: dict[str, Any]) -> str:
    for key in ("id", "slug", "model", "canonical_slug", "name"):
        value = model.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def model_name(model: dict[str, Any]) -> str:
    value = model.get("name")
    return value if isinstance(value, str) and value else model_slug(model)


def _price(model: dict[str, Any]) -> str:
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return "-"
    parts: list[str] = []
    per_image = pricing.get("image") or pricing.get("image_output") or pricing.get("output_image")
    if per_image not in (None, "", "0"):
        parts.append(f"{_usd(per_image)}/img")
    prompt = pricing.get("prompt")
    if prompt not in (None, "", "0"):
        parts.append(f"{_usd(prompt, per_million=True)}/M in")
    completion = pricing.get("completion")
    if completion not in (None, "", "0"):
        parts.append(f"{_usd(completion, per_million=True)}/M out")
    return ", ".join(parts) if parts else "-"


def _usd(value: Any, *, per_million: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if per_million:
        number *= 1_000_000
    if number >= 1:
        return f"${number:.2f}"
    return f"${number:.4f}".rstrip("0").rstrip(".")


def _supported_parameters(model: dict[str, Any]) -> list[str]:
    params = model.get("supported_parameters")
    if isinstance(params, list):
        return [str(p) for p in params]
    if isinstance(params, dict):
        return sorted(str(k) for k in params)
    return []


def max_reference_images(model: dict[str, Any]) -> int | None:
    """How many reference images the model accepts, when the catalogue says so."""
    params = model.get("supported_parameters")
    if isinstance(params, dict):
        descriptor = params.get("input_references")
        if isinstance(descriptor, dict):
            value = descriptor.get("max") or descriptor.get("maximum")
            if isinstance(value, (int, float)):
                return int(value)
        if isinstance(descriptor, (int, float)):
            return int(descriptor)
    value = model.get("max_input_references")
    return int(value) if isinstance(value, (int, float)) else None


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class ModelCatalog:
    """Cached view of the image models and their providers."""

    def __init__(self, client: OpenRouterClient, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._client = client
        self._ttl = ttl
        self._cache: dict[str, _CacheEntry] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    async def _cached(self, key: str, loader: Any, refresh: bool = False) -> Any:
        entry = self._cache.get(key)
        if not refresh and entry is not None and entry.expires_at > time.monotonic():
            return entry.value
        value = await loader()
        self._cache[key] = _CacheEntry(value, time.monotonic() + self._ttl)
        return value

    async def image_models(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return await self._cached("image_models", self._client.list_image_models, refresh)

    async def endpoints(self, model: str, *, refresh: bool = False) -> dict[str, Any]:
        return await self._cached(
            f"endpoints:{model}", lambda: self._client.list_model_endpoints(model), refresh
        )

    async def search(self, query: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        models = await self.image_models(refresh=refresh)
        return filter_models(models, query)

    async def resolve(self, query: str, *, refresh: bool = False) -> dict[str, Any]:
        """Resolve a slug or a friendly name (``"nano banana 2"``) to one model."""
        models = await self.image_models(refresh=refresh)
        wanted = query.strip().lower()
        for model in models:
            if model_slug(model).lower() == wanted:
                return model
        matches = filter_models(models, query)
        exact_names = [m for m in matches if model_name(m).lower() == wanted]
        if exact_names:
            return exact_names[0]
        if len(matches) == 1:
            return matches[0]
        raise ModelNotFoundError(query, matches or models)


def filter_models(models: Iterable[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Match models on the query, ignoring punctuation and casing.

    A contiguous phrase match wins over a loose all-terms match, so that
    ``"nano banana 2"`` does not also pull in ``"Nano Banana (Gemini 2.5 ...)"``.
    """
    phrase = " ".join(_normalise(query).split())
    if not phrase:
        return list(models)
    terms = phrase.split()

    exact: list[dict[str, Any]] = []
    loose: list[dict[str, Any]] = []
    for model in models:
        haystack = " ".join(
            _normalise(
                " ".join(
                    [model_slug(model), model_name(model), str(model.get("description") or "")]
                )
            ).split()
        )
        if phrase in haystack:
            exact.append(model)
        elif all(term in haystack for term in terms):
            loose.append(model)
    return exact or loose


def _normalise(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text.lower())


# ------------------------------------------------------------------ rendering


def format_model_table(models: Sequence[dict[str, Any]], *, limit: int | None = None) -> str:
    shown = list(models)[:limit] if limit else list(models)
    if not shown:
        return "No image models matched."
    lines = [
        "| model (use this slug) | name | price | notes |",
        "| --- | --- | --- | --- |",
    ]
    for model in shown:
        refs = max_reference_images(model)
        notes = []
        if refs:
            notes.append(f"up to {refs} reference images")
        params = _supported_parameters(model)
        interesting = [p for p in params if p in {"aspect_ratio", "resolution", "quality", "seed"}]
        if interesting:
            notes.append(", ".join(sorted(interesting)))
        lines.append(
            f"| `{model_slug(model)}` | {model_name(model)} | {_price(model)} | "
            f"{'; '.join(notes) or '-'} |"
        )
    if limit and len(models) > limit:
        lines.append("")
        lines.append(f"... {len(models) - limit} more; narrow the search with `query`.")
    return "\n".join(lines)


def format_model_details(model: dict[str, Any]) -> str:
    slug = model_slug(model)
    architecture = model.get("architecture") or {}
    lines = [
        f"# {model_name(model)}",
        "",
        f"- **slug**: `{slug}`",
        f"- **price**: {_price(model)}",
    ]
    inputs = architecture.get("input_modalities") or model.get("input_modalities")
    outputs = architecture.get("output_modalities") or model.get("output_modalities")
    if inputs:
        lines.append(f"- **input modalities**: {', '.join(map(str, inputs))}")
    if outputs:
        lines.append(f"- **output modalities**: {', '.join(map(str, outputs))}")
    refs = max_reference_images(model)
    if refs:
        lines.append(f"- **max reference images**: {refs}")
    params = _supported_parameters(model)
    if params:
        lines.append(f"- **supported parameters**: {', '.join(sorted(params))}")
    description = model.get("description")
    if isinstance(description, str) and description.strip():
        lines += ["", description.strip()[:800]]
    return "\n".join(lines)


def endpoint_slug(endpoint: dict[str, Any]) -> str:
    """The value to pass in ``provider.order`` / ``provider.only``."""
    for key in ("tag", "provider_slug", "slug", "provider_tag"):
        value = endpoint.get(key)
        if isinstance(value, str) and value:
            return value
    name = endpoint.get("provider_name") or endpoint.get("name") or ""
    return str(name).split("|")[0].strip().lower().replace(" ", "-") or "unknown"


def format_endpoints(model: str, data: dict[str, Any]) -> str:
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        return f"No provider endpoints reported for `{model}`."
    lines = [
        f"Providers serving `{model}` (pass the slug as `providers=[...]`):",
        "",
        "| provider slug | provider | price | status |",
        "| --- | --- | --- | --- |",
    ]
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        status = endpoint.get("status")
        uptime = endpoint.get("uptime_last_30m")
        status_text = "ok" if status in (None, 0) else str(status)
        if isinstance(uptime, (int, float)):
            status_text += f" ({uptime:.0f}% uptime/30m)"
        lines.append(
            f"| `{endpoint_slug(endpoint)}` | "
            f"{endpoint.get('provider_name') or endpoint.get('name') or '-'} | "
            f"{_price(endpoint)} | {status_text} |"
        )
    return "\n".join(lines)
