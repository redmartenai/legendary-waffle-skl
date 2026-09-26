from django.http import FileResponse, Http404
from rest_framework.response import Response

from apps.accounts.models import MANAGEMENT_ROLES
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today

from . import services
from .models import ApprovalRequest

KIND_ORDER = ["leave", "marks", "refund", "admission", "attendance"]
URGENT_DAYS = 7


def urgency(today):
    """Sort key: anything due within a week first (soonest first), then the rest oldest first."""

    def key(r):
        if r.due_on and (r.due_on - today).days <= URGENT_DAYS:
            return (0, r.due_on.toordinal(), r.created_at.timestamp())
        return (1, 0, r.created_at.timestamp())

    return key


def _device(request) -> str:
    return request.META.get("HTTP_USER_AGENT", "")[:200]


def _get(request_id) -> ApprovalRequest:
    req = ApprovalRequest.objects.filter(id=request_id).select_related("requested_by", "decided_by").first()
    if req is None:
        raise Http404
    return req


class ApprovalListView(SchoolAPIView):
    """The in-tray: pending requests, soonest first, with counts per kind."""

    allowed_roles = MANAGEMENT_ROLES

    def get(self, request):
        pending = list(ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING).select_related("requested_by"))
        counts = {k: sum(1 for r in pending if r.kind == k) for k in KIND_ORDER}
        kind = request.query_params.get("kind")
        items = [r for r in pending if not kind or r.kind == kind]
        items.sort(key=urgency(school_today(request.school)))
        return Response({"total": len(pending), "counts": counts, "items": [services.payload(r) for r in items]})


class ApprovalHistoryView(SchoolAPIView):
    allowed_roles = MANAGEMENT_ROLES

    def get(self, request):
        done = ApprovalRequest.objects.exclude(status=ApprovalRequest.Status.PENDING).select_related("requested_by", "decided_by").order_by("-decided_at", "-updated_at")[:40]
        return Response({"items": [services.payload(r) for r in done]})


class ApprovalDecideView(SchoolAPIView):
    allowed_roles = MANAGEMENT_ROLES

    def post(self, request, request_id):
        req = services.decide(_get(request_id), str(request.data.get("decision", "")), request.user, note=str(request.data.get("note", "")), device=_device(request))
        return Response(services.payload(req))


class ApprovalUndoView(SchoolAPIView):
    allowed_roles = MANAGEMENT_ROLES

    def post(self, request, request_id):
        return Response(services.payload(services.undo(_get(request_id), request.user, device=_device(request))))


class ApprovalFileView(SchoolAPIView):
    """The document attached to a request (a medical certificate)."""

    allowed_roles = MANAGEMENT_ROLES

    def get(self, request, request_id):
        req = _get(request_id)
        f = getattr(req.target, "certificate", None)
        if not f:
            raise Http404
        return FileResponse(f.open("rb"), as_attachment=False, filename=req.target.certificate_name or "certificate")
