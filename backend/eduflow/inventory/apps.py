from django.apps import AppConfig


class InventoryConfig(AppConfig):
    """Stock, assets, vendors and procurement (requisitions, purchase orders, receipts, invoices)."""

    name = "eduflow.inventory"
    label = "inventory"

    def ready(self) -> None:
        from . import approvals, policies  # noqa: F401  (registers scope rules and approval providers)

        approvals.register()
