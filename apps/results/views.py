from rest_framework.response import Response

from apps.academics.access import student_for_request
from apps.core.api import SchoolAPIView

from .services import student_results


class StudentResultsView(SchoolAPIView):
    def get(self, request, student_id):
        return Response(student_results(student_for_request(request, student_id)))


class StudentExamsView(SchoolAPIView):
    """The next exam for a child: date sheet, admit card and the prep checklist."""

    def get(self, request, student_id):
        from apps.core.utils import school_today

        from .models import Exam, PrepItem

        student = student_for_request(request, student_id)
        today = school_today(request.school)
        exam = (
            Exam.objects.filter(class_group=student.class_group, is_published=False, papers__isnull=False)
            .distinct()
            .order_by("held_on")
            .first()
        )
        if exam is None:
            return Response({"exam": None})
        papers = list(exam.papers.select_related("subject"))
        prep = PrepItem.objects.filter(exam=exam, student=student)
        return Response(
            {
                "exam": {
                    "id": str(exam.id),
                    "name": exam.name,
                    "starts_on": papers[0].date.isoformat() if papers else exam.held_on.isoformat(),
                    "ends_on": papers[-1].date.isoformat() if papers else exam.held_on.isoformat(),
                    "report_by": exam.report_by.strftime("%H:%M") if exam.report_by else None,
                    "results_on": exam.results_on.isoformat() if exam.results_on else None,
                    "admit_cards_from": exam.admit_cards_from.isoformat() if exam.admit_cards_from else None,
                    "admit_card_available": bool(exam.admit_cards_from and exam.admit_cards_from <= today),
                    "datesheet": f"/students/{student.id}/exams/{exam.id}/datesheet.pdf",
                    "admit_card": f"/students/{student.id}/exams/{exam.id}/admit-card.pdf",
                    "released_by": student.class_group.class_teacher.full_name if student.class_group.class_teacher else None,
                },
                "papers": [
                    {
                        "id": str(p.id),
                        "subject": {"name": p.subject.name, "code": p.subject.code, "color": p.subject.color},
                        "date": p.date.isoformat(),
                        "starts_at": p.starts_at.strftime("%H:%M"),
                        "ends_at": p.ends_at.strftime("%H:%M"),
                        "room": p.room,
                        "syllabus": p.syllabus,
                    }
                    for p in papers
                ],
                "prep": [
                    {"id": str(i.id), "title": i.title, "due_date": i.due_date.isoformat(), "done_at": i.done_at.isoformat() if i.done_at else None}
                    for i in prep
                ],
                "student": {"name": student.full_name, "class": student.class_group.short_label, "roll_no": student.roll_no},
            }
        )


class PrepItemToggleView(SchoolAPIView):
    """The student ticks an item on their exam-prep checklist."""

    def post(self, request, item_id):
        from django.http import Http404
        from django.utils import timezone

        from apps.accounts.models import Role

        from .models import PrepItem

        item = PrepItem.objects.filter(id=item_id).select_related("student").first()
        if item is None or not ({Role.STUDENT, Role.PARENT} & request.roles):
            raise Http404
        student_for_request(request, item.student_id)
        done = request.data.get("done")
        item.done_at = timezone.now() if done in (True, "true", "1", 1) else None
        item.save(update_fields=["done_at", "updated_at"])
        return Response({"id": str(item.id), "done_at": item.done_at.isoformat() if item.done_at else None})


class DatesheetPdfView(SchoolAPIView):
    def get(self, request, student_id, exam_id):
        from django.http import Http404, HttpResponse

        from apps.documents.pdf import datesheet_pdf

        from .models import Exam

        student = student_for_request(request, student_id)
        exam = Exam.objects.filter(id=exam_id, class_group=student.class_group).first()
        if exam is None:
            raise Http404
        papers = [(p.date, p.subject.name, p.starts_at, p.ends_at, p.room, p.syllabus) for p in exam.papers.select_related("subject")]
        pdf = datesheet_pdf(school_name=request.school.name, exam=exam.name, class_label=student.class_group.short_label, papers=papers, report_by=exam.report_by)
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{exam.name.replace(" ", "_")}_date_sheet.pdf"'
        return response


class AdmitCardPdfView(SchoolAPIView):
    """The admit card PDF, once the school has released admit cards for this exam."""

    def get(self, request, student_id, exam_id):
        from django.http import Http404, HttpResponse

        from apps.core.utils import school_today
        from apps.documents.pdf import admit_card_pdf

        from .models import Exam

        student = student_for_request(request, student_id)
        exam = Exam.objects.filter(id=exam_id, class_group=student.class_group).first()
        if exam is None or not exam.admit_cards_from or exam.admit_cards_from > school_today(request.school):
            raise Http404
        papers = [(p.date, p.subject.name, p.starts_at, p.ends_at, p.room, p.syllabus) for p in exam.papers.select_related("subject")]
        pdf = admit_card_pdf(
            school_name=request.school.name, exam=exam.name, student=student.full_name, class_label=student.class_group.short_label,
            roll_no=student.roll_no, admission_no=student.admission_no, papers=papers, report_by=exam.report_by,
        )
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{exam.name.replace(" ", "_")}_admit_card.pdf"'
        return response
