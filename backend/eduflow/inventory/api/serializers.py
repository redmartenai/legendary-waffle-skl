from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer

from ..models import (
    Asset,
    AssetStatus,
    GoodsReceipt,
    Item,
    MovementKind,
    OrderLine,
    OrderStatus,
    PurchaseOrder,
    Requisition,
    StockMovement,
    Vendor,
    VendorInvoice,
)

ZERO = Decimal(0)


def Money(**kwargs: Any) -> serializers.DecimalField:
    return serializers.DecimalField(max_digits=12, decimal_places=2, **kwargs)


def Qty(**kwargs: Any) -> serializers.DecimalField:
    return serializers.DecimalField(max_digits=12, decimal_places=3, **kwargs)


class ItemOut(serializers.ModelSerializer[Item]):
    on_hand = Qty(read_only=True)
    below_reorder = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = (
            "id",
            "name",
            "sku",
            "unit",
            "category",
            "reorder_level",
            "is_active",
            "on_hand",
            "below_reorder",
        )
        read_only_fields = fields

    def get_below_reorder(self, item: Item) -> bool:
        return bool(item.reorder_level and item.on_hand <= item.reorder_level)  # type: ignore[attr-defined]


class ItemIn(StrictSerializer):
    name = serializers.CharField(max_length=150)
    sku = serializers.CharField(max_length=40)
    unit = serializers.CharField(max_length=20, required=False)
    category = serializers.CharField(max_length=60, required=False, allow_blank=True)
    reorder_level = Qty(min_value=ZERO, required=False)


class ItemUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=150, required=False)
    unit = serializers.CharField(max_length=20, required=False)
    category = serializers.CharField(max_length=60, required=False, allow_blank=True)
    reorder_level = Qty(min_value=ZERO, required=False)
    is_active = serializers.BooleanField(required=False)


class MovementOut(serializers.ModelSerializer[StockMovement]):
    item_id = serializers.UUIDField()

    class Meta:
        model = StockMovement
        fields = ("id", "item_id", "kind", "quantity", "note", "issued_to", "created_at")
        read_only_fields = fields


class MovementIn(StrictSerializer):
    item_id = serializers.UUIDField()
    kind = serializers.ChoiceField(MovementKind.choices)
    quantity = Qty(help_text="Positive for receipts and issues; signed for adjustments.")
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)
    issued_to = serializers.CharField(max_length=150, required=False, allow_blank=True)


class AssetOut(serializers.ModelSerializer[Asset]):
    custodian = serializers.CharField(source="custodian.user.full_name", allow_null=True, default=None)
    vendor_id = serializers.UUIDField(allow_null=True)

    class Meta:
        model = Asset
        fields = (
            "id",
            "tag",
            "name",
            "category",
            "location",
            "custodian",
            "purchase_date",
            "purchase_cost",
            "vendor_id",
            "status",
            "disposed_on",
            "notes",
        )
        read_only_fields = fields


class AssetIn(StrictSerializer):
    tag = serializers.CharField(max_length=40)
    name = serializers.CharField(max_length=150)
    category = serializers.CharField(max_length=60, required=False, allow_blank=True)
    location = serializers.CharField(max_length=100, required=False, allow_blank=True)
    custodian_id = serializers.UUIDField(required=False, allow_null=True)
    purchase_date = serializers.DateField(required=False, allow_null=True)
    purchase_cost = Money(min_value=ZERO, required=False, allow_null=True)
    vendor_id = serializers.UUIDField(required=False, allow_null=True)
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class AssetUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=150, required=False)
    location = serializers.CharField(max_length=100, required=False, allow_blank=True)
    custodian_id = serializers.UUIDField(required=False, allow_null=True)
    status = serializers.ChoiceField(AssetStatus.choices, required=False)
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class VendorOut(serializers.ModelSerializer[Vendor]):
    class Meta:
        model = Vendor
        fields = ("id", "name", "contact_person", "phone", "email", "tax_id", "address", "is_active")
        read_only_fields = fields


