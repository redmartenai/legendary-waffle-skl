"""White-label test fixtures: real (tiny) image files built in code, and API helpers.

Test settings use ``WHITE_LABEL_BASE_DOMAIN = "eduflow.test"``, ``WHITE_LABEL_PLATFORM_HOSTS =
["app.eduflow.test"]``, an in-memory ``branding`` storage and the ``MemoryVerifier`` for DNS.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile

from eduflow.branding.verification import MemoryVerifier


def png(width: int = 64, height: int = 64) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    rows = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def jpeg(width: int = 80, height: int = 40) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp(width: int = 100, height: int = 50) -> bytes:
    payload = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little")
    payload += (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WEBP" + payload


def ico(side: int = 32) -> bytes:
    image = png(side, side)
    entry = struct.pack("<BBBBHHII", side % 256, side % 256, 0, 0, 1, 32, len(image), 22)
    return b"\x00\x00\x01\x00\x01\x00" + entry + image


def upload(data: bytes, name: str = "image.png", content_type: str = "image/png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type=content_type)


@pytest.fixture(autouse=True)
def _clean_white_label_state() -> Any:
    MemoryVerifier.records.clear()
    cache.clear()
    yield
    MemoryVerifier.records.clear()


@pytest.fixture
def dns() -> Any:
    """``dns(hostname, token)`` publishes the verification TXT record in the memory verifier."""
    from eduflow.branding.verification import challenge_name, challenge_value

    def _publish(hostname: str, token: str) -> None:
        MemoryVerifier.records.setdefault(challenge_name(hostname), []).append(challenge_value(token))

    return _publish


@pytest.fixture
def platform_admin(make_user: Any, client_for: Any) -> Any:
    return client_for(make_user(is_platform_admin=True))
