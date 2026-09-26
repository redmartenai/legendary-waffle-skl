"""Console: reports & analytics page (PReports).

Three charts and a report library that all follow the page's filters (term, grades, sections, dates); scheduled
reports with a delivery log; a custom report builder. Every export is audited. Rows come from ``apps.reports``.
"""

from datetime import time

from django.db.models import Max
from django.http import FileResponse, Http404
from django.urls import path
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.academics.models import ClassGroup
from apps.accounts.audit import audit
from apps.accounts.models import MANAGEMENT_ROLES, Membership, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_now
from apps.principal.views import _grade_key
from apps.reports import library, services
from apps.reports.models import CustomReport, ReportDelivery, ReportRun, ScheduledReport

from .common import CONSOLE_ROLES

RECIPIENT_ROLES = [Role.PRINCIPAL, Role.ADMIN, Role.ACCOUNTANT, Role.TRANSPORT_MANAGER]


def recipient_memberships():
    """Who can receive scheduled reports: the principal, the office desks (admins with a department), accounts
    and the transport desk. General support staff can't."""
    return Membership.objects.filter(role__in=RECIPIENT_ROLES, is_active=True).exclude(role=Role.ADMIN, department="")


def _run_payload(run: ReportRun | None) -> dict | None:
    if run is None:
        return None
    return {"id": str(run.id), "at": run.created_at.isoformat(), "format": run.format, "rows": run.rows, "size": run.size, "title": run.title, "file": f"/console/reports/runs/{run.id}/file"}


def _latest_runs() -> dict:
    latest = ReportRun.objects.values("report").annotate(at=Max("created_at"))
    ids = {}
    for row in latest:
        run = ReportRun.objects.filter(report=row["report"], created_at=row["at"]).first()
        if run:
            ids[row["report"]] = run
    return ids


def _people(users) -> list[dict]:
    titles = {}
    for m in Membership.objects.filter(user__in=users).order_by("role"):
        titles.setdefault(m.user_id, m.title or m.get_role_display())
    first = set(Membership.objects.filter(user__in=users, role=Role.PRINCIPAL).values_list("user_id", flat=True))
    users = sorted(users, key=lambda u: (u.id not in first, u.full_name))
    return [{"id": str(u.id), "name": u.full_name, "title": titles.get(u.id, "")} for u in users]


def _schedule_payload(s: ScheduledReport) -> dict:
    return {
        "id": str(s.id),
        "name": s.name,
        "report": s.report,
        "format": s.format,
        "frequency": s.frequency,
        "weekday": s.weekday,
        "day_of_month": s.day_of_month,
        "at": s.at.strftime("%H:%M"),
        "recipients": _people(list(s.recipients.all())),
        "enabled": s.enabled,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at and s.enabled else None,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
    }


def _can_export(request) -> bool:
    from apps.accounts.permissions import has_permission

    return has_permission(request, "reports", "export")


class ReportsView(SchoolAPIView):
    """Charts, schedules and the library for the page's filters."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        f = services.filters_from(request.school, request.query_params)
        runs = _latest_runs()
        groups = sorted(ClassGroup.objects.all(), key=lambda g: (_grade_key(g.grade), g.section))
        recipients = list(
            {m.user_id: m.user for m in recipient_memberships().select_related("user").order_by("user__full_name")}.values()
        )
        customs = list(CustomReport.objects.all())
        return Response(
            {
                "filters": {**f.as_dict(), "describe": f.describe()},
                "options": {
                    "terms": [t["name"] for t in services.terms(request.school)],
                    "grades": sorted({g.grade for g in groups}, key=_grade_key),
                    "sections": [{"id": str(g.id), "label": g.short_label, "grade": g.grade} for g in groups],
                },
                "charts": services.analytics(request.school, f),
                "schedules": [_schedule_payload(s) for s in ScheduledReport.objects.prefetch_related("recipients")],
                "library": [{"key": key, "formats": spec["formats"], "last": _run_payload(runs.get(key))} for key, spec in library.LIBRARY.items()],
                "custom": [
                    {"key": f"custom:{c.id}", "id": str(c.id), "name": c.name, "module": c.module, "columns": c.columns, "formats": ["pdf", "xlsx"], "last": _run_payload(runs.get(f"custom:{c.id}"))}
                    for c in customs
                ],
                "custom_modules": {m: list(cols.items()) for m, cols in library.CUSTOM_MODULES.items()},
                "recipients": _people(recipients),
                "can_export": _can_export(request),
                "refreshed_at": school_now(request.school).isoformat(),
            }
        )


class GenerateView(SchoolAPIView):
    """Generate a library (or custom) report, or the page summary, with the page's filters. Audited."""

    allowed_roles = CONSOLE_ROLES
    permission = ("reports", "export")

    def post(self, request):
        report = request.data.get("report") or ""
        fmt = request.data.get("format") or "pdf"
        if report not in library.LIBRARY and report != "summary" and not report.startswith("custom:"):
            raise ValidationError({"report": "Unknown report."})
        if fmt not in ("pdf", "xlsx"):
            raise ValidationError({"format": "PDF or XLSX."})
        f = services.filters_from(request.school, request.data)
        try:
            run = services.generate(request.school, report, fmt, f, user=request.user)
        except KeyError as exc:
            raise Http404 from exc
        except ValueError as exc:
            raise ValidationError({"format": str(exc)}) from exc
        audit(request, "reports.export", target=run, summary=f"{run.title} · {fmt.upper()} · {run.rows} rows", detail={"report": report, "filters": f.as_dict()})
        return Response(_run_payload(run), status=status.HTTP_201_CREATED)


