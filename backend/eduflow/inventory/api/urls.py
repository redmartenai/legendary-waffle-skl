from django.urls import path

from . import views

urlpatterns = [
    path("inventory/items", views.ItemList.as_view(), name="stock-item-list"),
    path("inventory/items/<uuid:pk>", views.ItemDetail.as_view(), name="stock-item-detail"),
    path("inventory/movements", views.MovementList.as_view(), name="stock-movement-list"),
    path("inventory/assets", views.AssetList.as_view(), name="asset-list"),
    path("inventory/assets/<uuid:pk>", views.AssetDetail.as_view(), name="asset-detail"),
    path("procurement/vendors", views.VendorList.as_view(), name="vendor-list"),
    path("procurement/vendors/<uuid:pk>", views.VendorDetail.as_view(), name="vendor-detail"),
    path("procurement/requisitions", views.RequisitionList.as_view(), name="requisition-list"),
    path("procurement/requisitions/<uuid:pk>", views.RequisitionDetail.as_view(), name="requisition-detail"),
    path("procurement/orders", views.OrderList.as_view(), name="purchase-order-list"),
    path("procurement/orders/<uuid:pk>", views.OrderDetail.as_view(), name="purchase-order-detail"),
    path(
        "procurement/orders/<uuid:pk>/status", views.OrderStatusView.as_view(), name="purchase-order-status"
    ),
    path(
        "procurement/orders/<uuid:pk>/receive",
        views.OrderReceiveView.as_view(),
        name="purchase-order-receive",
    ),
    path(
        "procurement/orders/<uuid:pk>/invoices",
        views.OrderInvoicesView.as_view(),
        name="purchase-order-invoices",
    ),
    path("procurement/invoices", views.InvoiceList.as_view(), name="vendor-invoice-list"),
    path("procurement/invoices/<uuid:pk>/pay", views.InvoicePayView.as_view(), name="vendor-invoice-pay"),
]
