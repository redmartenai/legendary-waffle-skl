from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import Book, Copy, CopyStatus, Loan


class LibrarySettingsOut(serializers.Serializer[Any]):
    loan_days = serializers.IntegerField(allow_null=True)
    fine_per_day = serializers.DecimalField(max_digits=8, decimal_places=2, allow_null=True)
    max_loans = serializers.IntegerField(allow_null=True)


class LibrarySettingsIn(StrictSerializer):
    loan_days = serializers.IntegerField(min_value=1, max_value=365, allow_null=True, required=False)
    fine_per_day = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=Decimal(0), allow_null=True, required=False
    )
    max_loans = serializers.IntegerField(min_value=1, max_value=100, allow_null=True, required=False)


class CopyOut(serializers.ModelSerializer[Copy]):
    book_id = serializers.UUIDField()

    class Meta:
        model = Copy
        fields = ("id", "book_id", "accession_number", "shelf", "status")
        read_only_fields = fields


class CopyIn(StrictSerializer):
    accession_number = serializers.CharField(max_length=32)
    shelf = serializers.CharField(max_length=50, required=False, allow_blank=True)


class CopyStatusIn(StrictSerializer):
    status = serializers.ChoiceField([CopyStatus.AVAILABLE, CopyStatus.LOST, CopyStatus.WITHDRAWN])


class BookOut(serializers.ModelSerializer[Book]):
    copies = CopyOut(many=True)
    available = serializers.IntegerField()

    class Meta:
        model = Book
        fields = (
            "id",
            "title",
            "authors",
            "isbn",
            "publisher",
            "category",
            "copies",
            "available",
            "created_at",
        )
        read_only_fields = fields


class BookIn(StrictSerializer):
    title = serializers.CharField(max_length=300)
    authors = serializers.CharField(max_length=300, required=False, allow_blank=True)
    isbn = serializers.CharField(max_length=17, required=False, allow_blank=True)
    publisher = serializers.CharField(max_length=200, required=False, allow_blank=True)
    category = serializers.CharField(max_length=100, required=False, allow_blank=True)
    copies = CopyIn(many=True, required=False)


class BookUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=300, required=False)
    authors = serializers.CharField(max_length=300, required=False, allow_blank=True)
    isbn = serializers.CharField(max_length=17, required=False, allow_blank=True)
    publisher = serializers.CharField(max_length=200, required=False, allow_blank=True)
    category = serializers.CharField(max_length=100, required=False, allow_blank=True)


class LoanOut(serializers.ModelSerializer[Loan]):
    copy = CopyOut()
    title = serializers.CharField(source="copy.book.title")
    student = PersonRef(allow_null=True)
    staff_member = serializers.CharField(source="member.user.full_name", allow_null=True, default=None)
    overdue = serializers.BooleanField()

    class Meta:
        model = Loan
        fields = (
            "id",
            "copy",
            "title",
            "student",
            "staff_member",
            "issued_on",
            "due_on",
            "returned_on",
            "renewals",
            "fine",
            "fine_paid",
            "overdue",
        )
        read_only_fields = fields


class IssueIn(StrictSerializer):
    copy_id = serializers.UUIDField()
    student_id = serializers.UUIDField(required=False, allow_null=True)
    staff_id = serializers.UUIDField(required=False, allow_null=True)
    due_on = serializers.DateField(required=False)


class ReturnIn(StrictSerializer):
    lost = serializers.BooleanField(required=False, default=False)


class RenewIn(StrictSerializer):
    due_on = serializers.DateField(required=False)
