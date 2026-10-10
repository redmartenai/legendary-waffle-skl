"""Inventory and procurement writes. Transactional and audited (``inventory.*``)."""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.tenancy import clock, domain
from eduflow.tenancy.models import Membership

from .models import (
    Asset,
    AssetStatus,
    GoodsReceipt,
    InvoiceStatus,
    Item,
    MovementKind,
    OrderLine,
    OrderStatus,
    PurchaseOrder,
    ReceiptLine,
    Requisition,
    RequisitionStatus,
    StockMovement,
    Vendor,
    VendorInvoice,
)

ZERO = Decimal(0)


# ------------------------------------------------------------------------------------------------ stock
def with_stock(qs: QuerySet[Item]) -> QuerySet[Item]:
    total = (
        StockMovement.objects.filter(item=OuterRef("pk"))
        .values("item")
        .annotate(t=Sum("quantity"))
        .values("t")
    )
    qty = DecimalField(max_digits=14, decimal_places=3)
    return qs.annotate(on_hand=Coalesce(Subquery(total, output_field=qty), Value(ZERO), output_field=qty))


def on_hand(item: Item) -> Decimal:
    return item.movements.aggregate(t=Sum("quantity"))["t"] or ZERO


@transaction.atomic
def move_stock(
    actor: Actor, *, item_id: Any, kind: str, quantity: Decimal, note: str = "", issued_to: str = ""
) -> StockMovement:
    item = Item.objects.select_for_update().filter(school_id=actor.school.pk, pk=item_id).first()
    if item is None:
        raise ValidationError({"item_id": ["Unknown item."]})
    if quantity == 0 or (kind != MovementKind.ADJUSTMENT and quantity < 0):
        raise ValidationError({"quantity": ["Receipts and issues are positive; an adjustment is not zero."]})
    signed = -quantity if kind == MovementKind.ISSUE else quantity
    if on_hand(item) + signed < 0:
        raise Conflict(f"Only {on_hand(item)} {item.unit} in stock.")
    movement = StockMovement.objects.create(
        school=actor.school,
        item=item,
        kind=kind,
        quantity=signed,
        note=note,
        issued_to=issued_to,
        recorded_by=actor.membership,
    )
    domain.record("inventory.stock.moved", movement, item=str(item.pk), kind=kind, quantity=str(signed))
    return movement


def low_stock(school: Any) -> list[Item]:
    items = with_stock(Item.objects.filter(school_id=school.pk, is_active=True, reorder_level__gt=0))
    return [i for i in items if i.on_hand <= i.reorder_level]  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------------------------ assets
