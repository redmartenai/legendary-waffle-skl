"""Inventory and procurement: stock that never goes negative, reorder flags, assets without invented
depreciation, the requisition -> order -> receipt -> invoice flow with stock receipts, approvals, isolation
and RLS."""

import uuid
from decimal import Decimal

import pytest

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.inventory import services
from eduflow.inventory.models import Item, PurchaseOrder, Requisition, StockMovement, Vendor, VendorInvoice

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


@pytest.fixture
def chalk(world, as_member):
    return _ok(
        as_member(world.admin).post(
            "/api/v1/inventory/items",
            {"name": "Chalk box", "sku": "CH-1", "unit": "box", "reorder_level": "5"},
            format="json",
        ),
        201,
    )


def _move(client, item, kind, qty, expect=201):
    return _ok(
        client.post(
            "/api/v1/inventory/movements",
            {"item_id": item["id"], "kind": kind, "quantity": qty},
            format="json",
        ),
        expect,
    )


def test_stock_never_goes_negative(world, chalk, as_member):
    admin = as_member(world.admin)
    _move(admin, chalk, "receipt", "10")
    _move(admin, chalk, "issue", "7")
    _move(admin, chalk, "issue", "4", expect=409)
    _move(admin, chalk, "receipt", "-1", expect=400)
    _move(admin, chalk, "adjustment", "-1")
    item = _ok(admin.get(f"/api/v1/inventory/items/{chalk['id']}"))
    assert (item["on_hand"], item["below_reorder"]) == ("2.000", True)
    assert [i.sku for i in services.low_stock(world.school)] == ["CH-1"]
    assert as_member(world.teacher).get("/api/v1/inventory/items").status_code == 403


def test_assets(world, as_member):
    admin = as_member(world.admin)
    asset = _ok(
        admin.post(
            "/api/v1/inventory/assets",
            {
                "tag": "PRJ-1",
                "name": "Projector",
                "purchase_cost": "45000",
                "purchase_date": "2025-04-01",
                "custodian_id": str(world.teacher.pk),
            },
            format="json",
        ),
        201,
    )
    assert asset["custodian"] is not None
    assert "depreciation" not in asset
    disposed = _ok(
        admin.patch(f"/api/v1/inventory/assets/{asset['id']}", {"status": "disposed"}, format="json")
    )
    assert disposed["disposed_on"] is not None
    _ok(admin.patch(f"/api/v1/inventory/assets/{asset['id']}", {"location": "x"}, format="json"), 409)


def test_procurement_flow(world, chalk, as_member):
    admin, teacher = as_member(world.admin), as_member(world.teacher)
    req = _ok(
        teacher.post(
            "/api/v1/procurement/requisitions",
            {"title": "Chalk for term 2", "justification": "Out of stock", "estimated_total": "1200"},
            format="json",
        ),
        201,
    )
    assert [r["id"] for r in _ok(teacher.get("/api/v1/procurement/requisitions"))["results"]] == [req["id"]]
    vendor = _ok(
        admin.post(
            "/api/v1/procurement/vendors",
            {"name": "Stationers & Co", "tax_id": "29ABCDE1234F1Z5"},
            format="json",
        ),
        201,
    )
    order_body = {
        "number": "PO-1",
        "vendor_id": vendor["id"],
        "requisition_id": req["id"],
        "lines": [
            {"item_id": chalk["id"], "description": "Chalk", "quantity": "10", "unit_price": "100"},
            {"description": "Delivery", "quantity": "1", "unit_price": "200"},
        ],
    }
    _ok(admin.post("/api/v1/procurement/orders", order_body, format="json"), 400)  # requisition not approved
    queue = _ok(as_member(world.principal).get("/api/v1/approvals"))
    assert [(i["kind"], i["amount"]) for i in queue] == [("expense", "1200.00")]
    _ok(
        as_member(world.principal).post(
            f"/api/v1/approvals/expense/{req['id']}/decision", {"decision": "approve"}, format="json"
        )
    )
    order = _ok(admin.post("/api/v1/procurement/orders", order_body, format="json"), 201)
    assert order["total"] == "1200.00"
    _ok(
        admin.post(
            f"/api/v1/procurement/orders/{order['id']}/receive",
            {"lines": [{"line_id": order["lines"][0]["id"], "quantity": "1"}]},
            format="json",
        ),
        409,
    )
    _ok(admin.post(f"/api/v1/procurement/orders/{order['id']}/status", {"status": "issued"}, format="json"))
    _ok(admin.patch(f"/api/v1/procurement/orders/{order['id']}", {"number": "PO-2"}, format="json"), 409)
    chalk_line, delivery = order["lines"][0]["id"], order["lines"][1]["id"]
    _ok(
        admin.post(
            f"/api/v1/procurement/orders/{order['id']}/receive",
            {"lines": [{"line_id": chalk_line, "quantity": "11"}]},
            format="json",
        ),
        400,
    )
    partial = _ok(
        admin.post(
            f"/api/v1/procurement/orders/{order['id']}/receive",
            {"lines": [{"line_id": chalk_line, "quantity": "6"}]},
            format="json",
        )
    )
    assert partial["status"] == "issued"
    done = _ok(
        admin.post(
            f"/api/v1/procurement/orders/{order['id']}/receive",
            {"lines": [{"line_id": chalk_line, "quantity": "4"}, {"line_id": delivery, "quantity": "1"}]},
            format="json",
        )
    )
    assert done["status"] == "received"
    assert services.on_hand(Item.objects.get(pk=chalk["id"])) == Decimal(10)
    invoice = _ok(
        admin.post(
            f"/api/v1/procurement/orders/{order['id']}/invoices",
            {"number": "INV-9", "invoice_date": "2026-07-20", "amount": "1200"},
            format="json",
        ),
        201,
    )
    _ok(
        admin.post(
            f"/api/v1/procurement/orders/{order['id']}/invoices",
            {"number": "INV-9", "invoice_date": "2026-07-20", "amount": "1"},
            format="json",
        ),
        409,
    )
    _ok(
        admin.post(
            f"/api/v1/procurement/invoices/{invoice['id']}/pay",
            {"paid_on": "2026-07-25", "payment_reference": "NEFT-1"},
            format="json",
        )
    )
    _ok(
        admin.post(
            f"/api/v1/procurement/invoices/{invoice['id']}/pay", {"paid_on": "2026-07-25"}, format="json"
        ),
        409,
    )
    _ok(
        admin.post(
            f"/api/v1/procurement/orders/{order['id']}/status", {"status": "cancelled"}, format="json"
        ),
        409,
    )
    assert teacher.post("/api/v1/procurement/orders", order_body, format="json").status_code == 403


