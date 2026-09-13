from __future__ import annotations

import shutil
import subprocess

import pytest

from openrouter_image_mcp.imaging import image_dimensions
from openrouter_image_mcp.media import MediaFileError, contact_sheet, load_input, probe_video

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is not installed",
)


def test_local_video_is_inlined_as_data_url(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"abc")
    assert load_input(str(clip), "video") == "data:video/mp4;base64,YWJj"


def test_local_audio_types_are_recognised(tmp_path):
    for name, media_type in (("a.m4a", "audio/mp4"), ("b.wav", "audio/wav")):
        path = tmp_path / name
        path.write_bytes(b"x")
        assert load_input(str(path), "audio").startswith(f"data:{media_type};base64,")


def test_wrong_kind_is_rejected(tmp_path, png_bytes):
    image = tmp_path / "still.png"
    image.write_bytes(png_bytes)
    with pytest.raises(MediaFileError, match="does not look like audio"):
        load_input(str(image), "audio")


def test_urls_are_passed_through():
    assert load_input("https://example.com/a.mp3", "audio") == "https://example.com/a.mp3"


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(MediaFileError, match="not found"):
        load_input(str(tmp_path / "nope.mp4"), "video")


def test_probe_returns_none_for_garbage(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"definitely not a video")
    assert probe_video(path) is None or probe_video(path).width is None


@needs_ffmpeg
def test_probe_and_contact_sheet_on_real_clip(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(clip),
        ],
        check=True,
    )
    info = probe_video(clip)
    assert info is not None
    assert (info.width, info.height, info.has_audio) == (320, 240, True)
    assert info.duration == pytest.approx(2, abs=0.3)

    sheet = contact_sheet(clip, 256, duration=info.duration)
    assert sheet is not None and sheet[1] == "image/png"
    assert max(image_dimensions(sheet[0])) <= 256
