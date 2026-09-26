"""What each kind of request shows the principal, and what approving (or undoing) it changes."""

import re
from datetime import timedelta
from decimal import Decimal

from django.db.models import Avg
from django.utils import timezone

from apps.notifications.models import Category
from apps.notifications.services import notify


def _person(user) -> dict | None:
    return {"id": str(user.id), "name": user.full_name, "initials": user.initials} if user else None


def _question(note: str) -> dict:
    """A marker's note like "Q7 · 0 → 4" names the question and its old and new marks."""
    m = re.match(r"\s*(Q\d+[a-z]?)\s*·\s*(\d+(?:\.\d+)?)\s*→\s*(\d+(?:\.\d+)?)", note or "")
    if m:
        return {"question": m.group(1), "question_from": float(m.group(2)), "question_to": float(m.group(3))}
    m = re.match(r"\s*(Q\d+[a-z]?)\b", note or "")
    return {"question": m.group(1) if m else None, "question_from": None, "question_to": None}


class Handler:
    kind = ""

    def details(self, req, target) -> dict:
        return {}

    def apply(self, req, target, actor) -> None:
        pass

    def decline(self, req, target, actor) -> None:
        pass

    def revert(self, req, target, actor) -> None:
        pass


class LeaveHandler(Handler):
    kind = "leave"

    def details(self, req, leave) -> dict:
        from apps.academics.models import TeachingAssignment, TimetableSlot
        from apps.staff.services import leave_balances

        subject = TeachingAssignment.objects.filter(teacher=leave.user).select_related("subject").first()
        periods = 0
        day = leave.from_date
        while day <= leave.to_date:
            if day.weekday() != 6:
                periods += TimetableSlot.objects.filter(teacher=leave.user, weekday=day.weekday()).count()
            day += timedelta(days=1)
        if leave.half_day:
            periods = (periods + 1) // 2
        balance = next(b for b in leave_balances(leave.user, leave.school) if b["kind"] == leave.kind)
        counted = leave.status == "approved"
        return {
            "person": {**_person(leave.user), "subject": subject.subject.name if subject else None},
            "from_date": leave.from_date.isoformat(),
            "to_date": leave.to_date.isoformat(),
            "days": float(leave.days),
            "half_day": leave.half_day,
            "leave_kind": leave.kind,
            "reason": leave.reason,
            "cover_periods": periods,
            "allowed": balance["allowed"],
            "left_after": balance["left"] - (0 if counted else float(leave.days)),
            "certificate": {"name": leave.certificate_name, "url": f"/approvals/{req.id}/file"} if leave.certificate else None,
        }

    def _set(self, leave, status, actor):
        leave.status = status
        leave.decided_by = actor if status != "pending" else None
        leave.decided_at = timezone.now() if status != "pending" else None
        leave.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])

    def apply(self, req, leave, actor):
        self._set(leave, "approved", actor)
        notify([leave.user], school=req.school, category=Category.GENERAL, title="Leave approved", body=f"{leave.from_date:%a %d %b} · {leave.days:g} day(s). Cover is being arranged.", data={"type": "staff_leave", "leave_id": str(leave.id)})

    def decline(self, req, leave, actor):
        self._set(leave, "declined", actor)
        leave.decision_note = req.decision_note
        leave.save(update_fields=["decision_note"])
        notify([leave.user], school=req.school, category=Category.GENERAL, title="Leave not approved", body=req.decision_note or f"{leave.from_date:%a %d %b}", data={"type": "staff_leave", "leave_id": str(leave.id)})

    def revert(self, req, leave, actor):
        self._set(leave, "pending", actor)


