"""Thin async HTTP client for the OpenRouter REST API."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .config import Settings

RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.5


class OpenRouterError(RuntimeError):
    """An error returned by OpenRouter (or by the transport talking to it)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _error_message(response: httpx.Response) -> str:
    """Pull the most useful message out of an OpenRouter error body."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        text = response.text.strip()
        return text[:500] or f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or ""
        metadata = error.get("metadata")
        if metadata:
            message = f"{message} ({json.dumps(metadata)[:300]})"
        if message:
            return str(message)
    return json.dumps(payload)[:500]


class OpenRouterClient:
    """Small wrapper around the OpenRouter endpoints this server needs."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=settings.timeout)

    async def aclose(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ HTTP

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_status: frozenset[int] = frozenset(),
        idempotent: bool = True,
    ) -> tuple[int, Any]:
        """Perform a request and return ``(status_code, parsed_body)``.

        Statuses listed in ``allow_status`` are returned to the caller instead of
        raising, which is how the fallback from ``/images`` to
        ``/chat/completions`` is implemented.

        ``idempotent=False`` is for calls that start paid jobs: only a 429 (rejected
        before any work started) is retried, never a timeout or a 5xx, because the
        job may already be running and a retry would pay for it twice.
        """
        url = f"{self.settings.base_url}/{path.lstrip('/')}"
        headers = self.settings.headers()
        retry_status = RETRY_STATUS if idempotent else frozenset({429})
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=self.settings.timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS or not idempotent:
                    raise OpenRouterError(
                        f"Request to {url} timed out after {self.settings.timeout:.0f}s. "
                        "Image models can be slow - raise OPENROUTER_IMAGE_TIMEOUT if needed."
                    ) from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS or not idempotent:
                    raise OpenRouterError(f"Could not reach {url}: {exc}") from exc
            else:
                if response.status_code in allow_status:
                    return response.status_code, _safe_json(response)
                if response.status_code >= 400:
                    if response.status_code in retry_status and attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(BACKOFF_BASE**attempt)
                        continue
                    raise OpenRouterError(
                        f"OpenRouter returned {response.status_code}: {_error_message(response)}",
                        status_code=response.status_code,
                        payload=_safe_json(response),
                    )
                return response.status_code, _safe_json(response)

            await asyncio.sleep(BACKOFF_BASE**attempt)

        raise OpenRouterError(f"Request to {url} failed: {last_error}")  # pragma: no cover

    # --------------------------------------------------------------- reading

    async def list_all_models(self) -> list[dict[str, Any]]:
        _, body = await self.request("GET", "/models")
        return _as_list(body)

    async def list_image_models(self) -> list[dict[str, Any]]:
        """Image capable models, preferring the dedicated image catalogue."""
        status, body = await self.request(
            "GET", "/images/models", allow_status=frozenset({404, 405})
        )
        if status < 400:
            models = _as_list(body)
            if models:
                return models
        return [m for m in await self.list_all_models() if _supports_image_output(m)]

    async def list_model_endpoints(self, model: str) -> dict[str, Any]:
        """Providers (endpoints) that serve a given model slug."""
        _, body = await self.request("GET", f"/models/{model.strip('/')}/endpoints")
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            return body["data"]
        return body if isinstance(body, dict) else {}

    async def list_providers(self) -> list[dict[str, Any]]:
        _, body = await self.request("GET", "/providers")
        return _as_list(body)

    async def get_key_info(self) -> dict[str, Any]:
        _, body = await self.request("GET", "/key")
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else (body if isinstance(body, dict) else {})

    async def get_credits(self) -> dict[str, Any]:
        _, body = await self.request("GET", "/credits")
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else (body if isinstance(body, dict) else {})

    # --------------------------------------------------------------- writing

    async def create_image(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Generate images.

        Uses the unified ``POST /images`` endpoint and transparently falls back to
        ``POST /chat/completions`` with ``modalities: ["image", "text"]`` for
        accounts or deployments where the image endpoint is unavailable.
        Returns ``(endpoint_used, response_body)``.
        """
        status, body = await self.request(
            "POST", "/images", json_body=payload, allow_status=frozenset({404, 405})
        )
        if status < 400:
            return "/images", body if isinstance(body, dict) else {}

        _, chat_body = await self.request(
            "POST", "/chat/completions", json_body=to_chat_payload(payload)
        )
        return "/chat/completions", chat_body if isinstance(chat_body, dict) else {}

    async def download(self, url: str) -> tuple[bytes, str]:
        """Fetch a remote image that a model returned as a URL."""
        response = await self._client.get(url, timeout=self.settings.timeout)
        response.raise_for_status()
        media_type = response.headers.get("content-type", "image/png").split(";")[0].strip()
        return response.content, media_type

    # ----------------------------------------------------------------- video

    async def list_video_models(self) -> list[dict[str, Any]]:
        _, body = await self.request("GET", "/videos/models")
        return _as_list(body)

    async def create_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit a video job; returns ``{id, polling_url, status}``."""
        _, body = await self.request("POST", "/videos", json_body=payload, idempotent=False)
        if not isinstance(body, dict) or not body.get("id"):
            raise OpenRouterError(f"OpenRouter did not return a video job id: {body!r}"[:500])
        return body

    async def get_video_job(self, job_id: str) -> dict[str, Any]:
        _, body = await self.request("GET", f"/videos/{job_id}")
        return body if isinstance(body, dict) else {}

    async def download_video(
        self, job_id: str, index: int = 0, url: str | None = None
    ) -> tuple[bytes, str]:
        """Fetch a finished video. OpenRouter content URLs are not presigned, so they
        get the API key; foreign storage URLs are fetched without it."""
        target = url or f"{self.settings.base_url}/videos/{job_id}/content?index={index}"
        headers = self.settings.headers() if _is_openrouter_url(target, self.settings) else None
        try:
            response = await self._client.get(
                target, headers=headers, timeout=self.settings.timeout, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"Could not download the video from {target}: {exc}") from exc
        if response.status_code >= 400:
            raise OpenRouterError(
                f"Video download returned {response.status_code}: {_error_message(response)}",
                status_code=response.status_code,
            )
        media_type = response.headers.get("content-type", "video/mp4").split(";")[0].strip()
        return response.content, media_type


def to_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an ``/images`` payload into a ``/chat/completions`` payload."""
    content: list[dict[str, Any]] = [{"type": "text", "text": payload.get("prompt", "")}]
    for reference in payload.get("input_references") or []:
        url = reference.get("image_url") if isinstance(reference, dict) else reference
        if isinstance(url, dict):
            url = url.get("url")
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})

    chat: dict[str, Any] = {
        "model": payload["model"],
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": content}],
    }
    if payload.get("provider"):
        chat["provider"] = payload["provider"]

    image_config = {
        key: payload[key]
        for key in ("aspect_ratio", "resolution", "quality", "background", "output_format", "n")
        if payload.get(key) is not None
    }
    if image_config:
        chat["image_config"] = image_config
    if payload.get("seed") is not None:
        chat["seed"] = payload["seed"]
    return chat


def _is_openrouter_url(url: str, settings: Settings) -> bool:
    return url.startswith((settings.base_url, "https://openrouter.ai/api/"))


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return {"raw": response.text}


def _as_list(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict):
        for key in ("data", "models"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    return []


def _supports_image_output(model: dict[str, Any]) -> bool:
    architecture = model.get("architecture") or {}
    modalities = architecture.get("output_modalities") or model.get("output_modalities") or []
    return "image" in {str(m).lower() for m in modalities}
