"""Fee plans are office data (school scope only). Obligations, payments and refunds follow their student:
a parent sees their children's, a student their own (people.scoping)."""

from __future__ import annotations

from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import student_rules

from .models import FeePlan, Payment, Refund, StudentFee

plans: ScopedResource[FeePlan] = ScopedResource("fee_plan", FeePlan)
student_fees: ScopedResource[StudentFee] = ScopedResource("student_fee", StudentFee)
payments: ScopedResource[Payment] = ScopedResource("fee_payment", Payment)
refunds: ScopedResource[Refund] = ScopedResource("fee_refund", Refund)

student_rules(student_fees)
student_rules(payments)
student_rules(refunds, "payment__student__")
