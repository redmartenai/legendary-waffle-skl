"""Inventory and procurement are office data (school scope), except requisitions: staff follow their own."""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource

from .models import Asset, Item, PurchaseOrder, Requisition, StockMovement, Vendor, VendorInvoice

items: ScopedResource[Item] = ScopedResource("stock_item", Item)
movements: ScopedResource[StockMovement] = ScopedResource("stock_movement", StockMovement)
assets: ScopedResource[Asset] = ScopedResource("asset", Asset)
vendors: ScopedResource[Vendor] = ScopedResource("vendor", Vendor)
requisitions: ScopedResource[Requisition] = ScopedResource("requisition", Requisition)
orders: ScopedResource[PurchaseOrder] = ScopedResource("purchase_order", PurchaseOrder)
invoices: ScopedResource[VendorInvoice] = ScopedResource("vendor_invoice", VendorInvoice)

# Staff may raise requisitions and follow their own.
requisitions.rule(DataScope.SELF)(lambda actor: Q(requested_by=actor.membership))
