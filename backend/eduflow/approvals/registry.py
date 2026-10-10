"""The central approvals queue: one place to see and decide every pending request (screen documentation:
"Central queue for attendance, leave, refunds, admissions and marks approvals").

There is no second workflow engine. Each module keeps its own request model, state machine, validation,
permission and audit, and registers a :class:`Provider` that:

* lists the requests the caller may decide (``pending``), and
* applies a decision through the module's own service (``decide``).

The queue only aggregates and delegates, so a decision taken here is exactly the decision the module's own
endpoint would take. Decisions use the standard verbs ``approve`` and ``decline`` (``reject`` is accepted as
an alias, CURRENT_STATE §12).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor


@dataclass(frozen=True)
class ApprovalItem:
    kind: str
    id: Any
    title: str
    requested_by: str
    requested_at: datetime.datetime
    detail: str = ""
    subject: str = ""
    amount: Decimal | None = None


@dataclass(frozen=True)
class Provider:
    kind: str
    label: str
    permission: str  # held school-wide to decide
    pending: Callable[[Actor], Iterable[ApprovalItem]]
    decide: Callable[[Actor, Any, str, str], Any]  # (actor, id, "approve" | "decline", note)


@dataclass
class _Registry:
    providers: dict[str, Provider] = field(default_factory=dict)

    def register(self, provider: Provider) -> None:
        self.providers[provider.kind] = provider

    def for_actor(self, actor: Actor) -> list[Provider]:
        return [p for p in self.providers.values() if DataScope.SCHOOL in actor.scopes(p.permission)]


registry = _Registry()
DECISIONS = {"approve": "approve", "decline": "decline", "reject": "decline"}


def pending_for(actor: Actor) -> list[ApprovalItem]:
    items: list[ApprovalItem] = []
    for provider in registry.for_actor(actor):
        items.extend(provider.pending(actor))
    return sorted(items, key=lambda item: item.requested_at)


@dataclass(frozen=True)
class _SchoolOnly:
    """Providers list pending requests from ``actor.school`` only; monitoring needs all of them."""

    school: Any


def all_pending(school: Any) -> list[ApprovalItem]:
    """Every pending request of the school, for monitoring ("Approvals waiting 24h+") and the pulse."""
    view = cast(Actor, _SchoolOnly(school))
    items = [item for provider in registry.providers.values() for item in provider.pending(view)]
    return sorted(items, key=lambda item: item.requested_at)
