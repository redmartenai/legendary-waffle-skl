from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.academics.access import student_for_request
from apps.accounts.models import MANAGEMENT_ROLES, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today
from apps.fees.models import FeeInvoice, FeeInvoiceItem, Payment
from apps.results.services import student_results

from .models import CertificateRequest, Document, DocumentDownload, DocumentGrant
from .pdf import invoice_pdf, receipt_pdf, report_card_pdf

FAMILY_ROLES = frozenset({Role.PARENT, Role.STUDENT})
# Certificates a family can ask the office for from the app. A transfer certificate needs the office.
REQUESTABLE = [CertificateRequest.Kind.BONAFIDE, CertificateRequest.Kind.CHARACTER, CertificateRequest.Kind.STUDY]


FAMILY_SUBJECTS = [DocumentGrant.Subject.PARENTS, DocumentGrant.Subject.STUDENTS]


def granted_documents(student, action: str = "view"):
    """Document ids a family may view (or download) through the console's permissions matrix."""
    from apps.transport.models import StudentTransport

    route_id = StudentTransport.objects.filter(student=student, is_active=True).values_list("route_id", flat=True).first()
    scope = Q(class_groups=student.class_group) | Q(class_groups__isnull=True, route__isnull=True)
    if route_id:
        scope |= Q(route_id=route_id)
    grants = DocumentGrant.objects.filter(subject__in=FAMILY_SUBJECTS, **{f"can_{action}": True}).filter(scope)
    return grants.values("document_id")


def visible_documents(student):
    """Documents this student's family may see: by audience, or (once a document has a permissions matrix) by its grants."""
    by_audience = (
        Q(audience__in=[Document.Audience.EVERYONE, Document.Audience.FAMILIES])
        | Q(audience=Document.Audience.CLASSES, class_groups=student.class_group)
        | Q(audience=Document.Audience.STUDENT, student=student)
    )
    return Document.objects.filter((by_audience & Q(grants__isnull=True)) | Q(id__in=granted_documents(student))).distinct()


def document_payload(doc: Document) -> dict:
    return {
        "id": str(doc.id),
        "kind": doc.kind,
        "title": doc.title,
        "subtitle": doc.subtitle,
        "size": doc.size,
        "date": (doc.issued_on or doc.created_at.date()).isoformat(),
        "download": f"/documents/{doc.id}/file",
    }


def _log(request, *, kind: str, ref: str = "", document=None, student=None, action: str = DocumentDownload.Action.DOWNLOAD):
    """Every download (and view) is logged with the device, and written to the audit trail."""
    from apps.accounts.audit import audit

    device = (request.META.get("HTTP_USER_AGENT") or "")[:200]
    DocumentDownload.objects.create(kind=kind, ref=ref, document=document, user=request.user, student=student, action=action, device=device)
    title = document.title if document else kind.replace("_", " ")
    audit(request, f"documents.{action}", target=document or (kind, ref), summary=title, module="documents")


class StudentDocumentsView(SchoolAPIView):
    """A child's documents, grouped the way the Documents screen shows them."""

    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        docs = list(visible_documents(student))
        results = student_results(student)
        report_cards = [
            {
                "id": f"exam-{exam['id']}",
                "kind": "report_card",
                "title": f"{exam['name']} · report card",
                "subtitle": f"{exam['percent']:.0f}% · Grade {exam['grade']}",
                "size": None,
                "date": exam["held_on"],
                "download": f"/students/{student.id}/results/{exam['id']}/report.pdf",
            }
            for exam in results["exams"]
        ] + [document_payload(d) for d in docs if d.kind == Document.Kind.REPORT_CARD]

        payments = Payment.objects.filter(
            invoice__student=student, status=Payment.Status.SUCCEEDED, receipt_no__isnull=False
        ).select_related("invoice")
        receipts = [
            {
                "id": str(p.id),
                "kind": "receipt",
                "title": p.invoice.title,
                "subtitle": f"Receipt {p.receipt_no}",
                "amount": str(p.amount),
                "size": None,
                "date": p.paid_at.date().isoformat() if p.paid_at else None,
                "download": f"/fees/payments/{p.id}/receipt.pdf",
            }
            for p in payments
        ]

        requests = {r.kind: r for r in CertificateRequest.objects.filter(student=student).order_by("created_at")}
        issued = [document_payload(d) for d in docs if d.kind == Document.Kind.CERTIFICATE]
        certificates = []
        for kind in CertificateRequest.Kind:
            req = requests.get(kind)
            state = "available"
            if kind == CertificateRequest.Kind.TRANSFER:
                state = "locked"
            elif req is not None:
                state = req.status
            certificates.append(
                {
                    "kind": kind.value,
                    "title": kind.label,
                    "state": state,
                    "requested_on": req.created_at.date().isoformat() if req else None,
                    "document": document_payload(req.document) if req and req.document else None,
                }
            )

        return Response(
            {
                "report_cards": report_cards,
                "receipts": receipts,
                "circulars": [document_payload(d) for d in docs if d.kind in (Document.Kind.CIRCULAR, Document.Kind.POLICY, Document.Kind.OTHER)],
                "certificates": certificates,
                "issued": issued,
                "total": len(report_cards) + len(receipts) + len(docs),
            }
        )


class CertificateRequestSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=[k.value for k in REQUESTABLE])
    purpose = serializers.CharField(max_length=200, required=False, allow_blank=True)


