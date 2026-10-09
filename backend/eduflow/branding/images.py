"""Validation of uploaded brand images, by content, without third-party libraries or decoding pixels.

The file's *bytes* decide its type (signature and header); the client's filename and Content-Type are
ignored. Accepted: PNG, JPEG and WebP logos; PNG and ICO favicons. **SVG is refused**: it is a document
that can carry scripts. GIF is refused (animation is not branding).

This proves the header is well formed and the dimensions are sane. It does not re-encode the image, so the
serving side adds its own protections (exact Content-Type, ``nosniff``, a sandboxing CSP, see api/views.py).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from rest_framework.exceptions import ValidationError

from .models import AssetKind


@dataclass(frozen=True)
class ImageInfo:
    content_type: str
    extension: str
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    min_side: int
    max_side: int
    types: frozenset[str]
    square: bool = False


LIMITS = {
    AssetKind.LOGO: Limits(1024 * 1024, 16, 4096, frozenset({"image/png", "image/jpeg", "image/webp"})),
    AssetKind.FAVICON: Limits(256 * 1024, 16, 512, frozenset({"image/png", "image/x-icon"}), square=True),
}


class InvalidImage(ValidationError):
    pass


def _bad(message: str) -> InvalidImage:
    return InvalidImage({"file": [message]})


def _png(data: bytes) -> tuple[int, int]:
    # Signature, then the IHDR chunk first: length 13, type "IHDR", width, height (big-endian).
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise _bad("This PNG file is damaged.")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg(data: bytes) -> tuple[int, int]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            raise _bad("This JPEG file is damaged.")
        marker = data[index + 1]
        if marker == 0xFF:  # fill byte
            index += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
            index += 2
            continue
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        if length < 2:
            raise _bad("This JPEG file is damaged.")
        # Start-of-frame markers (baseline, progressive, ...) carry the dimensions.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + length
    raise _bad("This JPEG file has no image frame.")


def _webp(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        raise _bad("This WebP file is damaged.")
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise _bad("This WebP file is damaged.")


def _ico(data: bytes) -> tuple[int, int]:
    (count,) = struct.unpack("<H", data[4:6])
    if count < 1 or len(data) < 6 + 16 * count:
        raise _bad("This icon file is damaged.")
    width, height = data[6] or 256, data[7] or 256  # 0 means 256
    return width, height


def inspect(data: bytes, kind: str) -> ImageInfo:
    """Identify and check an upload for ``kind``. Raises a ``400``-style ``ValidationError``."""
    limits = LIMITS[AssetKind(kind)]
    if not data:
        raise _bad("The file is empty.")
    if len(data) > limits.max_bytes:
        raise _bad(f"The file is larger than {limits.max_bytes // 1024} KB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, extension, (width, height) = "image/png", "png", _png(data)
    elif data.startswith(b"\xff\xd8\xff"):
        content_type, extension, (width, height) = "image/jpeg", "jpg", _jpeg(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        content_type, extension, (width, height) = "image/webp", "webp", _webp(data)
    elif data[:4] == b"\x00\x00\x01\x00":
        content_type, extension, (width, height) = "image/x-icon", "ico", _ico(data)
    else:
        raise _bad("Upload a PNG, JPEG or WebP image (or PNG or ICO for a favicon). SVG is not accepted.")
    if content_type not in limits.types:
        raise _bad(f"A {kind} must be one of: {', '.join(sorted(limits.types))}.")
    if not (limits.min_side <= width <= limits.max_side and limits.min_side <= height <= limits.max_side):
        raise _bad(f"The image must be between {limits.min_side} and {limits.max_side} pixels on each side.")
    if limits.square and width != height:
        raise _bad("A favicon must be square.")
    return ImageInfo(content_type, extension, width, height, hashlib.sha256(data).hexdigest())
