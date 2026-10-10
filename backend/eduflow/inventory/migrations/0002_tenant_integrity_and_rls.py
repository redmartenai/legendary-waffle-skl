"""School-consistency foreign keys and RLS for inventory (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['inventory_item', 'inventory_stock_movement', 'inventory_asset', 'inventory_vendor', 'inventory_requisition', 'inventory_purchase_order', 'inventory_order_line', 'inventory_goods_receipt', 'inventory_receipt_line', 'inventory_vendor_invoice']

FKS = [
    ('inventory_stock_movement_item_school_fk', 'inventory_stock_movement', ('item_id', 'school_id'), 'inventory_item', ('id', 'school_id')),
    ('inventory_stock_movement_goods_receipt_line_school_fk', 'inventory_stock_movement', ('goods_receipt_line_id', 'school_id'), 'inventory_receipt_line', ('id', 'school_id')),
    ('inventory_stock_movement_recorded_by_school_fk', 'inventory_stock_movement', ('recorded_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('inventory_asset_custodian_school_fk', 'inventory_asset', ('custodian_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('inventory_asset_vendor_school_fk', 'inventory_asset', ('vendor_id', 'school_id'), 'inventory_vendor', ('id', 'school_id')),
    ('inventory_requisition_requested_by_school_fk', 'inventory_requisition', ('requested_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('inventory_requisition_decided_by_school_fk', 'inventory_requisition', ('decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('inventory_purchase_order_vendor_school_fk', 'inventory_purchase_order', ('vendor_id', 'school_id'), 'inventory_vendor', ('id', 'school_id')),
    ('inventory_purchase_order_requisition_school_fk', 'inventory_purchase_order', ('requisition_id', 'school_id'), 'inventory_requisition', ('id', 'school_id')),
    ('inventory_purchase_order_created_by_school_fk', 'inventory_purchase_order', ('created_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('inventory_order_line_order_school_fk', 'inventory_order_line', ('order_id', 'school_id'), 'inventory_purchase_order', ('id', 'school_id')),
    ('inventory_order_line_item_school_fk', 'inventory_order_line', ('item_id', 'school_id'), 'inventory_item', ('id', 'school_id')),
    ('inventory_goods_receipt_order_school_fk', 'inventory_goods_receipt', ('order_id', 'school_id'), 'inventory_purchase_order', ('id', 'school_id')),
    ('inventory_goods_receipt_received_by_school_fk', 'inventory_goods_receipt', ('received_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('inventory_receipt_line_receipt_school_fk', 'inventory_receipt_line', ('receipt_id', 'school_id'), 'inventory_goods_receipt', ('id', 'school_id')),
    ('inventory_receipt_line_line_school_fk', 'inventory_receipt_line', ('line_id', 'school_id'), 'inventory_order_line', ('id', 'school_id')),
    ('inventory_vendor_invoice_order_school_fk', 'inventory_vendor_invoice', ('order_id', 'school_id'), 'inventory_purchase_order', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]