def _asset_refs(actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    if "custodian_id" in data:
        cid = data.pop("custodian_id")
        data["custodian"] = (
            domain.resolve(Membership, actor.school, cid, "custodian_id", label="member") if cid else None
        )
    if "vendor_id" in data:
        vid = data.pop("vendor_id")
        data["vendor"] = domain.resolve(Vendor, actor.school, vid, "vendor_id") if vid else None
    return data


@transaction.atomic
def create_asset(actor: Actor, **data: Any) -> Asset:
    asset = domain.save(
        Asset(school=actor.school, **_asset_refs(actor, data)), conflict="This asset tag exists."
    )
    domain.record("inventory.asset.created", asset, tag=asset.tag)
    return asset


@transaction.atomic
def update_asset(actor: Actor, asset: Asset, **data: Any) -> Asset:
    if asset.status == AssetStatus.DISPOSED:
        raise Conflict("This asset has been disposed of.")
    data = _asset_refs(actor, data)
    if data.get("status") == AssetStatus.DISPOSED:
        data.setdefault("disposed_on", clock.today(actor.school))
    changed = domain.apply_changes(asset, data)
    if changed:
        domain.save(asset, conflict="This asset tag exists.")
        domain.record("inventory.asset.updated", asset, fields=changed, status=asset.status)
    return asset


# ------------------------------------------------------------------------------------------------ vendors
@transaction.atomic
def create_vendor(actor: Actor, **data: Any) -> Vendor:
    vendor = domain.save(Vendor(school=actor.school, **data), conflict="A vendor with this name exists.")
    domain.record("inventory.vendor.created", vendor, name=vendor.name)
    return vendor


@transaction.atomic
def update_vendor(actor: Actor, vendor: Vendor, **data: Any) -> Vendor:
    changed = domain.apply_changes(vendor, data)
    if changed:
        domain.save(vendor, conflict="A vendor with this name exists.")
        domain.record("inventory.vendor.updated", vendor, fields=changed)
    return vendor


# ------------------------------------------------------------------------------------------- requisitions
@transaction.atomic
def request_purchase(
    actor: Actor, *, title: str, justification: str, estimated_total: Decimal
) -> Requisition:
    req = Requisition.objects.create(
        school=actor.school,
        title=title,
        justification=justification,
        estimated_total=estimated_total,
        requested_by=actor.membership,
    )
    domain.record("inventory.requisition.requested", req, total=str(estimated_total))
    return req


@transaction.atomic
def decide_requisition(actor: Actor, req: Requisition, decision: str, note: str = "") -> Requisition:
    req = Requisition.objects.select_for_update(of=("self",)).get(pk=req.pk)
    if req.status != RequisitionStatus.PENDING:
        raise Conflict("This requisition has already been decided.")
    req.status = RequisitionStatus.APPROVED if decision == "approve" else RequisitionStatus.DECLINED
    req.decided_by, req.decided_at, req.decision_note = actor.membership, timezone.now(), note
    req.save()
    domain.record("inventory.requisition.decided", req, decision=req.status)
    return req


# ------------------------------------------------------------------------------------------------ orders
def _lines(actor: Actor, order: PurchaseOrder, lines: Iterable[dict[str, Any]]) -> None:
    order.lines.all().delete()
    for i, line in enumerate(lines):
        item_id = line.pop("item_id", None)
        item = (
            domain.resolve(Item, actor.school, item_id, f"lines[{i}].item_id", label="item")
            if item_id
            else None
        )
        OrderLine.objects.create(school=actor.school, order=order, item=item, **line)


@transaction.atomic
def create_order(
    actor: Actor, *, number: str, vendor_id: Any, lines: list[dict[str, Any]], requisition_id: Any = None
) -> PurchaseOrder:
    vendor = domain.resolve(Vendor, actor.school, vendor_id, "vendor_id", label="vendor")
    if not vendor.is_active:
        raise ValidationError({"vendor_id": ["This vendor is inactive."]})
    req = None
    if requisition_id:
        req = domain.resolve(Requisition, actor.school, requisition_id, "requisition_id", label="requisition")
        if req.status != RequisitionStatus.APPROVED:
            raise ValidationError({"requisition_id": ["The requisition is not approved."]})
    order = domain.save(
        PurchaseOrder(
            school=actor.school, number=number, vendor=vendor, requisition=req, created_by=actor.membership
        ),
        conflict="This order number exists.",
    )
    _lines(actor, order, lines)
    domain.record("inventory.purchase_order.created", order, total=str(order_total(order)))
    return order


def order_total(order: PurchaseOrder) -> Decimal:
    return sum((line.quantity * line.unit_price for line in order.lines.all()), ZERO)


@transaction.atomic
def update_order(actor: Actor, order: PurchaseOrder, **data: Any) -> PurchaseOrder:
    if order.status != OrderStatus.DRAFT:
        raise Conflict(f"This order is {order.status}; it cannot be changed.")
    if "lines" in data:
        _lines(actor, order, data.pop("lines"))
    changed = domain.apply_changes(order, data)
    if changed:
        domain.save(order, conflict="This order number exists.")
    domain.record("inventory.purchase_order.updated", order, total=str(order_total(order)))
    return order


@transaction.atomic
def set_order_status(actor: Actor, order: PurchaseOrder, status: str) -> PurchaseOrder:
    order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    allowed = {
        OrderStatus.DRAFT: {OrderStatus.ISSUED, OrderStatus.CANCELLED},
        OrderStatus.ISSUED: {OrderStatus.CANCELLED},
    }
    if status not in allowed.get(OrderStatus(order.status), set()):
        raise Conflict(f"An order that is {order.status} cannot become {status}.")
    if status == OrderStatus.CANCELLED and order.receipts.exists():
        raise Conflict("Goods were received against this order.")
    if status == OrderStatus.ISSUED and not order.lines.exists():
        raise ValidationError({"lines": ["An order needs at least one line."]})
    order.status = status
    if status == OrderStatus.ISSUED:
        order.issued_at = timezone.now()
    order.save()
    domain.record("inventory.purchase_order.status_changed", order, status=status)
    return order


@transaction.atomic
def receive(
    actor: Actor,
    order: PurchaseOrder,
    *,
    lines: list[dict[str, Any]],
    received_on: datetime.date | None = None,
    note: str = "",
) -> GoodsReceipt:
    order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != OrderStatus.ISSUED:
        raise Conflict("Goods are received only against an issued order.")
    receipt = GoodsReceipt.objects.create(
        school=actor.school,
        order=order,
        received_on=received_on or clock.today(actor.school),
        note=note,
        received_by=actor.membership,
    )
    for i, entry in enumerate(lines):
        line = OrderLine.objects.select_for_update().filter(order=order, pk=entry["line_id"]).first()
        if line is None:
            raise ValidationError({f"lines[{i}].line_id": ["This line is not on the order."]})
        if line.received_quantity + entry["quantity"] > line.quantity:
            raise ValidationError(
                {f"lines[{i}].quantity": [f"Only {line.quantity - line.received_quantity} outstanding."]}
            )
        line.received_quantity += entry["quantity"]
        line.save(update_fields=["received_quantity"])
        rline = ReceiptLine.objects.create(
            school=actor.school, receipt=receipt, line=line, quantity=entry["quantity"]
        )
        if line.item_id:
            StockMovement.objects.create(
                school=actor.school,
                item_id=line.item_id,
                kind=MovementKind.RECEIPT,
                quantity=entry["quantity"],
                note=f"PO {order.number}",
                goods_receipt_line=rline,
                recorded_by=actor.membership,
            )
    if not order.lines.exclude(received_quantity=F("quantity")).exists():
        order.status = OrderStatus.RECEIVED
        order.save(update_fields=["status"])
    domain.record("inventory.goods_receipt.recorded", receipt, order=str(order.pk), lines=len(lines))
    return receipt


@transaction.atomic
def add_invoice(actor: Actor, order: PurchaseOrder, **data: Any) -> VendorInvoice:
    if order.status in (OrderStatus.DRAFT, OrderStatus.CANCELLED):
        raise Conflict("Invoices are recorded against issued or received orders.")
    invoice = domain.save(
        VendorInvoice(school=actor.school, order=order, **data), conflict="This invoice exists."
    )
    domain.record("inventory.vendor_invoice.recorded", invoice, amount=str(invoice.amount))
    return invoice


@transaction.atomic
def pay_invoice(
    actor: Actor, invoice: VendorInvoice, *, paid_on: datetime.date, payment_reference: str = ""
) -> VendorInvoice:
    invoice = VendorInvoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == InvoiceStatus.PAID:
        raise Conflict("This invoice is already paid.")
    invoice.status, invoice.paid_on, invoice.payment_reference = (
        InvoiceStatus.PAID,
        paid_on,
        payment_reference,
    )
    invoice.save()
    domain.record("inventory.vendor_invoice.paid", invoice, amount=str(invoice.amount))
    return invoice