class MarksHandler(Handler):
    kind = "marks"

    def details(self, req, corr) -> dict:
        from apps.academics.models import Student
        from apps.results.models import ExamMark

        students = {str(s.id): s for s in Student.objects.filter(id__in=[e["student_id"] for e in corr.entries])}
        roster = Student.objects.filter(class_group=corr.exam.class_group, is_active=True)
        marks = ExamMark.objects.filter(exam=corr.exam, subject=corr.subject, is_absent=False)
        avg_now = marks.aggregate(a=Avg("marks"), m=Avg("max_marks"))
        out_of = float(avg_now["m"] or 100)
        count = marks.count() or 1
        delta = sum(Decimal(str(e["to"])) - Decimal(str(e["from"])) for e in corr.entries)
        applied = corr.applied_at is not None
        before = float(avg_now["a"] or 0) - (float(delta) / count if applied else 0)
        after = before + float(delta) / count
        return {
            "exam": corr.exam.name,
            "subject": corr.subject.name,
            "class": corr.exam.class_group.short_label,
            "out_of": out_of,
            "entries": [
                {
                    "student": students[e["student_id"]].full_name if e["student_id"] in students else "",
                    "roll_no": students[e["student_id"]].roll_no if e["student_id"] in students else None,
                    "from": e["from"],
                    "to": e["to"],
                    "note": e.get("note", ""),
                    **_question(e.get("note", "")),
                }
                for e in corr.entries
            ],
            "reason": corr.reason,
            "checked_by": corr.checked_by.full_name if corr.checked_by else None,
            "published_on": corr.exam.published_on.isoformat() if corr.exam.published_on else None,
            "class_size": roster.count(),
            "last_roll": max((r for r in roster.values_list("roll_no", flat=True) if r), default=None),
            "applied": applied,
            "average_before": round(before * 100 / out_of, 1),
            "average_after": round(after * 100 / out_of, 1),
        }

    def _write(self, corr, key):
        from apps.results.models import ExamMark

        for e in corr.entries:
            ExamMark.objects.filter(exam=corr.exam, subject=corr.subject, student_id=e["student_id"]).update(marks=Decimal(str(e[key])))

    def apply(self, req, corr, actor):
        from apps.academics.access import guardians_of
        from apps.academics.models import Student

        self._write(corr, "to")
        corr.applied_at = timezone.now()
        corr.save(update_fields=["applied_at", "updated_at"])
        students = list(Student.objects.filter(id__in=[e["student_id"] for e in corr.entries]))
        notify(list(guardians_of(students).keys()), school=req.school, category=Category.RESULTS, title=f"{corr.exam.name} {corr.subject.name}: marks updated", body="A marking error was corrected. The report card is updated.", data={"type": "results"})
        if req.requested_by:
            notify([req.requested_by], school=req.school, category=Category.RESULTS, title="Marks correction approved", body=f"{corr.exam.class_group.short_label} {corr.subject.name} · {len(corr.entries)} change(s)", data={"type": "marks_correction"})

    def decline(self, req, corr, actor):
        if req.requested_by:
            notify([req.requested_by], school=req.school, category=Category.RESULTS, title="Marks correction sent back", body=req.decision_note or "See the principal's note.", data={"type": "marks_correction"})

    def revert(self, req, corr, actor):
        if corr.applied_at:
            self._write(corr, "from")
            corr.applied_at = None
            corr.save(update_fields=["applied_at", "updated_at"])


class AttendanceHandler(Handler):
    kind = "attendance"

    def details(self, req, corr) -> dict:
        from apps.academics.models import Student

        students = {str(s.id): s for s in Student.objects.filter(id__in=[e["student_id"] for e in corr.entries])}
        return {
            "class": corr.session.class_group.short_label,
            "date": corr.session.date.isoformat(),
            "reason": corr.reason,
            "number": corr.number,
            "entries": [
                {
                    "student": students[e["student_id"]].full_name if e["student_id"] in students else "",
                    "roll_no": students[e["student_id"]].roll_no if e["student_id"] in students else None,
                    "from": e["from"],
                    "to": e["to"],
                }
                for e in corr.entries
            ],
        }

    def _write(self, corr, key):
        from apps.attendance.models import AttendanceException

        for e in corr.entries:
            status = e[key]
            if status == "present":
                AttendanceException.objects.filter(session=corr.session, student_id=e["student_id"]).delete()
            else:
                AttendanceException.objects.update_or_create(session=corr.session, student_id=e["student_id"], defaults={"status": status})

    def apply(self, req, corr, actor):
        self._write(corr, "to")
        corr.applied_at = timezone.now()
        corr.save(update_fields=["applied_at", "updated_at"])
        if req.requested_by:
            notify([req.requested_by], school=req.school, category=Category.ATTENDANCE, title="Register change approved", body=f"{corr.session.class_group.short_label} · {corr.session.date:%a %d %b}", data={"type": "attendance_correction"})

    def decline(self, req, corr, actor):
        if req.requested_by:
            notify([req.requested_by], school=req.school, category=Category.ATTENDANCE, title="Register change not approved", body=req.decision_note or f"{corr.session.class_group.short_label}", data={"type": "attendance_correction"})

    def revert(self, req, corr, actor):
        if corr.applied_at:
            self._write(corr, "from")
            corr.applied_at = None
            corr.save(update_fields=["applied_at", "updated_at"])


