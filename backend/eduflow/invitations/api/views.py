"""Invitation endpoints.

* Management (``X-School-Id``, ``invitation.read`` / ``invitation.manage`` school-wide): list, create, detail,
  resend, revoke.
* Recipient (public, rate-limited per IP and per secret): preview, verification, accept. The secret travels in
  the request body, never in a URL, so it cannot end up in access logs.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView

from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.api.resources import (
    Filter,
    ResourceDetailView,
    ResourceListView,
    ResourceView,
    document_resource,
)
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors
from eduflow.identity.api.serializers import MyMembershipOut
from eduflow.identity.api.views import session_payload
from eduflow.identity.authentication import request_user
from eduflow.identity.delivery import Channel
from eduflow.identity.models import User
from eduflow.identity.throttles import (
    InvitationIpThrottle,
    InvitationManageUserThrottle,
    InvitationTokenThrottle,
)

from .. import policies, selectors, services
from ..models import Invitation, InvitationKind, InvitationStatus
from . import serializers as s

TAG = "invitations"


# ------------------------------------------------------------------------------------------------ management
class _Invitation(ResourceView):
    tag = TAG
    resource = policies.invitations
    read_permission, write_permission = "invitation.read", "invitation.manage"
    output_serializer = s.InvitationOut

    def base_queryset(self) -> QuerySet[Invitation]:
        return selectors.invitation_queryset()


@document_resource
class InvitationList(_Invitation, ResourceListView):
    create_serializer = s.InvitationCreateIn
    filters = [
        Filter("status", "status", serializers.ChoiceField(InvitationStatus.choices)),
        Filter("kind", "kind", serializers.ChoiceField(InvitationKind.choices)),
        Filter("channel", "channel", serializers.ChoiceField(Channel.choices)),
    ]

    def get_throttles(self) -> list[BaseThrottle]:
        return [InvitationManageUserThrottle()] if self.request.method == "POST" else []

    def perform_create(self, data: dict[str, Any]) -> Invitation:
        return services.create_invitation(self.actor, services.NewInvitation(**data))


@document_resource
class InvitationDetail(_Invitation, ResourceDetailView):
    pass


class _InvitationAction(TenantAPIView):
    required_permissions = {"POST": "invitation.manage"}

    def get_throttles(self) -> list[BaseThrottle]:
        return [InvitationManageUserThrottle()]

    def load(self, pk: Any) -> Invitation:
        if DataScope.SCHOOL not in self.actor.scopes("invitation.manage"):
            self.permission_denied(self.request)
        return policies.invitations.get(self.actor, "invitation.manage", pk)

    def respond(self, invitation: Invitation) -> Response:
        return Response(s.InvitationOut(selectors.invitation_queryset().get(pk=invitation.pk)).data)


class InvitationResendView(_InvitationAction):
    @extend_schema(
        tags=[TAG],
        summary="Resend an invitation with a new link",
        description=(
            "Requires `invitation.manage`. Issues a new secret and deadline (the previous link stops "
            "working). "
            "Allowed for pending and expired invitations, at most once per `INVITATION_RESEND_SECONDS`."
        ),
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.InvitationOut, **errors(401, 403, 404, 409, 429, 503)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.respond(services.resend_invitation(self.actor, self.load(pk)))


class InvitationRevokeView(_InvitationAction):
    @extend_schema(
        tags=[TAG],
        summary="Revoke an invitation",
        description="Requires `invitation.manage`. A revocation never removes accounts or memberships.",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.InvitationOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.respond(services.revoke_invitation(self.actor, self.load(pk)))


# ------------------------------------------------------------------------------------------------ recipient
class _RecipientView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [InvitationIpThrottle, InvitationTokenThrottle]


class InvitationPreviewView(_RecipientView):
    authentication_classes = []

    @extend_schema(
        tags=[TAG],
        summary="What an invitation is for (public)",
        description=(
            "Every unusable secret (unknown, expired, revoked, accepted) gives the same "
            "`404 invitation_invalid`."
        ),
        request=s.TokenIn,
        responses={200: s.InvitationPreviewOut, **errors(400, 404, 429)},
    )
    def post(self, request: Request) -> Response:
        body = s.TokenIn(data=request.data)
        body.is_valid(raise_exception=True)
        preview = services.preview(body.validated_data["token"])
        return Response(s.InvitationPreviewOut(preview).data)


class InvitationVerificationView(_RecipientView):
    authentication_classes = []

    @extend_schema(
        tags=[TAG],
        summary="Send a one-time code to the invited address (public)",
        description=(
            "The code goes to the invitation's own email or number. `503` if that channel cannot deliver."
        ),
        request=s.TokenIn,
        responses={200: s.VerificationOut, **errors(400, 404, 429, 503)},
    )
    def post(self, request: Request) -> Response:
        body = s.TokenIn(data=request.data)
        body.is_valid(raise_exception=True)
        result = services.request_verification(body.validated_data["token"])
        data: dict[str, Any] = {
            "challenge_id": result.challenge_id,
            "expires_in": result.expires_in,
            "resend_in": result.resend_in,
        }
        if result.dev_code is not None:
            data["dev_code"] = result.dev_code
        return Response(data)


class InvitationAcceptView(_RecipientView):
    """Bearer token optional: present means "accept with my existing account", absent means "create one"."""

    @extend_schema(
        tags=[TAG],
        summary="Accept an invitation (public, or signed in)",
        description=(
            "Needs a code from `/invitations/verification`. Without a bearer token a new account is "
            "created for "
            "the invited address and signed in (`409 account_exists` if one already uses it: sign in first). "
            "With a bearer token the signed-in account accepts. Either way the account joins the school with "
            "the invitation's roles and profile link."
        ),
        request=s.AcceptIn,
        responses={200: s.AcceptanceOut, **errors(400, 401, 404, 409, 429)},
    )
    def post(self, request: Request) -> Response:
        body = s.AcceptIn(data=request.data)
        body.is_valid(raise_exception=True)
        signed_in = request_user(request) if isinstance(request.user, User) else None
        acceptance = services.accept_invitation(
            body.validated_data["token"],
            challenge_id=str(body.validated_data["challenge_id"]),
            code=body.validated_data["code"],
            signed_in=signed_in,
        )
        return Response(
            {
                "membership": MyMembershipOut(acceptance.membership).data,
                "session": session_payload(acceptance.session) if acceptance.session else None,
            }
        )
