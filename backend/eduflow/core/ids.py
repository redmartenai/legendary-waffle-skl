"""Identifier generation (docs/database/conventions.md).

Primary keys are UUIDv7 (RFC 9562): a 48-bit millisecond timestamp followed by random bits. They sort by
creation time, so B-tree indexes stay compact, and they cannot be enumerated. Python 3.13 has no
``uuid.uuid7``, so it is built here from the standard library. 74 bits of each ID come from ``os.urandom``.
"""

from __future__ import annotations

import os
import time
import uuid

_RAND_A_BITS = 12
_RAND_B_BITS = 62


def uuid7() -> uuid.UUID:
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> _RAND_B_BITS) & ((1 << _RAND_A_BITS) - 1)
    rand_b = rand & ((1 << _RAND_B_BITS) - 1)
    value = (
        (unix_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76  # version
        | rand_a << 64
        | 0b10 << 62  # RFC 4122 variant
        | rand_b
    )
    return uuid.UUID(int=value)
