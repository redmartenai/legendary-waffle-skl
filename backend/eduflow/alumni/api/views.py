"""Alumni endpoints: ``alumni.read`` / ``alumni.manage`` (the office, school-wide)."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, DecimalField, QuerySet, Sum, Value
from django.db.models.functions import Coalesce
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

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

from .. import policies, services
from ..models import Alumnus, Campaign, Event
from . import serializers as s

TAG = "alumni"
READ, MANAGE = "alumni.read", "alumni.manage"


class _Alumnus(ResourceView):
    tag = TAG
    resource = policies.alumni
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.AlumnusOut


@document_resource
class AlumnusList(_Alumnus, ResourceListView):
    create_serializer = s.AlumnusIn
    filters = [
        Filter("graduation_year", "graduation_year", serializers.IntegerField()),
        Filter("consent_to_contact", "consent_to_contact", serializers.BooleanField()),
        Filter("city", "city__iexact", serializers.CharField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Alumnus:
        return services.add_alumnus(self.actor, **data)


@document_resource
class AlumnusDetail(_Alumnus, ResourceDetailView):
    update_serializer = s.AlumnusUpdateIn

    def perform_update(self, obj: Alumnus, data: dict[str, Any]) -> Alumnus:
        return services.update_alumnus(self.actor, obj, **data)


class _Event(ResourceView):
    tag = TAG
    resource = policies.events
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.EventOut

    def base_queryset(self) -> QuerySet[Event]:
        return Event.objects.annotate(registered=Count("registrations"))


@document_resource
class EventList(_Event, ResourceListView):
    create_serializer = s.EventIn

    def perform_create(self, data: dict[str, Any]) -> Event:
        return services.create_event(self.actor, **data)


@document_resource
class EventDetail(_Event, ResourceDetailView):
    pass


class EventRegistrationsView(TenantAPIView):
    required_permissions = {"GET": READ, "POST": MANAGE}

    def _event(self, permission: str, pk: Any) -> Event:
        if DataScope.SCHOOL not in self.actor.scopes(permission):
            self.permission_denied(self.request)
        return policies.events.get(self.actor, permission, pk)

    @extend_schema(
        tags=[TAG],
        summary="Registrations for an event",
        parameters=[TENANT_HEADER],
        responses={200: s.RegistrationOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        event = self._event(READ, pk)
        return Response(s.RegistrationOut(event.registrations.select_related("alumnus"), many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Register an alumnus for an event",
        parameters=[TENANT_HEADER],
        request=s.RegistrationIn,
        responses={201: s.RegistrationOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        event = self._event(MANAGE, pk)
        body = s.RegistrationIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.RegistrationOut(services.register(self.actor, event, **body.validated_data)).data, status=201
        )


class _Campaign(ResourceView):
    tag = TAG
    resource = policies.campaigns
    read_permission, write_permission = READ, MANAGE
    output_serializer = s.CampaignOut

    def base_queryset(self) -> QuerySet[Campaign]:
        money = DecimalField(max_digits=14, decimal_places=2)
        return Campaign.objects.annotate(
            raised=Coalesce(Sum("donations__amount"), Value(0), output_field=money)
        )


@document_resource
class CampaignList(_Campaign, ResourceListView):
    create_serializer = s.CampaignIn

    def perform_create(self, data: dict[str, Any]) -> Campaign:
        return services.create_campaign(self.actor, **data)


@document_resource
class CampaignDetail(_Campaign, ResourceDetailView):
    pass


class CampaignDonationsView(TenantAPIView):
    required_permissions = {"GET": READ, "POST": MANAGE}

    def _campaign(self, permission: str, pk: Any) -> Campaign:
        if DataScope.SCHOOL not in self.actor.scopes(permission):
            self.permission_denied(self.request)
        return policies.campaigns.get(self.actor, permission, pk)

    @extend_schema(
        tags=[TAG],
        summary="Donations to a campaign",
        parameters=[TENANT_HEADER],
        responses={200: s.DonationOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        campaign = self._campaign(READ, pk)
        return Response(s.DonationOut(campaign.donations.order_by("received_on"), many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Record a donation (receipt number issued)",
        parameters=[TENANT_HEADER],
        request=s.DonationIn,
        responses={201: s.DonationOut, **errors(400, 401, 403, 404)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        campaign = self._campaign(MANAGE, pk)
        body = s.DonationIn(data=request.data)
        body.is_valid(raise_exception=True)
        donation = services.record_donation(self.actor, campaign, **body.validated_data)
        return Response(s.DonationOut(donation).data, status=201)