INVENTORY_MATRIX = [
    ("get", "/api/v1/inventory/items/{item}", None),
    ("patch", "/api/v1/inventory/items/{item}", {"name": "x"}),
    ("get", "/api/v1/procurement/vendors/{vendor}", None),
    ("patch", "/api/v1/procurement/vendors/{vendor}", {"phone": "1"}),
    ("get", "/api/v1/procurement/requisitions/{requisition}", None),
    ("get", "/api/v1/procurement/orders/{order}", None),
    ("patch", "/api/v1/procurement/orders/{order}", {"number": "x"}),
    ("post", "/api/v1/procurement/orders/{order}/status", {"status": "issued"}),
    (
        "post",
        "/api/v1/procurement/orders/{order}/receive",
        {"lines": [{"line_id": "{order}", "quantity": "1"}]},
    ),
    ("get", "/api/v1/procurement/orders/{order}/invoices", None),
    (
        "post",
        "/api/v1/procurement/orders/{order}/invoices",
        {"number": "x", "invoice_date": "2026-01-01", "amount": "1"},
    ),
    ("post", "/api/v1/procurement/invoices/{invoice}/pay", {"paid_on": "2026-01-01"}),
    ("get", "/api/v1/inventory/assets/{asset}", None),
    ("patch", "/api/v1/inventory/assets/{asset}", {"name": "x"}),
]
INVENTORY_MATRIX_PATHS = {p for _, p, _ in INVENTORY_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), INVENTORY_MATRIX)
def test_another_schools_inventory_answers_like_unknown(world, other_world, as_member, method, path, body):
    from eduflow.inventory.models import Asset

    s = other_world.school
    item = Item.objects.create(school=s, name="I", sku="S")
    vendor = Vendor.objects.create(school=s, name="V")
    req = Requisition.objects.create(
        school=s, title="R", justification="x", estimated_total=1, requested_by=other_world.admin
    )
    order = PurchaseOrder.objects.create(school=s, number="P", vendor=vendor, status="issued")
    invoice = VendorInvoice.objects.create(
        school=s, order=order, number="I", invoice_date="2026-01-01", amount=1
    )
    asset = Asset.objects.create(school=s, tag="T", name="A")
    real = {
        "item": item.pk,
        "vendor": vendor.pk,
        "requisition": req.pk,
        "order": order.pk,
        "invoice": invoice.pk,
        "asset": asset.pk,
    }
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        payload = None
        if body:
            payload = {
                k: (
                    [{kk: str(vv).format(**values) for kk, vv in e.items()} for e in v]
                    if isinstance(v, list)
                    else v
                )
                for k, v in body.items()
            }
        return (
            getattr(client, method)(url, payload, format="json") if payload else getattr(client, method)(url)
        )

    assert call(real).status_code == call(missing).status_code == 404
    assert PurchaseOrder.objects.get(pk=order.pk).number == "P"
    assert not StockMovement.objects.exists()


def test_inventory_is_under_rls(world, other_world):
    Vendor.objects.create(school=other_world.school, name="V")
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Vendor.objects.exists()
