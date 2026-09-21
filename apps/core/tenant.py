"""
Per-request school (tenant) context.

Every school-owned model uses ``SchoolScopedManager``: querying it without an
active school raises ``TenantContextMissing`` instead of silently returning
another school's rows. Code that legitimately works across schools (platform
jobs, GPS ingestion by device id, Django admin) must opt in with ``unscoped()``.
"""

from contextlib import contextmanager
from contextvars import ContextVar, Token

_school_var: ContextVar = ContextVar("eduflow_school", default=None)
_bypass_var: ContextVar[bool] = ContextVar("eduflow_tenant_bypass", default=False)


class TenantContextMissing(RuntimeError):
    """Raised when school-owned data is touched without a school in context."""


def current_school(required: bool = True):
    school = _school_var.get()
    if school is None and required and not _bypass_var.get():
        raise TenantContextMissing(
            "No school in context. Wrap the code in use_school(school) or unscoped()."
        )
    return school


def bypass_active() -> bool:
    return _bypass_var.get()


def activate_school(school) -> Token:
    return _school_var.set(school)


def deactivate_school(token: Token) -> None:
    _school_var.reset(token)


@contextmanager
def use_school(school):
    token = _school_var.set(school)
    try:
        yield school
    finally:
        _school_var.reset(token)


@contextmanager
def unscoped():
    """Explicit, greppable bypass for platform-level code."""
    token = _bypass_var.set(True)
    try:
        yield
    finally:
        _bypass_var.reset(token)
