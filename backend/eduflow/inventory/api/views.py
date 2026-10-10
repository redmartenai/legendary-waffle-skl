"""Inventory and procurement endpoints. ``inventory.read`` / ``inventory.manage`` for stock and assets;
``procurement.read`` / ``procurement.manage`` for vendors, orders, receipts and invoices;
``procurement.request`` to raise a requisition (staff: their own); ``procurement.approve`` decides
requisitions in the approvals queue (``expense``)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
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
from eduflow.tenancy import domain

from .. import policies, services
from ..models import (
    Asset,
    AssetStatus,
    InvoiceStatus,
    Item,
    MovementKind,
    OrderStatus,
    PurchaseOrder,
    Requisition,
    RequisitionStatus,
    StockMovement,
    Vendor,
    VendorInvoice,
)
from . import serializers as s

TAG = "inventory"


def _school_wide(view: TenantAPIView, permission: str) -> None:
    if DataScope.SCHOOL not in view.actor.scopes(permission):
        view.permission_denied(view.request)


# ------------------------------------------------------------------------------------------------ stock
class _Item(ResourceView):
    tag = TAG
    resource = policies.items
    read_permission, write_permission = "inventory.read", "inventory.manage"
    output_serializer = s.ItemOut

    def base_queryset(self) -> QuerySet[Item]:
        return services.with_stock(Item.objects.all())


@document_resource
class ItemList(_Item, ResourceListView):
    create_serializer = s.ItemIn
    filters = [
        Filter("category", "category", serializers.CharField()),
        Filter("is_active", "is_active", serializers.BooleanField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Item:
        item = domain.save(Item(school=self.actor.school, **data), conflict="This SKU exists.")
        domain.record("inventory.item.created", item, sku=item.sku)
        return item


@document_resource
class ItemDetail(_Item, ResourceDetailView):
    update_serializer = s.ItemUpdateIn

    def perform_update(self, obj: Item, data: dict[str, Any]) -> Item:
        changed = domain.apply_changes(obj, data)
        if changed:
            obj.save()
            domain.record("inventory.item.updated", obj, fields=changed)
        return obj


class _Movement(ResourceView):
    tag = TAG
    resource = policies.movements
    read_permission, write_permission = "inventory.read", "inventory.manage"
    output_serializer = s.MovementOut


@document_resource
class MovementList(_Movement, ResourceListView):
    create_serializer = s.MovementIn
    filters = [
        Filter("item_id", "item_id", serializers.UUIDField()),
        Filter("kind", "kind", serializers.ChoiceField(MovementKind.choices)),
    ]

    def perform_create(self, data: dict[str, Any]) -> StockMovement:
        return services.move_stock(self.actor, **data)


# ------------------------------------------------------------------------------------------------ assets
class _Asset(ResourceView):
    tag = TAG
    resource = policies.assets
    read_permission, write_permission = "inventory.read", "inventory.manage"
    output_serializer = s.AssetOut

    def base_queryset(self) -> QuerySet[Asset]:
        return Asset.objects.select_related("custodian__user")


@document_resource
class AssetList(_Asset, ResourceListView):
    create_serializer = s.AssetIn
    filters = [
        Filter("status", "status", serializers.ChoiceField(AssetStatus.choices)),
        Filter("category", "category", serializers.CharField()),
        Filter("location", "location", serializers.CharField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Asset:
        return services.create_asset(self.actor, **data)


@document_resource
class AssetDetail(_Asset, ResourceDetailView):
    update_serializer = s.AssetUpdateIn

    def perform_update(self, obj: Asset, data: dict[str, Any]) -> Asset:
        return services.update_asset(self.actor, obj, **data)


# ------------------------------------------------------------------------------------------------ vendors
class _Vendor(ResourceView):
    tag = TAG
    resource = policies.vendors
    read_permission, write_permission = "procurement.read", "procurement.manage"
    output_serializer = s.VendorOut


@document_resource
class VendorList(_Vendor, ResourceListView):
    create_serializer = s.VendorIn
    filters = [Filter("is_active", "is_active", serializers.BooleanField())]

    def perform_create(self, data: dict[str, Any]) -> Vendor:
        return services.create_vendor(self.actor, **data)


@document_resource
class VendorDetail(_Vendor, ResourceDetailView):
    update_serializer = s.VendorUpdateIn

    def perform_update(self, obj: Vendor, data: dict[str, Any]) -> Vendor:
        return services.update_vendor(self.actor, obj, **data)


# ------------------------------------------------------------------------------------------- requisitions
class _Requisition(ResourceView):
    tag = TAG
    resource = policies.requisitions
    read_permission, write_permission = "procurement.request", "procurement.request"
    output_serializer = s.RequisitionOut
    narrow_writes = True  # anyone allowed to request raises their own

    def base_queryset(self) -> QuerySet[Requisition]:
        return Requisition.objects.select_related("requested_by__user")


@document_resource
class RequisitionList(_Requisition, ResourceListView):
    create_serializer = s.RequisitionIn
    filters = [Filter("status", "status", serializers.ChoiceField(RequisitionStatus.choices))]

    def perform_create(self, data: dict[str, Any]) -> Requisition:
        return services.request_purchase(self.actor, **data)


@document_resource
class RequisitionDetail(_Requisition, ResourceDetailView):
    pass


# ------------------------------------------------------------------------------------------------ orders
class _Order(ResourceView):
    tag = TAG
    resource = policies.orders
    read_permission, write_permission = "procurement.read", "procurement.manage"
    output_serializer = s.OrderOut

    def base_queryset(self) -> QuerySet[PurchaseOrder]:
        return PurchaseOrder.objects.select_related("vendor").prefetch_related("lines")


@document_resource
class OrderList(_Order, ResourceListView):
    create_serializer = s.OrderIn
    filters = [
        Filter("status", "status", serializers.ChoiceField(OrderStatus.choices)),
        Filter("vendor_id", "vendor_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> PurchaseOrder:
        return services.create_order(self.actor, **data)


@document_resource
class OrderDetail(_Order, ResourceDetailView):
    update_serializer = s.OrderUpdateIn

    def perform_update(self, obj: PurchaseOrder, data: dict[str, Any]) -> PurchaseOrder:
        return services.update_order(self.actor, obj, **data)


class _OrderAction(TenantAPIView):
    required_permissions = {"POST": "procurement.manage"}

    def order(self, pk: Any) -> PurchaseOrder:
        _school_wide(self, "procurement.manage")
        return policies.orders.get(self.actor, "procurement.manage", pk)

    def respond(self, order: PurchaseOrder, status: int = 200) -> Response:
        fresh = PurchaseOrder.objects.select_related("vendor").prefetch_related("lines").get(pk=order.pk)
        return Response(s.OrderOut(fresh).data, status=status)


class OrderStatusView(_OrderAction):
    @extend_schema(
        tags=[TAG],
        summary="Issue or cancel a purchase order",
        parameters=[TENANT_HEADER],
        request=s.OrderStatusIn,
        responses={200: s.OrderOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        order = self.order(pk)
        body = s.OrderStatusIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.set_order_status(self.actor, order, body.validated_data["status"]))


class OrderReceiveView(_OrderAction):
    @extend_schema(
        tags=[TAG],
        summary="Record goods received against an issued order",
        description="Stock items get receipt movements. The order is `received` once every line is complete.",
        parameters=[TENANT_HEADER],
        request=s.ReceiveIn,
        responses={200: s.OrderOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        order = self.order(pk)
        body = s.ReceiveIn(data=request.data)
        body.is_valid(raise_exception=True)
        services.receive(self.actor, order, **body.validated_data)
        return self.respond(order)


class OrderInvoicesView(TenantAPIView):
    required_permissions = {"GET": "procurement.read", "POST": "procurement.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Invoices of an order",
        parameters=[TENANT_HEADER],
        responses={200: s.InvoiceOut(many=True), **errors(401, 403, 404)},
    )
    def get(self, request: Request, pk: Any) -> Response:
        _school_wide(self, "procurement.read")
        order = policies.orders.get(self.actor, "procurement.read", pk)
        return Response(s.InvoiceOut(order.invoices.all(), many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Record a vendor invoice",
        parameters=[TENANT_HEADER],
        request=s.InvoiceIn,
        responses={201: s.InvoiceOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self, "procurement.manage")
        order = policies.orders.get(self.actor, "procurement.manage", pk)
        body = s.InvoiceIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.InvoiceOut(services.add_invoice(self.actor, order, **body.validated_data)).data, status=201
        )


class _Invoice(ResourceView):
    tag = TAG
    resource = policies.invoices
    read_permission = "procurement.read"
    output_serializer = s.InvoiceOut


@document_resource
class InvoiceList(_Invoice, ResourceListView):
    filters = [Filter("status", "status", serializers.ChoiceField(InvoiceStatus.choices))]


class InvoicePayView(TenantAPIView):
    required_permissions = {"POST": "procurement.manage"}

    @extend_schema(
        tags=[TAG],
        summary="Record that an invoice was paid",
        parameters=[TENANT_HEADER],
        request=s.PayIn,
        responses={200: s.InvoiceOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self, "procurement.manage")
        invoice: VendorInvoice = policies.invoices.get(self.actor, "procurement.manage", pk)
        body = s.PayIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(s.InvoiceOut(services.pay_invoice(self.actor, invoice, **body.validated_data)).data)
