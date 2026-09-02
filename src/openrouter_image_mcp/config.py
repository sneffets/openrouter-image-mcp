"""Configuration for the OpenRouter image MCP server (environment driven)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OUTPUT_DIR = "openrouter-images"
DEFAULT_TIMEOUT = 180.0
DEFAULT_PREVIEW_MAX_PX = 768
DEFAULT_APP_TITLE = "openrouter-image-mcp"
DEFAULT_APP_URL = "https://github.com/sneffets/openrouter-image-mcp"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """Raised when the server is not configured well enough to talk to OpenRouter."""


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean value, got {raw!r}")


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _csv(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = env.get(name) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Runtime settings, normally built from environment variables."""

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    default_model: str | None = None
    default_providers: tuple[str, ...] = ()
    allow_fallbacks: bool = True
    ask_for_provider: bool = False
    output_dir: Path = field(default_factory=lambda: Path(DEFAULT_OUTPUT_DIR))
    timeout: float = DEFAULT_TIMEOUT
    inline_preview: bool = True
    preview_max_px: int = DEFAULT_PREVIEW_MAX_PX
    app_title: str = DEFAULT_APP_TITLE
    app_url: str = DEFAULT_APP_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        base_url = (env.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        output_dir = Path(env.get("OPENROUTER_IMAGE_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR).expanduser()
        api_key = (env.get("OPENROUTER_API_KEY") or "").strip() or None
        return cls(
            api_key=api_key,
            base_url=base_url,
            default_model=(env.get("OPENROUTER_IMAGE_MODEL") or "").strip() or None,
            default_providers=_csv(env, "OPENROUTER_IMAGE_PROVIDER"),
            allow_fallbacks=_bool(env, "OPENROUTER_IMAGE_ALLOW_FALLBACKS", True),
            ask_for_provider=_bool(env, "OPENROUTER_IMAGE_ASK_FOR_PROVIDER", False),
            output_dir=output_dir,
            timeout=_float(env, "OPENROUTER_IMAGE_TIMEOUT", DEFAULT_TIMEOUT),
            inline_preview=_bool(env, "OPENROUTER_IMAGE_INLINE_PREVIEW", True),
            preview_max_px=_int(env, "OPENROUTER_IMAGE_PREVIEW_MAX_PX", DEFAULT_PREVIEW_MAX_PX),
            app_title=(env.get("OPENROUTER_APP_TITLE") or DEFAULT_APP_TITLE).strip(),
            app_url=(env.get("OPENROUTER_APP_URL") or DEFAULT_APP_URL).strip(),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "OPENROUTER_API_KEY is not set. Add it to the MCP server environment "
                "(see README) and restart the server."
            )
        return self.api_key

    def headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.require_api_key()}",
            "Content-Type": "application/json",
        }
        # OpenRouter uses these for attribution on its leaderboards; both are optional.
        if self.app_url:
            headers["HTTP-Referer"] = self.app_url
        if self.app_title:
            headers["X-Title"] = self.app_title
        return headers
