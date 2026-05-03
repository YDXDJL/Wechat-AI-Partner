"""Image helpers for multimodal model input."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass


MAX_IMAGE_BYTES = 50 * 1024 * 1024
SUPPORTED_IMAGE_MIME = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
}
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


@dataclass
class ImageAttachment:
    path: str
    mime_type: str
    size: int
    source: str = "local"
    message_id: str | None = None


def detect_image_mime(data: bytes, path: str | None = None) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if path:
        ext = os.path.splitext(path)[1].lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(ext)
    return None


def extension_for_mime(mime_type: str) -> str:
    return _EXT_BY_MIME.get(mime_type, ".img")


def load_image_attachment(path: str, source: str = "local", message_id: str | None = None) -> ImageAttachment:
    full_path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"Image file not found: {full_path}")

    size = os.path.getsize(full_path)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image is too large ({size} bytes, max {MAX_IMAGE_BYTES})")

    with open(full_path, "rb") as f:
        head = f.read(32)
    mime_type = detect_image_mime(head, full_path)
    if mime_type not in SUPPORTED_IMAGE_MIME:
        raise ValueError(f"Unsupported image format: {full_path}")

    return ImageAttachment(
        path=full_path,
        mime_type=mime_type,
        size=size,
        source=source,
        message_id=message_id,
    )


def image_to_claude_block(image: ImageAttachment) -> dict:
    with open(image.path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.mime_type,
            "data": data,
        },
    }


def image_summary(image: ImageAttachment) -> str:
    parts = [
        "【图片】用户发送了一张图片",
        f"来源: {image.source}",
        f"路径: {image.path}",
        f"格式: {image.mime_type}",
        f"大小: {image.size} bytes",
    ]
    if image.message_id:
        parts.append(f"消息ID: {image.message_id}")
    return "\n".join(parts)


def build_multimodal_content(text: str, images: list[ImageAttachment]) -> str | list[dict]:
    if not images:
        return text

    blocks: list[dict] = []
    text = text.strip()
    if text:
        blocks.append({"type": "text", "text": text})
    for image in images:
        blocks.append({"type": "text", "text": image_summary(image)})
        blocks.append(image_to_claude_block(image))
    return blocks
