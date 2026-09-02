"""Local file handling: saving generated images, previews and reference inputs."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}
RASTER_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ImageFileError(RuntimeError):
    """Raised for unusable local paths or unreadable reference images."""


def extension_for(media_type: str) -> str:
    media_type = (media_type or "").split(";")[0].strip().lower()
    return EXTENSIONS.get(media_type) or mimetypes.guess_extension(media_type) or ".png"


def slugify(text: str, *, max_length: int = 48) -> str:
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "image"


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """A path inside ``directory`` that does not exist yet."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def save_image(
    data: bytes,
    media_type: str,
    directory: Path,
    *,
    prefix: str,
    index: int = 0,
    timestamp: datetime | None = None,
) -> Path:
    """Write image bytes to a timestamped, collision-free file."""
    stamp = (timestamp or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}-{slugify(prefix)}"
    if index:
        stem = f"{stem}-{index + 1}"
    path = unique_path(directory, stem, extension_for(media_type))
    path.write_bytes(data)
    return path


def write_sidecar(image_path: Path, metadata: dict[str, Any]) -> Path:
    """Store prompt/model/provider next to the image so results are reproducible."""
    sidecar = image_path.with_suffix(image_path.suffix + ".json")
    sidecar.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return sidecar


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - pillow is a hard dependency
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


def make_preview(data: bytes, media_type: str, max_pixels: int) -> tuple[bytes, str] | None:
    """Downscale an image so it can be shown inline without flooding the context.

    Returns ``None`` for formats that cannot be resized (e.g. SVG).
    """
    media_type = (media_type or "").split(";")[0].strip().lower()
    if media_type not in RASTER_TYPES:
        return None
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - pillow is a hard dependency
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGBA" if img.mode in {"RGBA", "LA", "P"} else "RGB")
            if max(img.size) > max_pixels:
                img.thumbnail((max_pixels, max_pixels), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "image/png"
    except Exception:
        return None


def to_data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def load_reference(reference: str) -> str:
    """Turn a reference image (path, http URL or data URL) into something the API accepts."""
    value = (reference or "").strip()
    if not value:
        raise ImageFileError("Empty reference image.")
    if value.startswith(("http://", "https://", "data:")):
        return value

    path = Path(value).expanduser()
    if not path.is_file():
        raise ImageFileError(f"Reference image not found: {path}")
    size = path.stat().st_size
    if size > MAX_REFERENCE_BYTES:
        raise ImageFileError(
            f"Reference image {path.name} is {size / 1_048_576:.1f} MB; "
            f"the limit is {MAX_REFERENCE_BYTES // 1_048_576} MB."
        )
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    if not media_type.startswith("image/"):
        raise ImageFileError(f"{path.name} does not look like an image ({media_type}).")
    return to_data_url(path.read_bytes(), media_type)


def read_local_image(path_like: str) -> tuple[bytes, str]:
    path = Path(path_like).expanduser()
    if not path.is_file():
        raise ImageFileError(f"No such image: {path}")
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return path.read_bytes(), media_type


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"
