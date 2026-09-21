"""
Payment gateway adapters.

EduFlow never holds school money (RBI Payment Aggregator Directions, 2025): each
school is onboarded as its own merchant/sub-merchant and its keys live in
School.settings["payments"]. Fees settle straight into the school's account.
"""

import hashlib
import hmac
import uuid

import httpx
from django.conf import settings
from rest_framework.exceptions import ValidationError


class GatewayNotConfigured(ValidationError):
    default_detail = "Online payment isn't set up for this school yet."
    default_code = "gateway_not_configured"


class MockGateway:
    """Development gateway: every checkout succeeds when the app confirms it."""

    name = "mock"

    def __init__(self, school):
        self.school = school

    def create_order(self, payment) -> dict:
        return {
            "order_id": f"mock_{uuid.uuid4().hex[:16]}",
            "amount_paise": int(payment.amount * 100),
            "currency": "INR",
        }

    def verify(self, payment, payload: dict) -> str | None:
        """Return the gateway payment id if the payment really succeeded."""
        if payload.get("simulate") == "success":
            return f"mockpay_{uuid.uuid4().hex[:16]}"
        return None


class RazorpayGateway:
    """Razorpay Orders + Checkout, using the school's own (sub-)merchant keys."""

    name = "razorpay"
    orders_url = "https://api.razorpay.com/v1/orders"

    def __init__(self, school):
        config = (school.settings.get("payments") or {}).get("razorpay") or {}
        self.key_id = config.get("key_id")
        self.key_secret = config.get("key_secret")
        if not self.key_id or not self.key_secret:
            raise GatewayNotConfigured()
        self.school = school

    def create_order(self, payment) -> dict:
        response = httpx.post(
            self.orders_url,
            auth=(self.key_id, self.key_secret),
            json={
                "amount": int(payment.amount * 100),
                "currency": "INR",
                "receipt": str(payment.id),
                "notes": {"school": self.school.code, "invoice": str(payment.invoice_id)},
            },
            timeout=10,
        )
        response.raise_for_status()
        order = response.json()
        return {"order_id": order["id"], "amount_paise": order["amount"], "currency": "INR", "key_id": self.key_id}

    def verify(self, payment, payload: dict) -> str | None:
        """Checkout returns order_id|payment_id signed with the key secret (HMAC-SHA256)."""
        payment_id = payload.get("razorpay_payment_id", "")
        signature = payload.get("razorpay_signature", "")
        message = f"{payment.gateway_order_id}|{payment_id}".encode()
        expected = hmac.new(self.key_secret.encode(), message, hashlib.sha256).hexdigest()
        return payment_id if payment_id and hmac.compare_digest(expected, signature) else None


GATEWAYS = {"mock": MockGateway, "razorpay": RazorpayGateway}


def gateway_for(school):
    name = (school.settings.get("payments") or {}).get("gateway") or ("mock" if settings.DEBUG else "")
    if name not in GATEWAYS:
        raise GatewayNotConfigured()
    return GATEWAYS[name](school)
