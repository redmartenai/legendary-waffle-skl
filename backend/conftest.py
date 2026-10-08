from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from typing import Any

import pytest
from django.conf import settings
from django.core.cache import cache
from rest_framework.test import APIClient

PASSWORD = "Correct-Horse-Battery-9"  # noqa: S105  (test-only)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture(autouse=True)
def _isolated_state() -> Iterator[None]:
    """Rate-limit counters, sent SMS and the database context never carry over between tests."""
    from eduflow.core import db_context
    from eduflow.identity.otp.providers import MemorySmsProvider

    MemorySmsProvider.outbox.clear()
    # If Redis is unavailable, tests that need it fail on their own.
    with suppress(Exception):
        cache.clear()
    yield
    if db_context.current() is not None:
        try:
            db_context.release()
        except Exception:
            db_context._current.set(None)


@pytest.fixture
def make_user(db: Any) -> Callable[..., Any]:
    from eduflow.identity.models import User

    counter = iter(range(1, 10_000))

    def _make(
        *, email: str | None = None, phone: str | None = None, password: str | None = PASSWORD, **extra: Any
    ):
        n = next(counter)
        if email is None and phone is None:
            email = f"user{n}@example.test"
        extra.setdefault("full_name", f"User {n}")
        return User.objects.create_user(email=email, phone=phone, password=password, **extra)

    return _make


@pytest.fixture
def make_school(db: Any) -> Callable[..., Any]:
    from eduflow.authz.services import seed_system_roles
    from eduflow.tenancy.models import School

    counter = iter(range(1, 10_000))

    def _make(code: str | None = None, **extra: Any):
        school = School.objects.create(
            code=code or f"school-{next(counter)}", name=extra.pop("name", "School"), **extra
        )
        seed_system_roles(school)
        school.refresh_from_db()
        return school

    return _make


@pytest.fixture
def make_member(make_user: Callable[..., Any]) -> Callable[..., Any]:
    from eduflow.authz.models import MembershipRole, Role
    from eduflow.authz.services import bump_rbac_version
    from eduflow.tenancy.models import Membership

    def _make(school: Any, user: Any = None, roles: Iterable[str] = ("staff",), **extra: Any):
        user = user or make_user()
        membership = Membership.objects.create(school=school, user=user, **extra)
        for key in roles:
            role = Role.objects.get(school=school, key=key)
            MembershipRole.objects.create(school=school, membership=membership, role=role)
        bump_rbac_version(school.pk)
        school.refresh_from_db()
        return membership

    return _make


@pytest.fixture
def client_for(db: Any) -> Callable[..., APIClient]:
    """An API client signed in as ``user`` (real tokens), optionally acting in ``school``."""
    from eduflow.identity.models import AuthMethod
    from eduflow.identity.tokens import start_session

    def _make(user: Any, school: Any = None) -> APIClient:
        client = APIClient()
        issued = start_session(user, method=AuthMethod.PASSWORD)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issued.access}")
        if school is not None:
            client.defaults["HTTP_X_SCHOOL_ID"] = str(school.pk)
        client.issued = issued  # type: ignore[attr-defined]
        return client

    return _make


class LogCapture:
    """Captures the real rendered log output (after redaction and JSON rendering)."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def text(self) -> str:
        return self._stream.getvalue()

    @property
    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]

    def find(self, event: str) -> list[dict[str, object]]:
        return [r for r in self.records if r.get("event") == event]


@pytest.fixture
def json_logs() -> Iterator[LogCapture]:
    """Attach a handler using the project's configured formatter to the root logger."""
    from logging.config import dictConfig

    config = settings.LOGGING
    dictConfig(config)  # ensure the formatter object exists exactly as configured
    root = logging.getLogger()
    formatter = root.handlers[0].formatter
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    try:
        yield LogCapture(stream)
    finally:
        root.removeHandler(handler)