class RefundHandler(Handler):
    kind = "refund"

    def details(self, req, refund) -> dict:
        from apps.academics.models import StudentGuardian

        payment = refund.payment
        student = payment.invoice.student
        link = StudentGuardian.objects.filter(student=student, user=refund.asked_by).first() if refund.asked_by else None
        return {
            "student": {"name": student.full_name, "initials": student.initials, "class": student.class_group.short_label},
            "asked_by": refund.asked_by.full_name if refund.asked_by else None,
            "relationship": link.relationship if link else None,
            "amount": str(refund.amount),
            "fee_head": refund.fee_head or payment.invoice.title,
            "paid_on": payment.paid_at.date().isoformat() if payment.paid_at else None,
            "method": payment.method,
            "receipt_no": payment.receipt_no,
            "reason": refund.reason,
            "status": refund.status,
            # Every payment against the same bill, so a double charge is plain to see.
            "invoice": {
                "title": payment.invoice.title,
                "amount": str(payment.invoice.amount),
                "payments": [
                    {
                        "id": str(p.id),
                        "amount": str(p.amount),
                        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                        "method": p.method,
                        "receipt_no": p.receipt_no,
                        "refunded": p.id == payment.id,
                    }
                    for p in payment.invoice.payments.filter(status="succeeded").order_by("paid_at")
                ],
            },
        }

    def apply(self, req, refund, actor):
        from apps.accounts.models import Department, Membership

        refund.status = "approved"
        refund.save(update_fields=["status", "updated_at"])
        accounts = [m.user for m in Membership.objects.filter(department=Department.ACCOUNTS, is_active=True).select_related("user")]
        notify(accounts, school=req.school, category=Category.FEES, title=f"Refund approved · ₹{refund.amount:,.0f}", body=f"{refund.payment.invoice.student.full_name} · {refund.fee_head}. Return it to the original payment method.", data={"type": "refund", "refund_id": str(refund.id)})
        if refund.asked_by:
            notify([refund.asked_by], school=req.school, category=Category.FEES, title="Refund approved", body=f"₹{refund.amount:,.0f} will go back to your original payment method.", data={"type": "refund"})

    def decline(self, req, refund, actor):
        refund.status = "declined"
        refund.save(update_fields=["status", "updated_at"])
        if refund.asked_by:
            notify([refund.asked_by], school=req.school, category=Category.FEES, title="Refund not approved", body=req.decision_note or "The school office will explain.", data={"type": "refund"})

    def revert(self, req, refund, actor):
        refund.status = "requested"
        refund.save(update_fields=["status", "updated_at"])


class AdmissionHandler(Handler):
    kind = "admission"

    def details(self, req, app) -> dict:
        from apps.admissions.models import Application

        seats = ((req.school.settings or {}).get("admissions") or {}).get("seats", {}).get(app.grade)
        taken = Application.objects.filter(grade=app.grade, academic_year=app.academic_year, status="offered").count()
        return {
            "child": app.child_name,
            "grade": app.grade,
            "academic_year": app.academic_year,
            "application_no": app.application_no,
            "documents_verified": app.documents_verified,
            "interaction_on": app.interaction_on.isoformat() if app.interaction_on else None,
            "sibling": {"name": app.sibling.full_name, "class": app.sibling.class_group.short_label} if app.sibling else None,
            "seats_left": max(0, seats - taken) if seats is not None else None,
            "seats": seats,
            "documents_pending": app.documents_pending,
            "guardian_name": app.guardian_name,
            "assessment_score": app.assessment_score,
            "assessment_out_of": app.assessment_out_of,
            "status": app.status,
        }

    def apply(self, req, app, actor):
        app.status = "offered"
        app.save(update_fields=["status", "updated_at"])

    def decline(self, req, app, actor):
        app.status = "declined"
        app.save(update_fields=["status", "updated_at"])

    def revert(self, req, app, actor):
        app.status = "applied"
        app.save(update_fields=["status", "updated_at"])


HANDLERS = {h.kind: h for h in (LeaveHandler(), MarksHandler(), AttendanceHandler(), RefundHandler(), AdmissionHandler())}
