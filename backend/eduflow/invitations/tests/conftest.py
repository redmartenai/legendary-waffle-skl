"""Invitation test helpers: send through the API, read secrets and codes from the in-memory outboxes."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from django.core import mail

from eduflow.authz.models import Role
from eduflow.identity.otp.providers import MemorySmsProvider

TOKEN = re.compile(r"#token=([A-Za-z0-9_-]+)")
CODE = re.compile(r"\b(\d{6})\b")


def _last_message(channel: str) -> str:
    if channel == "phone":
        return MemorySmsProvider.outbox[-1].message
    return str(mail.outbox[-1].body)


def last_token(channel: str = "phone") -> str:
    match = TOKEN.search(_last_message(channel))
    assert match is not None, "no invitation link was delivered"
    return match.group(1)


def last_code(channel: str = "phone") -> str:
    match = CODE.search(_last_message(channel))
    assert match is not None, "no code was delivered"
    return match.group(1)


@dataclass
class Sent:
    id: str
    token: str
    channel: str
    body: dict[str, Any]


@pytest.fixture
def invite(world: Any, as_member: Any) -> Callable[..., Sent]:
    """``invite(kind=..., recipient=..., by=membership, **fields)``: the created invitation and its secret."""

    def _invite(
        kind: str = "staff",
        *,
        recipient: str = "+919811100001",
        channel: str = "phone",
        by: Any = None,
        expect: int = 201,
        **fields: Any,
    ) -> Sent:
        body: dict[str, Any] = {
            "kind": kind,
            "channel": channel,
            "recipient": recipient,
            "full_name": fields.pop("full_name", "Invited Person"),
        }
        if kind == "staff":
            role_ids = fields.pop("role_ids", None)
            if role_ids is None:
                role_ids = [str(Role.objects.get(school=world.school, key="teacher").pk)]
            body.update({"role_ids": role_ids, "employee_id": fields.pop("employee_id", "T-NEW")})
        elif kind == "student":
            body["student_id"] = str(fields.pop("student_id", world.other_student.pk))
        elif kind == "guardian":
            body["guardian_id"] = str(fields.pop("guardian_id"))
        body.update(fields)
        response = as_member(by or world.admin).post("/api/v1/invitations", body, format="json")
        assert response.status_code == expect, response.content
        if expect != 201:
            return Sent(id="", token="", channel=channel, body=response.json())
        return Sent(
            id=response.json()["id"], token=last_token(channel), channel=channel, body=response.json()
        )

    return _invite


@pytest.fixture
def verify(api_client: Any) -> Callable[[Sent], str]:
    """Request a code for an invitation; returns the challenge ID (the code is in the outbox)."""

    def _verify(sent: Sent) -> str:
        response = api_client.post("/api/v1/invitations/verification", {"token": sent.token}, format="json")
        assert response.status_code == 200, response.content
        return str(response.json()["challenge_id"])

    return _verify


@pytest.fixture
def accept(api_client: Any, verify: Callable[[Sent], str]) -> Callable[..., Any]:
    """``accept(sent, client=None)``: verify, then accept anonymously or with ``client``'s bearer token."""

    def _accept(
        sent: Sent, client: Any = None, *, code: str | None = None, challenge_id: str | None = None
    ) -> Any:
        challenge = challenge_id or verify(sent)
        body = {"token": sent.token, "challenge_id": challenge, "code": code or last_code(sent.channel)}
        return (client or api_client).post("/api/v1/invitations/accept", body, format="json")

    return _accept