class RunFileView(SchoolAPIView):
    """Download a generated report: management, whoever generated it, or a recipient of the schedule. Audited."""

    def get(self, request, run_id):
        run = ReportRun.objects.filter(id=run_id).first()
        if run is None or not run.file:
            raise Http404
        allowed = bool(request.roles & MANAGEMENT_ROLES) or run.generated_by_id == request.user.id or ReportDelivery.objects.filter(run=run, user=request.user).exists()
        if not allowed:
            raise Http404
        audit(request, "reports.download", target=run, summary=f"{run.title} · {run.format.upper()}")
        response = FileResponse(run.file.open("rb"), as_attachment=True, filename=run.file.name.rsplit("/", 1)[-1], content_type=services.CONTENT_TYPES.get(run.format))
        return response


class ScheduleSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    report = serializers.CharField(max_length=40)
    format = serializers.ChoiceField(choices=["pdf", "xlsx"])
    frequency = serializers.ChoiceField(choices=ScheduledReport.Frequency.choices)
    weekday = serializers.IntegerField(min_value=0, max_value=6, default=0)
    day_of_month = serializers.IntegerField(min_value=1, max_value=28, default=1)
    at = serializers.TimeField(default=time(7, 0))
    recipient_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)
    enabled = serializers.BooleanField(default=True)


def _check_report(report: str, fmt: str):
    if report.startswith("custom:"):
        if not CustomReport.objects.filter(id=report.split(":", 1)[1]).exists():
            raise ValidationError({"report": "Unknown report."})
        return
    spec = library.LIBRARY.get(report)
    if spec is None:
        raise ValidationError({"report": "Unknown report."})
    if fmt not in spec["formats"]:
        raise ValidationError({"format": f"This report comes as {' or '.join(x.upper() for x in spec['formats'])}."})


class SchedulesView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES
    permission = ("reports", "export")

    def post(self, request):
        data = ScheduleSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        _check_report(v["report"], v["format"])
        users = list(recipient_memberships().filter(user_id__in=v["recipient_ids"]).values_list("user_id", flat=True).distinct())
        if len(set(users)) != len(set(v["recipient_ids"])):
            raise ValidationError({"recipient_ids": "Reports go to the principal's office, accounts or the transport desk."})
        s = ScheduledReport(
            name=v["name"], report=v["report"], format=v["format"], frequency=v["frequency"], weekday=v["weekday"],
            day_of_month=v["day_of_month"], at=v["at"], enabled=v["enabled"], created_by=request.user,
        )
        s.school = request.school
        s.next_run_at = services.next_run(s, school_now(request.school)) if s.enabled else None
        s.save()
        s.recipients.set(users)
        audit(request, "reports.schedule", target=s, summary=f"{s.name} · {s.get_frequency_display()} · {len(users)} recipients")
        return Response(_schedule_payload(s), status=status.HTTP_201_CREATED)


