"""Shared pieces for the web console: the top bar's context and the global search."""

from datetime import date

from django.db.models import Q
from rest_framework.response import Response

from apps.academics.models import ClassGroup, Student
from apps.accounts.models import MANAGEMENT_ROLES, Membership, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_now, school_today

CONSOLE_ROLES = MANAGEMENT_ROLES


def current_term(school, day: date) -> dict | None:
    """The term that contains ``day`` from ``school.settings.terms``, with its week number."""
    terms = (school.settings or {}).get("terms") or []
    for term in terms:
        if term.get("starts_on", "") <= day.isoformat() <= term.get("ends_on", ""):
            starts = date.fromisoformat(term["starts_on"])
            return {**term, "week": (day - starts).days // 7 + 1}
    return None


def next_term(school, day: date) -> dict | None:
    terms = sorted((school.settings or {}).get("terms") or [], key=lambda t: t.get("starts_on", ""))
    return next((t for t in terms if t.get("starts_on", "") > day.isoformat()), None)


class ContextView(SchoolAPIView):
    """What the console's sidebar and top bar show on every page."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        from apps.academics.models import AcademicYear
        from apps.approvals.models import ApprovalRequest
        from apps.notifications.models import Notification

        today = school_today(request.school)
        year = AcademicYear.objects.filter(is_current=True).first()
        return Response(
            {
                "now": school_now(request.school).isoformat(),
                "today": today.isoformat(),
                "term": current_term(request.school, today),
                "academic_year": year.name if year else None,
                "campus": (request.school.settings or {}).get("campus"),
                "approvals": ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING).count(),
                "unread_notifications": Notification.objects.filter(school=request.school, user=request.user, read_at__isnull=True).count(),
            }
        )


class SearchView(SchoolAPIView):
    """⌘K search across students, staff, receipts and classes."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        from apps.fees.models import Payment

        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"items": []})
        items = []
        for s in Student.objects.filter(Q(full_name__icontains=q) | Q(admission_no__icontains=q), is_active=True).select_related("class_group")[:6]:
            items.append({"kind": "student", "id": str(s.id), "title": s.full_name, "detail": f"{s.class_group.short_label} · {s.admission_no}"})
        staff = (
            Membership.objects.filter(user__full_name__icontains=q, is_active=True)
            .exclude(role__in=[Role.PARENT, Role.STUDENT])
            .select_related("user")
            .order_by("user__full_name")
        )
        seen = set()
        for m in staff:
            if m.user_id in seen or len(seen) >= 4:
                continue
            seen.add(m.user_id)
            items.append({"kind": "staff", "id": str(m.user_id), "title": m.user.full_name, "detail": m.title or m.get_role_display()})
        for p in Payment.objects.filter(receipt_no__icontains=q, status="succeeded").select_related("invoice__student")[:4]:
            items.append({"kind": "receipt", "id": str(p.id), "title": p.receipt_no, "detail": f"{p.invoice.student.full_name} · ₹{p.amount:,.0f}"})
        for g in ClassGroup.objects.all():
            if q.lower().replace(" ", "") in g.short_label.lower().replace(" ", "") and len([i for i in items if i["kind"] == "class"]) < 4:
                items.append({"kind": "class", "id": str(g.id), "title": g.short_label, "detail": g.label})
        return Response({"items": items})
