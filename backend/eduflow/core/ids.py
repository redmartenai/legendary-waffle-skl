"""Identifier generation (docs/database/conventions.md).

Primary keys are UUIDv7 (RFC 9562): a 48-bit millisecond timestamp followed by random bits. They sort by
creation time, so B-tree indexes stay compact, and they cannot be enumerated. Python 3.13 has no
``uuid.uuid7``, so it is built here from the standard library. 62 bits of each ID come from ``os.urandom``,
plus a randomly seeded 12-bit counter that keeps IDs of the same millisecond in creation order.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_RAND_A_BITS = 12
_RAND_B_BITS = 62
_COUNTER_MAX = (1 << _RAND_A_BITS) - 1

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def _next_timestamp_and_counter() -> tuple[int, int]:
    """Monotonic within the process (RFC 9562 section 6.2, method 1: a 12-bit counter in ``rand_a``).

    IDs created in the same millisecond keep their creation order, so ordering by ID (cursor pagination,
    "newest first") is exact. The counter starts at a random value below half its range each millisecond,
    and a full counter borrows the next millisecond.
    """
    global _last_ms, _counter
    now_ms = time.time_ns() // 1_000_000
    with _lock:
        if now_ms > _last_ms:
            _last_ms = now_ms
            _counter = int.from_bytes(os.urandom(2), "big") & (_COUNTER_MAX >> 1)
        elif _counter < _COUNTER_MAX:
            _counter += 1
        else:  # 4096 IDs in one millisecond, or the clock went backwards: move forward instead
            _last_ms += 1
            _counter = 0
        return _last_ms, _counter


def uuid7() -> uuid.UUID:
    unix_ms, rand_a = _next_timestamp_and_counter()
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << _RAND_B_BITS) - 1)
    value = (
        (unix_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76  # version
        | rand_a << 64
        | 0b10 << 62  # RFC 4122 variant
        | rand_b
    )
    return uuid.UUID(int=value)
