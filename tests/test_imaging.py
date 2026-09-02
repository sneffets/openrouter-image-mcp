from __future__ import annotations

from pathlib import Path

import pytest

from openrouter_image_mcp import imaging
from openrouter_image_mcp.imaging import ImageFileError


def test_extension_and_slug():
    assert imaging.extension_for("image/webp") == ".webp"
    assert imaging.extension_for("image/svg+xml") == ".svg"
    assert (
        imaging.slugify("Hero image for the *landing* page!") == "hero-image-for-the-landing-page"
    )
    assert imaging.slugify("   ") == "image"


def test_save_image_never_overwrites(tmp_path: Path, png_bytes: bytes):
    first = imaging.save_image(png_bytes, "image/png", tmp_path, prefix="hero")
    second = imaging.save_image(png_bytes, "image/png", tmp_path, prefix="hero")
    assert first != second
    assert first.exists() and second.exists()
    assert first.suffix == ".png"


def test_write_sidecar(tmp_path: Path, png_bytes: bytes):
    image = imaging.save_image(png_bytes, "image/png", tmp_path, prefix="hero")
    sidecar = imaging.write_sidecar(image, {"prompt": "a cat"})
    assert sidecar.name.endswith(".png.json")
    assert "a cat" in sidecar.read_text(encoding="utf-8")


def test_preview_downscales(png_bytes: bytes):
    preview = imaging.make_preview(png_bytes, "image/png", 16)
    assert preview is not None
    data, media_type = preview
    assert media_type == "image/png"
    assert max(imaging.image_dimensions(data)) <= 16


def test_preview_skipped_for_vector_formats(png_bytes: bytes):
    assert imaging.make_preview(b"<svg/>", "image/svg+xml", 128) is None


def test_load_reference_passes_urls_through():
    assert imaging.load_reference("https://x.test/a.png") == "https://x.test/a.png"
    assert imaging.load_reference("data:image/png;base64,AA").startswith("data:")


def test_load_reference_encodes_local_file(tmp_path: Path, png_bytes: bytes):
    path = tmp_path / "ref.png"
    path.write_bytes(png_bytes)
    assert imaging.load_reference(str(path)).startswith("data:image/png;base64,")


def test_load_reference_rejects_missing_and_non_images(tmp_path: Path):
    with pytest.raises(ImageFileError, match="not found"):
        imaging.load_reference(str(tmp_path / "nope.png"))
    text = tmp_path / "notes.txt"
    text.write_text("hi", encoding="utf-8")
    with pytest.raises(ImageFileError, match="does not look like an image"):
        imaging.load_reference(str(text))
