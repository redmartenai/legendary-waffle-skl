"""Library endpoints. ``library.read`` for the catalogue (everyone granted it) and loans (own, children's or
all); ``library.manage`` (school-wide, the librarian) for everything that changes the library."""

from __future__ import annotations

from typing import Any

from django.db.models import BooleanField, Case, Count, Q, QuerySet, Value, When
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.api.resources import (
    Filter,
    ResourceDetailView,
    ResourceListView,
    ResourceView,
    document_resource,
)
from eduflow.authz.catalog import DataScope
from eduflow.core.api import TENANT_HEADER, errors
from eduflow.tenancy import clock, domain

from .. import policies, services
from ..models import Book, CopyStatus, Loan
from . import serializers as s

TAG = "library"
MANAGE = "library.manage"


def _school_wide(view: TenantAPIView) -> None:
    if DataScope.SCHOOL not in view.actor.scopes(MANAGE):
        view.permission_denied(view.request)


class SettingsView(TenantAPIView):
    required_permissions = {"GET": "library.read", "PUT": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Library settings",
        parameters=[TENANT_HEADER],
        responses={200: s.LibrarySettingsOut, **errors(401, 403)},
    )
    def get(self, request: Request) -> Response:
        return Response(s.LibrarySettingsOut(services.settings_for(self.actor.school)).data)

    @extend_schema(
        tags=[TAG],
        summary="Set the loan period, fine per day and loan limit",
        description="All optional. Without a fine rate no fine is charged.",
        parameters=[TENANT_HEADER],
        request=s.LibrarySettingsIn,
        responses={200: s.LibrarySettingsOut, **errors(400, 401, 403)},
    )
    def put(self, request: Request) -> Response:
        _school_wide(self)
        body = s.LibrarySettingsIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(s.LibrarySettingsOut(services.set_settings(self.actor, **body.validated_data)).data)


class _Book(ResourceView):
    tag = TAG
    resource = policies.books
    read_permission, write_permission = "library.read", MANAGE
    output_serializer = s.BookOut

    def base_queryset(self) -> QuerySet[Book]:
        return Book.objects.prefetch_related("copies").annotate(
            available=Count("copies", filter=Q(copies__status=CopyStatus.AVAILABLE))
        )


def _search(qs: QuerySet[Any], value: str) -> QuerySet[Any]:
    return qs.filter(Q(title__icontains=value) | Q(authors__icontains=value) | Q(isbn=value))


@document_resource
class BookList(_Book, ResourceListView):
    create_serializer = s.BookIn
    filters = [
        Filter("q", "", serializers.CharField(max_length=100), "Title, author or ISBN.", apply=_search),
        Filter("category", "category", serializers.CharField(max_length=100)),
    ]

    def perform_create(self, data: dict[str, Any]) -> Book:
        return services.add_book(self.actor, **data)


@document_resource
class BookDetail(_Book, ResourceDetailView):
    update_serializer = s.BookUpdateIn

    def perform_update(self, obj: Book, data: dict[str, Any]) -> Book:
        changed = domain.apply_changes(obj, data)
        if changed:
            obj.save()
            domain.record("library.book.updated", obj, fields=changed)
        return obj


class BookCopiesView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Add a copy of a book",
        parameters=[TENANT_HEADER],
        request=s.CopyIn,
        responses={201: s.CopyOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self)
        book = policies.books.get(self.actor, MANAGE, pk)
        body = s.CopyIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.CopyOut(services.add_copy(self.actor, book, **body.validated_data)).data, status=201
        )


class CopyStatusView(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    @extend_schema(
        tags=[TAG],
        summary="Mark a copy available, lost or withdrawn",
        parameters=[TENANT_HEADER],
        request=s.CopyStatusIn,
        responses={200: s.CopyOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        _school_wide(self)
        copy = policies.copies.get(self.actor, MANAGE, pk)
        body = s.CopyStatusIn(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            s.CopyOut(services.set_copy_status(self.actor, copy, body.validated_data["status"])).data
        )


class _Loan(ResourceView):
    tag = TAG
    resource = policies.loans
    read_permission, write_permission = "library.read", MANAGE
    output_serializer = s.LoanOut

    def base_queryset(self) -> QuerySet[Loan]:
        today = clock.today(self.actor.school)
        return Loan.objects.select_related("copy__book", "student", "member__user").annotate(
            overdue=Case(
                When(returned_on__isnull=True, due_on__lt=today, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )


def _open(qs: QuerySet[Any], value: bool) -> QuerySet[Any]:
    return qs.filter(returned_on__isnull=value)


@document_resource
class LoanList(_Loan, ResourceListView):
    create_serializer = s.IssueIn
    filters = [
        Filter("open", "", serializers.BooleanField(), "Only loans not yet returned.", apply=_open),
        Filter("overdue", "overdue", serializers.BooleanField()),
        Filter("student_id", "student_id", serializers.UUIDField()),
    ]

    def perform_create(self, data: dict[str, Any]) -> Loan:
        return services.issue(self.actor, **data)


@document_resource
class LoanDetail(_Loan, ResourceDetailView):
    pass


class _LoanAction(TenantAPIView):
    required_permissions = {"POST": MANAGE}

    def loan(self, pk: Any) -> Loan:
        _school_wide(self)
        return policies.loans.get(self.actor, MANAGE, pk)

    def respond(self, loan: Loan) -> Response:
        return Response(s.LoanOut(_Loan.base_queryset(self).get(pk=loan.pk)).data)  # type: ignore[arg-type]


class LoanReturnView(_LoanAction):
    @extend_schema(
        tags=[TAG],
        summary="Return a loan (or close it as lost)",
        description="The fine is the days late times the school's fine per day (none without a rate).",
        parameters=[TENANT_HEADER],
        request=s.ReturnIn,
        responses={200: s.LoanOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        body = s.ReturnIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.return_loan(self.actor, self.loan(pk), **body.validated_data))


class LoanRenewView(_LoanAction):
    @extend_schema(
        tags=[TAG],
        summary="Renew a loan",
        parameters=[TENANT_HEADER],
        request=s.RenewIn,
        responses={200: s.LoanOut, **errors(400, 401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        body = s.RenewIn(data=request.data)
        body.is_valid(raise_exception=True)
        return self.respond(services.renew(self.actor, self.loan(pk), **body.validated_data))


class LoanFinePaidView(_LoanAction):
    @extend_schema(
        tags=[TAG],
        summary="Record that a loan's fine was paid",
        parameters=[TENANT_HEADER],
        request=None,
        responses={200: s.LoanOut, **errors(401, 403, 404, 409)},
    )
    def post(self, request: Request, pk: Any) -> Response:
        return self.respond(services.mark_fine_paid(self.actor, self.loan(pk)))
