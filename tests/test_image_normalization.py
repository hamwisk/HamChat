from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from hamchat.media_helper import (
    ImageValidationError, MAX_INFERENCE_PIXELS, normalize_image_bytes, process_images,
)
from hamchat.infra.llm.base import ChatMessage
from hamchat.infra.llm.openai_client import OpenAIClient


def encoded(fmt: str, image: Image.Image, **kwargs) -> bytes:
    out = io.BytesIO(); image.save(out, format=fmt, **kwargs); return out.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_common_rasters_normalize_by_content_not_filename(fmt):
    raw = encoded(fmt, Image.new("RGB", (32, 16), "red"))
    result = normalize_image_bytes(raw)
    assert result.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert (result.width, result.height, result.source_mime) == (32, 16, f"image/{'jpeg' if fmt == 'JPEG' else fmt.lower()}")


def test_corrupt_and_decompression_bomb_inputs_are_rejected(monkeypatch):
    with pytest.raises(ImageValidationError):
        normalize_image_bytes(b"not an image")
    monkeypatch.setattr("hamchat.media_helper.MAX_DECODED_PIXELS", 10)
    with pytest.raises(ImageValidationError):
        normalize_image_bytes(encoded("PNG", Image.new("RGB", (4, 4))))


def test_orientation_animation_transparency_and_metadata_are_normalized():
    oriented = Image.new("RGB", (12, 30), "blue")
    exif = Image.Exif(); exif[274] = 6
    result = normalize_image_bytes(encoded("JPEG", oriented, exif=exif))
    assert (result.width, result.height) == (30, 12)

    first, second = Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "green")
    animated = encoded("GIF", first, save_all=True, append_images=[second], loop=0)
    frame = Image.open(io.BytesIO(normalize_image_bytes(animated).png_bytes))
    assert frame.convert("RGB").getpixel((0, 0))[0] > 200

    transparent = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    output = Image.open(io.BytesIO(normalize_image_bytes(encoded("PNG", transparent)).png_bytes))
    assert output.mode == "RGB" and output.getpixel((0, 0)) == (245, 245, 245)
    assert not output.getexif()


def test_resize_preserves_aspect_and_never_upscales():
    small = normalize_image_bytes(encoded("PNG", Image.new("RGB", (300, 200))))
    assert (small.width, small.height) == (300, 200)
    large = normalize_image_bytes(encoded("PNG", Image.new("RGB", (4000, 2000))))
    assert large.width * large.height <= MAX_INFERENCE_PIXELS
    assert abs((large.width / large.height) - 2.0) < 0.01


def test_process_images_keeps_original_bytes_and_sends_png(tmp_path, monkeypatch):
    source = tmp_path / "MISLEADING.UPPER"
    original = encoded("WEBP", Image.new("RGB", (40, 20), "purple"))
    source.write_bytes(original)
    captured = []
    monkeypatch.setattr("hamchat.db_ops.cas_put", lambda _db, **kwargs: (captured.append(open(kwargs["src_path"], "rb").read()) or 1))

    batch = process_images([str(source)], ephemeral=False, db=object())

    assert captured[0] == original
    part = batch["llm_parts"][0]
    assert part["media_type"] == "image/png"
    assert base64.b64decode(part["data_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert (part["width"], part["height"]) == (40, 20)


def test_openai_vision_uses_normalized_png_data_url():
    captured = {}
    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return []
    client = OpenAIClient.__new__(OpenAIClient)
    client.client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    image = base64.b64encode(normalize_image_bytes(encoded("JPEG", Image.new("RGB", (4, 4)))).png_bytes).decode()

    # Attachments use the multipart branch and declare PNG regardless of original format.
    message = ChatMessage("user", "look")
    setattr(message, "parts", [{"type": "image", "data_base64": image}])
    list(client.stream_chat(model="vision", messages=[message], options={}))
    assert captured["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
