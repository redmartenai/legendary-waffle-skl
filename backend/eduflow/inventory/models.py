"""Inventory, assets and procurement (screen documentation "Inventory", "Assets", "Vendors", "Purchase";
prototype approval kind ``expense``).

Stock
    An **item** is a consumable (chalk, paper). Its stock is the sum of its **movements**: receipts (+),
    issues (-) and adjustments (+/-). An issue never takes stock below zero (checked under a row lock).
    At or below the reorder level the item is flagged.

Assets
    A tagged durable thing (projector, bench) with its location, custodian and state. **Depreciation is not
    calculated**: no source defines a method, rate or useful life. Purchase cost and date are kept so a
    method can be added once the school specifies one.

Procurement
    vendor -> requisition (approved in the queue as ``expense``) -> purchase order (draft -> issued) ->
    goods received (lines received; stock items get receipt movements; the order is ``received`` when every
    line is complete) -> vendor invoice (unpaid -> paid).
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from eduflow.core.ids import uuid7
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _uniq(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"inventory_{model}_id_school_uniq")


# ------------------------------------------------------------------------------------------------ stock
class Item(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=150)
    sku = models.CharField(max_length=40)
    unit = models.CharField(max_length=20, default="unit")
    category = models.CharField(max_length=60, blank=True)
    reorder_level = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    is_active = models.BooleanField(default=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_item"
        constraints = [
            models.UniqueConstraint(fields=["school", "sku"], name="inventory_item_sku_uniq"),
            _uniq("item"),
        ]


class MovementKind(models.TextChoices):
    RECEIPT = "receipt", "Receipt"
    ISSUE = "issue", "Issue"
    ADJUSTMENT = "adjustment", "Adjustment"


class StockMovement(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=16, choices=MovementKind.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, help_text="Signed: receipts +, issues -.")
    note = models.CharField(max_length=300, blank=True)
    issued_to = models.CharField(max_length=150, blank=True)
    goods_receipt_line = models.ForeignKey(
        "ReceiptLine", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    recorded_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_stock_movement"
        constraints = [
            models.CheckConstraint(
                condition=(Q(kind="receipt") & Q(quantity__gt=0))
                | (Q(kind="issue") & Q(quantity__lt=0))
                | (Q(kind="adjustment") & ~Q(quantity=0)),
                name="inventory_movement_sign_check",
            ),
            _uniq("stock_movement"),
        ]


# ------------------------------------------------------------------------------------------------ assets
class AssetStatus(models.TextChoices):
    IN_USE = "in_use", "In use"
    IN_STORE = "in_store", "In store"
    IN_REPAIR = "in_repair", "In repair"
    DISPOSED = "disposed", "Disposed"


class Asset(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    tag = models.CharField(max_length=40)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=60, blank=True)
    location = models.CharField(max_length=100, blank=True)
    custodian = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    purchase_date = models.DateField(null=True, blank=True)
    purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    vendor = models.ForeignKey("Vendor", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=16, choices=AssetStatus.choices, default=AssetStatus.IN_USE)
    disposed_on = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=1000, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_asset"
        constraints = [
            models.UniqueConstraint(fields=["school", "tag"], name="inventory_asset_tag_uniq"),
            models.CheckConstraint(
                condition=~Q(status="disposed") | Q(disposed_on__isnull=False),
                name="inventory_disposed_check",
            ),
            _uniq("asset"),
        ]


# ------------------------------------------------------------------------------------------------ procurement
class Vendor(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    tax_id = models.CharField(max_length=32, blank=True, help_text="GSTIN or other tax registration.")
    address = models.CharField(max_length=500, blank=True)
    is_active = models.BooleanField(default=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_vendor"
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="inventory_vendor_name_uniq"),
            _uniq("vendor"),
        ]


class RequisitionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"


class Requisition(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    title = models.CharField(max_length=200)
    justification = models.CharField(max_length=1000)
    estimated_total = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=RequisitionStatus.choices, default=RequisitionStatus.PENDING
    )
    requested_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_requisition"
        constraints = [
            models.CheckConstraint(
                condition=Q(estimated_total__gt=0), name="inventory_requisition_total_check"
            ),
            _uniq("requisition"),
        ]


class OrderStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ISSUED = "issued", "Issued"
    RECEIVED = "received", "Received"
    CANCELLED = "cancelled", "Cancelled"


class PurchaseOrder(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    number = models.CharField(max_length=32)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="orders")
    requisition = models.ForeignKey(
        Requisition, on_delete=models.PROTECT, null=True, blank=True, related_name="orders"
    )
    status = models.CharField(max_length=16, choices=OrderStatus.choices, default=OrderStatus.DRAFT)
    issued_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_purchase_order"
        constraints = [
            models.UniqueConstraint(fields=["school", "number"], name="inventory_po_number_uniq"),
            _uniq("purchase_order"),
        ]


class OrderLine(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    received_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_order_line"
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="inventory_line_qty_check"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="inventory_line_price_check"),
            models.CheckConstraint(
                condition=Q(received_quantity__gte=0) & Q(received_quantity__lte=F("quantity")),
                name="inventory_line_received_check",
            ),
            _uniq("order_line"),
        ]


class GoodsReceipt(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    received_on = models.DateField()
    note = models.CharField(max_length=300, blank=True)
    received_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_goods_receipt"
        constraints = [_uniq("goods_receipt")]


class ReceiptLine(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    line = models.ForeignKey(OrderLine, on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_receipt_line"
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="inventory_receipt_qty_check"),
            _uniq("receipt_line"),
        ]


class InvoiceStatus(models.TextChoices):
    UNPAID = "unpaid", "Unpaid"
    PAID = "paid", "Paid"


class VendorInvoice(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField(max_length=50)
    invoice_date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=16, choices=InvoiceStatus.choices, default=InvoiceStatus.UNPAID)
    paid_on = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=100, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "inventory_vendor_invoice"
        constraints = [
            models.UniqueConstraint(fields=["order", "number"], name="inventory_invoice_number_uniq"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="inventory_invoice_amount_check"),
            models.CheckConstraint(
                condition=Q(status="unpaid") | Q(paid_on__isnull=False), name="inventory_invoice_paid_check"
            ),
            _uniq("vendor_invoice"),
        ]
