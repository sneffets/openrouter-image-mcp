"""Video and audio files: input references, probing and frame previews (via ffmpeg)."""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import imaging
from .imaging import ImageFileError

VIDEO_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}
# mimetypes differs between platforms for these, so pin the common ones.
_KNOWN_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}
MAX_INPUT_BYTES = {
    "image": imaging.MAX_REFERENCE_BYTES,
    "video": 50 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
}
PROBE_TIMEOUT = 30
FFMPEG_TIMEOUT = 60


class MediaFileError(ImageFileError):
    """Raised for unusable local video or audio inputs."""


def guess_media_type(path: Path) -> str | None:
    return _KNOWN_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]


def video_extension(media_type: str) -> str:
    media_type = (media_type or "").split(";")[0].strip().lower()
    return VIDEO_EXTENSIONS.get(media_type) or mimetypes.guess_extension(media_type) or ".mp4"


def load_input(reference: str, kind: str) -> str:
    """Turn an image/video/audio input (path, http URL or data URL) into an API URL.

    Local files are inlined as data URLs. For big clips a public http URL is the
    more robust choice, because the whole request body has a size limit.
    """
    if kind == "image":
        return imaging.load_reference(reference)
    value = (reference or "").strip()
    if not value:
        raise MediaFileError(f"Empty {kind} reference.")
    if value.startswith(("http://", "https://", "data:")):
        return value

    path = Path(value).expanduser()
    if not path.is_file():
        raise MediaFileError(f"Reference {kind} not found: {path}")
    limit = MAX_INPUT_BYTES[kind]
    size = path.stat().st_size
    if size > limit:
        raise MediaFileError(
            f"Reference {kind} {path.name} is {size / 1_048_576:.1f} MB; local files are "
            f"limited to {limit // 1_048_576} MB - upload it somewhere and pass the URL instead."
        )
    media_type = guess_media_type(path) or ""
    if not media_type.startswith(f"{kind}/"):
        raise MediaFileError(f"{path.name} does not look like {kind} ({media_type or '?'}).")
    return imaging.to_data_url(path.read_bytes(), media_type)


@dataclass
class VideoInfo:
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False

    def describe(self) -> str:
        parts = []
        if self.duration:
            parts.append(f"{self.duration:.1f}s")
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        parts.append("with audio" if self.has_audio else "no audio")
        return ", ".join(parts)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_video(path: Path) -> VideoInfo | None:
    """Duration, dimensions and audio presence via ffprobe; ``None`` without ffprobe."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=PROBE_TIMEOUT,
            check=True,
        )
        data = json.loads(completed.stdout or b"{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None

    info = VideoInfo()
    try:
        info.duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        info.duration = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and info.width is None:
            info.width, info.height = stream.get("width"), stream.get("height")
        elif stream.get("codec_type") == "audio":
            info.has_audio = True
    return info


def contact_sheet(
    path: Path, max_pixels: int, *, duration: float | None = None
) -> tuple[bytes, str] | None:
    """A 2x2 grid of evenly spaced frames, so a clip can be judged from one image.

    Returns ``None`` when ffmpeg is missing or the file cannot be decoded.
    """
    if shutil.which("ffmpeg") is None:
        return None
    tile_width = max(64, max_pixels // 2)
    if duration and duration > 0:
        video_filter = f"fps={4 / duration:.6f},scale={tile_width}:-2,tile=2x2"
    else:
        video_filter = f"scale={max_pixels}:-2"
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                video_filter,
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "png",
                "-",
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if not completed.stdout:
        return None
    return imaging.make_preview(completed.stdout, "image/png", max_pixels)
