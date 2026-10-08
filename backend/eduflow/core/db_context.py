"""PostgreSQL session context for Row-Level Security (ADR-018, docs/security/rls.md).

Application code is the primary tenant boundary. RLS is the second layer: if a query forgets its school
filter, PostgreSQL still returns only the current tenant's rows.

How PostgreSQL learns the context
---------------------------------
At the start of a request or task the connection switches to the ``eduflow_app`` role (``NOLOGIN``,
``NOBYPASSRLS``; created by migration ``core.0001``) and three settings are written with ``set_config``:

* ``eduflow.school_id``: the tenant resolved from a validated membership (never the raw header)
* ``eduflow.user_id``: the authenticated user, which allows reading that user's own memberships
* ``eduflow.rls_bypass``: ``on`` only inside :func:`system_context`, for platform-level code paths

The policies read these through SQL helper functions. When nothing is set, tenant tables return no rows
and reject writes, so a forgotten context fails closed.

The values are written only by this module, from server-side state. No client input reaches them
directly: ``X-School-Id`` is first checked against an active membership (``eduflow.tenancy.context``).

Lifetime
--------
The context is session-level, because requests run in autocommit mode (one transaction per query). It is
therefore reset explicitly at the end of every request and task, and re-applied if Django opens a new
connection mid-request (``connection_created``), so a context can neither leak into the next request on a
persistent connection nor be lost on a reconnect. A connection that is not engaged (migrations,
management commands, test fixtures) keeps the owner role, which is not subject to RLS.

Do not open or leave a context inside ``transaction.atomic()``: a rolled-back transaction also rolls back
``set_config``. Enter the context first and open the transaction inside it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.dispatch import receiver

from .logging import get_logger

log = get_logger(__name__)

GUC_SCHOOL = "eduflow.school_id"
GUC_USER = "eduflow.user_id"
GUC_BYPASS = "eduflow.rls_bypass"

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class DbContext:
    school_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    bypass: bool = False


# None means "not engaged": the connection runs as its login role and RLS does not apply to it.
_current: ContextVar[DbContext | None] = ContextVar("db_context", default=None)


class DbContextError(RuntimeError):
    pass


def app_role() -> str:
    role = str(getattr(settings, "DATABASE_RLS_ROLE", "") or "")
    if role and not _ROLE_NAME.fullmatch(role):
        raise DbContextError("DATABASE_RLS_ROLE is not a valid role name.")
    return role


def current() -> DbContext | None:
    return _current.get()


def _write(conn: BaseDatabaseWrapper, ctx: DbContext | None) -> None:
    role = app_role()
    with conn.cursor() as cursor:
        if ctx is None:
            cursor.execute(
                "SELECT set_config(%s, '', false), set_config(%s, '', false), set_config(%s, '', false)",
                [GUC_SCHOOL, GUC_USER, GUC_BYPASS],
            )
            if role:
                cursor.execute("RESET ROLE")
            return
        if role:
            cursor.execute("SELECT set_config('role', %s, false)", [role])
        cursor.execute(
            "SELECT set_config(%s, %s, false), set_config(%s, %s, false), set_config(%s, %s, false)",
            [
                GUC_SCHOOL,
                str(ctx.school_id) if ctx.school_id else "",
                GUC_USER,
                str(ctx.user_id) if ctx.user_id else "",
                GUC_BYPASS,
                "on" if ctx.bypass else "",
            ],
        )


def _apply(ctx: DbContext | None) -> None:
    try:
        _write(connection, ctx)
    except DatabaseError:
        # The connection is in an unknown state. Closing it guarantees the old context cannot survive on it;
        # the next connection gets ``ctx`` from _on_connection_created (or nothing, for a release).
        log.warning("db_context_apply_failed")
        _current.set(ctx)
        connection.close()
        raise
    # Set only after a successful write. A newly opened connection meanwhile received the previous value,
    # which the write above then replaced.
    _current.set(ctx)


def engage(ctx: DbContext | None = None) -> None:
    """Start an RLS-enforced unit of work (request or task) with ``ctx`` (empty by default)."""
    _apply(ctx or DbContext())


def release() -> None:
    """End the unit of work: clear every setting and return to the login role."""
    if _current.get() is None:
        return
    # On failure the error is already logged and the connection closed, so nothing can leak.
    with suppress(DatabaseError):
        _apply(None)


def update(**changes: Any) -> DbContext:
    """Change part of the engaged context, e.g. once the user or the tenant is known."""
    ctx = _current.get()
    if ctx is None:
        raise DbContextError("No database context is engaged.")
    new = replace(ctx, **changes)
    _apply(new)
    return new


@contextmanager
def scoped(ctx: DbContext) -> Iterator[DbContext]:
    """Run a block under ``ctx`` and then restore whatever was active before (including "not engaged")."""
    previous = _current.get()
    _apply(ctx)
    try:
        yield ctx
    finally:
        if previous is None:
            release()
        else:
            _apply(previous)


@contextmanager
def system_context(reason: str) -> Iterator[DbContext]:
    """Lift tenant restrictions for a platform-level operation. Every use names its reason, and is logged.

    Use it only where crossing tenants is the point (platform administration, maintenance jobs). Ordinary
    tenant code never needs it.
    """
    ctx = _current.get() or DbContext()
    log.info("rls_bypass", reason=reason)
    with scoped(replace(ctx, bypass=True)) as active:
        yield active


@receiver(connection_created)
def _on_connection_created(sender: Any, connection: BaseDatabaseWrapper, **_: Any) -> None:
    ctx = _current.get()
    if ctx is not None and connection.vendor == "postgresql":
        _write(connection, ctx)
