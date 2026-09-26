"""Console seed: operations (transport, fees, approvals).

Each part exposes ``clear(cmd, school)`` (remove what an earlier run of the part made, so ``--only operations`` is
idempotent) and ``seed(cmd, school)``. Parts are cleared in reverse order (approvals can point at fee payments, and
fees bill transport riders), then seeded in order. ``OPS_PARTS=fees,approvals`` limits a run to some parts while
iterating. Phones come from ``cmd._phone(5000..5999)``: fees 5000–5499, transport 5500–5799, approvals 5800–5999.
"""

import os

from .ops import approvals, fees, transport

PARTS = {"transport": transport, "fees": fees, "approvals": approvals}


def seed(cmd, school):
    wanted = [p.strip() for p in os.environ.get("OPS_PARTS", "").split(",") if p.strip()] or list(PARTS)
    parts = [PARTS[name] for name in PARTS if name in wanted]
    for part in reversed(parts):
        part.clear(cmd, school)
    for part in parts:
        part.seed(cmd, school)