class CertificateRequestView(SchoolAPIView):
    allowed_roles = FAMILY_ROLES

    def post(self, request, student_id):
        student = student_for_request(request, student_id)
        data = CertificateRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        kind = data.validated_data["kind"]
        if CertificateRequest.objects.filter(student=student, kind=kind, status=CertificateRequest.Status.REQUESTED).exists():
            raise ValidationError({"kind": "You've already asked for this certificate. The office will let you know when it's ready."})
        req = CertificateRequest.objects.create(
            student=student, kind=kind, purpose=data.validated_data.get("purpose", ""), requested_by=request.user
        )
        return Response({"id": str(req.id), "kind": req.kind, "state": req.status}, status=status.HTTP_201_CREATED)


class DocumentFileView(SchoolAPIView):
    """Download a stored document. Every download is logged."""

    permission = ("documents", "download")

    def get(self, request, document_id):
        doc = Document.objects.filter(id=document_id).first()
        if doc is None:
            raise Http404
        roles = request.roles
        if roles & FAMILY_ROLES and not roles - FAMILY_ROLES:
            from apps.academics.access import children_of

            students = list(children_of(request.user)) if Role.PARENT in roles else []
            if Role.STUDENT in roles:
                from apps.academics.models import Student

                students += list(Student.objects.filter(user=request.user))
            if not any(visible_documents(s).filter(id=doc.id).exists() for s in students):
                raise Http404
            # With a permissions matrix, seeing a document isn't the same as downloading it.
            if doc.grants.exists() and not any(Document.objects.filter(id=doc.id, id__in=granted_documents(s, "download")).exists() for s in students):
                raise PermissionDenied("This document is view-only for families.")
        elif doc.audience == Document.Audience.STAFF and not roles - FAMILY_ROLES:
            raise Http404
        if not roles & MANAGEMENT_ROLES:
            # Payslips and a teacher's own certificates: the owner only.
            if doc.audience == Document.Audience.PRIVATE and doc.owner_id != request.user.id:
                raise Http404
            # A question paper with the exam cell stays closed, even to its author, until the lock lifts.
            if doc.locked_until and doc.locked_until > school_today(request.school):
                raise Http404
        viewing = request.query_params.get("view") == "1"
        _log(request, kind="document", ref=str(doc.id), document=doc, student=doc.student, action=DocumentDownload.Action.VIEW if viewing else DocumentDownload.Action.DOWNLOAD)
        return FileResponse(doc.file.open("rb"), as_attachment=not viewing, filename=doc.file.name.rsplit("/", 1)[-1])


class ReportCardPdfView(SchoolAPIView):
    permission = ("exams", "download")

    def get(self, request, student_id, exam_id):
        student = student_for_request(request, student_id)
        results = student_results(student)
        if not any(e["id"] == str(exam_id) for e in results["exams"]):
            raise Http404
        pdf = report_card_pdf(results, str(exam_id))
        _log(request, kind="report_card", ref=str(exam_id), student=student)
        name = f"{student.full_name.replace(' ', '_')}_report_card.pdf"
        return _pdf_response(pdf, name)


class ReceiptPdfView(SchoolAPIView):
    permission = ("fees", "download")

    def get(self, request, payment_id):
        payment = (
            Payment.objects.filter(id=payment_id, status=Payment.Status.SUCCEEDED, receipt_no__isnull=False)
            .select_related("invoice__student__class_group", "paid_by")
            .first()
        )
        if payment is None:
            raise Http404
        student = student_for_request(request, payment.invoice.student_id, purpose="fees")
        items = [(i.head, i.amount) for i in FeeInvoiceItem.objects.filter(invoice=payment.invoice)] or [(payment.invoice.title, payment.amount)]
        pdf = receipt_pdf(
            school_name=request.school.name,
            receipt_no=payment.receipt_no,
            paid_at=payment.paid_at,
            student=student.full_name,
            class_label=student.class_group.short_label,
            title=payment.invoice.title,
            items=items,
            amount=payment.amount,
            method=payment.method or payment.gateway,
            paid_by=payment.paid_by.full_name if payment.paid_by else None,
        )
        _log(request, kind="receipt", ref=str(payment.id), student=student)
        return _pdf_response(pdf, f"Receipt_{payment.receipt_no.replace('/', '-')}.pdf")


class InvoicePdfView(SchoolAPIView):
    permission = ("fees", "download")

    def get(self, request, invoice_id):
        invoice = FeeInvoice.objects.filter(id=invoice_id).select_related("student__class_group").first()
        if invoice is None:
            raise Http404
        student = student_for_request(request, invoice.student_id, purpose="fees")
        items = [(i.head, i.amount) for i in FeeInvoiceItem.objects.filter(invoice=invoice)] or [(invoice.title, invoice.amount)]
        pdf = invoice_pdf(
            school_name=request.school.name,
            title=invoice.title,
            student=student.full_name,
            class_label=student.class_group.short_label,
            due_date=invoice.due_date,
            items=items,
            amount=invoice.amount,
            balance=invoice.balance,
        )
        _log(request, kind="invoice", ref=str(invoice.id), student=student)
        return _pdf_response(pdf, f"{invoice.title.replace(' ', '_')}_{student.first_name}.pdf")


def _pdf_response(pdf: bytes, filename: str) -> HttpResponse:
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