class ScheduleView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def _get(self, schedule_id) -> ScheduledReport:
        s = ScheduledReport.objects.filter(id=schedule_id).first()
        if s is None:
            raise Http404
        return s

    def patch(self, request, schedule_id):
        s = self._get(schedule_id)
        if "enabled" in request.data:
            s.enabled = bool(request.data["enabled"])
            s.next_run_at = services.next_run(s, school_now(request.school)) if s.enabled else None
            s.save(update_fields=["enabled", "next_run_at", "updated_at"])
            audit(request, "reports.schedule_resume" if s.enabled else "reports.schedule_pause", target=s, summary=s.name)
        return Response(_schedule_payload(s))

    def delete(self, request, schedule_id):
        s = self._get(schedule_id)
        audit(request, "reports.schedule_delete", target=s, summary=s.name)
        s.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ScheduleRunView(SchoolAPIView):
    """Run a schedule now (outside its time): generated and delivered like a scheduled run."""

    allowed_roles = CONSOLE_ROLES
    permission = ("reports", "export")

    def post(self, request, schedule_id):
        s = ScheduledReport.objects.filter(id=schedule_id).first()
        if s is None:
            raise Http404
        next_at = s.next_run_at
        run = services.run_schedule(s)
        if next_at and s.enabled:
            # A manual run doesn't move the regular schedule.
            ScheduledReport.objects.filter(pk=s.pk).update(next_run_at=next_at)
        audit(request, "reports.export", target=run, summary=f"{s.name} (run now) · {run.format.upper()}")
        return Response(_run_payload(run), status=status.HTTP_201_CREATED)


class DeliveriesView(SchoolAPIView):
    """The delivery log: every scheduled run handed to someone, on each channel."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        rows = ReportDelivery.objects.select_related("schedule", "run", "user").order_by("-created_at")[:300]
        return Response(
            {
                "items": [
                    {
                        "id": str(d.id),
                        "at": d.created_at.isoformat(),
                        "schedule": d.schedule.name,
                        "report": d.run.title,
                        "format": d.run.format,
                        "user": d.user.full_name if d.user else None,
                        "channel": d.channel,
                        "address": d.address,
                        "status": d.status,
                        "file": f"/console/reports/runs/{d.run_id}/file",
                    }
                    for d in rows
                ]
            }
        )


class CustomSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    module = serializers.ChoiceField(choices=list(library.CUSTOM_MODULES))
    columns = serializers.ListField(child=serializers.CharField(max_length=30), allow_empty=False)


class CustomReportsView(SchoolAPIView):
    """Save a custom report (a module and its columns) to the library. Needs the Export permission."""

    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        if not _can_export(request):
            raise PermissionDenied("Building reports needs the Export permission.")
        data = CustomSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        catalog = library.CUSTOM_MODULES[v["module"]]
        bad = [c for c in v["columns"] if c not in catalog]
        if bad:
            raise ValidationError({"columns": f"Unknown column: {', '.join(bad)}."})
        c = CustomReport.objects.create(name=v["name"].strip(), module=v["module"], columns=v["columns"], created_by=request.user)
        audit(request, "reports.custom_create", target=c, summary=f"{c.name} · {c.module} · {len(c.columns)} columns")
        return Response({"key": f"custom:{c.id}", "id": str(c.id), "name": c.name, "module": c.module, "columns": c.columns, "formats": ["pdf", "xlsx"], "last": None}, status=status.HTTP_201_CREATED)


class CustomReportView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def delete(self, request, custom_id):
        c = CustomReport.objects.filter(id=custom_id).first()
        if c is None:
            raise Http404
        ScheduledReport.objects.filter(report=f"custom:{c.id}").delete()
        audit(request, "reports.custom_delete", target=c, summary=c.name)
        c.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


urlpatterns = [
    path("console/reports", ReportsView.as_view()),
    path("console/reports/generate", GenerateView.as_view()),
    path("console/reports/runs/<uuid:run_id>/file", RunFileView.as_view()),
    path("console/reports/schedules", SchedulesView.as_view()),
    path("console/reports/schedules/<uuid:schedule_id>", ScheduleView.as_view()),
    path("console/reports/schedules/<uuid:schedule_id>/run", ScheduleRunView.as_view()),
    path("console/reports/deliveries", DeliveriesView.as_view()),
    path("console/reports/custom", CustomReportsView.as_view()),
    path("console/reports/custom/<uuid:custom_id>", CustomReportView.as_view()),
]