class VendorIn(StrictSerializer):
    name = serializers.CharField(max_length=200)
    contact_person = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    tax_id = serializers.CharField(max_length=32, required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)


class VendorUpdateIn(VendorIn):
    name = serializers.CharField(max_length=200, required=False)
    is_active = serializers.BooleanField(required=False)


class RequisitionOut(serializers.ModelSerializer[Requisition]):
    requested_by = serializers.CharField(source="requested_by.user.full_name")

    class Meta:
        model = Requisition
        fields = (
            "id",
            "title",
            "justification",
            "estimated_total",
            "status",
            "requested_by",
            "decided_at",
            "decision_note",
            "created_at",
        )
        read_only_fields = fields


class RequisitionIn(StrictSerializer):
    title = serializers.CharField(max_length=200)
    justification = serializers.CharField(max_length=1000)
    estimated_total = Money(min_value=Decimal("0.01"))


class OrderLineOut(serializers.ModelSerializer[OrderLine]):
    item_id = serializers.UUIDField(allow_null=True)

    class Meta:
        model = OrderLine
        fields = ("id", "item_id", "description", "quantity", "unit_price", "received_quantity")
        read_only_fields = fields


class OrderLineIn(StrictSerializer):
    item_id = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(max_length=300)
    quantity = Qty(min_value=Decimal("0.001"))
    unit_price = Money(min_value=ZERO)


class OrderOut(serializers.ModelSerializer[PurchaseOrder]):
    vendor = VendorOut()
    requisition_id = serializers.UUIDField(allow_null=True)
    lines = OrderLineOut(many=True)
    total = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = (
            "id",
            "number",
            "vendor",
            "requisition_id",
            "status",
            "issued_at",
            "lines",
            "total",
            "created_at",
        )
        read_only_fields = fields

    def get_total(self, order: PurchaseOrder) -> str:
        total = sum((line.quantity * line.unit_price for line in order.lines.all()), ZERO)
        return f"{total:.2f}"


class OrderIn(StrictSerializer):
    number = serializers.CharField(max_length=32)
    vendor_id = serializers.UUIDField()
    requisition_id = serializers.UUIDField(required=False, allow_null=True)
    lines = OrderLineIn(many=True, allow_empty=False)


class OrderUpdateIn(StrictSerializer):
    number = serializers.CharField(max_length=32, required=False)
    lines = OrderLineIn(many=True, allow_empty=False, required=False)


class OrderStatusIn(StrictSerializer):
    status = serializers.ChoiceField([OrderStatus.ISSUED, OrderStatus.CANCELLED])


class ReceiveLineIn(StrictSerializer):
    line_id = serializers.UUIDField()
    quantity = Qty(min_value=Decimal("0.001"))


class ReceiveIn(StrictSerializer):
    received_on = serializers.DateField(required=False)
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)
    lines = ReceiveLineIn(many=True, allow_empty=False)


class ReceiptOut(serializers.ModelSerializer[GoodsReceipt]):
    class Meta:
        model = GoodsReceipt
        fields = ("id", "received_on", "note")
        read_only_fields = fields


class InvoiceOut(serializers.ModelSerializer[VendorInvoice]):
    order_id = serializers.UUIDField()

    class Meta:
        model = VendorInvoice
        fields = (
            "id",
            "order_id",
            "number",
            "invoice_date",
            "amount",
            "status",
            "paid_on",
            "payment_reference",
        )
        read_only_fields = fields


class InvoiceIn(StrictSerializer):
    number = serializers.CharField(max_length=50)
    invoice_date = serializers.DateField()
    amount = Money(min_value=Decimal("0.01"))


class PayIn(StrictSerializer):
    paid_on = serializers.DateField()
    payment_reference = serializers.CharField(max_length=100, required=False, allow_blank=True)
