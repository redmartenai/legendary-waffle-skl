"""Scoped resource endpoints: one implementation of list / create / detail / update / delete for every
school-owned resource, built on :class:`TenantAPIView` and :class:`ScopedResource`.

Every request still goes through the Phase 2 chain (authentication -> tenant -> permission). On top of it:

* **Reads** come only from ``resource.queryset(actor, read_permission)``: school filter plus data scope.
  Filters narrow that queryset further; they can never widen it. An object outside it is 404.
* **Writes** need the write permission **school-wide**. A write references other records (a section, a
  student, a membership) that a narrower scope could not vouch for, so a section- or self-scoped write grant
  does not authorise any write here. Narrower write scopes arrive with the modules that need them (e.g.
  attendance). The target is still loaded through ``resource.get(actor, write_permission, pk)``; the change
  itself is a service function (validation, transaction, audit).
* **Lists** are cursor-paginated, newest first by UUIDv7 (docs/api/conventions.md).

A subclass declares::

    @document_resource
    class SectionList(ResourceListView):
        tag = "academics"
        resource = sections
        read_permission, write_permission = "section.read", "section.manage"
        output_serializer, create_serializer = SectionOut, SectionCreateIn
        filters = [Filter("academic_year_id", "academic_year_id", serializers.UUIDField()), ...]
        def base_queryset(self): return Section.objects.select_related("grade", "academic_year")
        def perform_create(self, data): return services.create_section(self.actor, **data)
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from django.db.models import Model, QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.request import Request
from rest_framework.response import Response

from eduflow.core.api import TENANT_HEADER, errors

from ..catalog import DataScope
from ..scopes import ScopedResource
from .base import TenantAPIView


@dataclass(frozen=True)
class Filter:
    """A query parameter: validated with ``field``, then applied as ``qs.filter(<lookup>=value)``.

    ``apply`` replaces the default for filters that cross a to-many relation (to avoid duplicate rows).
    """

    param: str
    lookup: str
    field: serializers.Field[Any, Any, Any, Any]
    description: str = ""
    apply: Callable[[QuerySet[Any], Any], QuerySet[Any]] | None = None

    def openapi(self) -> OpenApiParameter:
        kind = {
            serializers.UUIDField: OpenApiTypes.UUID,
            serializers.BooleanField: OpenApiTypes.BOOL,
            serializers.DateField: OpenApiTypes.DATE,
        }.get(type(self.field), OpenApiTypes.STR)
        enum = list(self.field.choices) if isinstance(self.field, serializers.ChoiceField) else None
        return OpenApiParameter(self.param, kind, enum=enum, description=self.description)


class Cursor(CursorPagination):
    ordering = "-id"  # UUIDv7: creation order
    page_size = 50
    max_page_size = 200
    page_size_query_param = "page_size"


PAGINATION_PARAMETERS = [
    OpenApiParameter("cursor", OpenApiTypes.STR, description="Opaque cursor from `next` / `previous`."),
    OpenApiParameter("page_size", OpenApiTypes.INT, description="1-200, default 50."),
]


class ResourceView(TenantAPIView):
    tag: ClassVar[str] = ""
    resource: ClassVar[ScopedResource[Any]]
    read_permission: ClassVar[str]
    write_permission: ClassVar[str | None] = None
    output_serializer: ClassVar[type[serializers.Serializer[Any]]]
    # True when the module's service re-checks a narrower write grant itself (ADR-027: a teacher writing
    # for a section they teach). The target is still loaded through the write permission's scope.
    narrow_writes: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        concrete = cls.permission_map.__func__ is not ResourceView.permission_map.__func__  # type: ignore[attr-defined]
        if concrete and getattr(cls, "read_permission", None):
            cls.required_permissions = cls.permission_map()
            # Methods without a permission are not routed at all (405), not merely refused.
            cls.http_method_names = [m.lower() for m in cls.required_permissions] + ["head", "options"]

    @classmethod
    def permission_map(cls) -> dict[str, str]:
        raise NotImplementedError

    def base_queryset(self) -> QuerySet[Any]:
        qs: QuerySet[Any] = self.resource.model._default_manager.all()
        return qs

    def scoped(self, permission: str) -> QuerySet[Any]:
        return self.resource.queryset(self.actor, permission, self.base_queryset())

    def get_object(self, pk: Any, permission: str) -> Any:
        return self.resource.get(self.actor, permission, pk, base=self.base_queryset())

    def require_school_scope(self, request: Request, permission: str) -> None:
        if self.narrow_writes and self.actor.scopes(permission):
            return
        if DataScope.SCHOOL not in self.actor.scopes(permission):
            self.permission_denied(request)

    def out(self, obj: Model) -> dict[str, Any]:
        # Re-read through the read scope, with the list's select_related, so the response is complete.
        fresh = self.base_queryset().filter(pk=obj.pk).first() or obj
        return dict(self.output_serializer(fresh).data)


class ResourceListView(ResourceView):
    create_serializer: ClassVar[type[serializers.Serializer[Any]] | None] = None
    filters: ClassVar[Sequence[Filter]] = ()

    @classmethod
    def permission_map(cls) -> dict[str, str]:
        perms = {"GET": cls.read_permission}
        if cls.create_serializer is not None and cls.write_permission:
            perms["POST"] = cls.write_permission
        return perms

    def filter_queryset(self, qs: QuerySet[Any], request: Request) -> QuerySet[Any]:
        problems: dict[str, list[str]] = {}
        for f in self.filters:
            raw = request.query_params.get(f.param)
            if raw is None or raw == "":
                continue
            try:
                value = f.field.run_validation(raw)
            except ValidationError as exc:
                detail = exc.detail if isinstance(exc.detail, list) else [exc.detail]
                problems[f.param] = [str(d) for d in detail]
                continue
            qs = f.apply(qs, value) if f.apply else qs.filter(**{f.lookup: value})
        if problems:
            raise ValidationError(problems)
        return qs

    def get(self, request: Request) -> Response:
        qs = self.filter_queryset(self.scoped(self.read_permission), request)
        paginator = Cursor()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(self.output_serializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        if self.create_serializer is None:  # pragma: no cover - POST is not routed without it
            self.permission_denied(request)
        self.require_school_scope(request, self.write_permission or "")
        body = self.create_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        obj = self.perform_create(dict(body.validated_data))
        return Response(self.out(obj), status=201)

    def perform_create(self, data: dict[str, Any]) -> Model:
        raise NotImplementedError


class ResourceDetailView(ResourceView):
    update_serializer: ClassVar[type[serializers.Serializer[Any]] | None] = None
    allow_delete: ClassVar[bool] = False

    @classmethod
    def permission_map(cls) -> dict[str, str]:
        perms = {"GET": cls.read_permission}
        if cls.write_permission:
            if cls.update_serializer is not None:
                perms["PATCH"] = cls.write_permission
            if cls.allow_delete:
                perms["DELETE"] = cls.write_permission
        return perms

    def get(self, request: Request, pk: Any) -> Response:
        return Response(self.output_serializer(self.get_object(pk, self.read_permission)).data)

    def patch(self, request: Request, pk: Any) -> Response:
        if self.update_serializer is None or not self.write_permission:
            self.permission_denied(request)
        self.require_school_scope(request, self.write_permission)
        obj = self.get_object(pk, self.write_permission)
        body = self.update_serializer(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        return Response(self.out(self.perform_update(obj, dict(body.validated_data))))

    def delete(self, request: Request, pk: Any) -> Response:
        if not self.allow_delete or not self.write_permission:
            self.permission_denied(request)
        self.require_school_scope(request, self.write_permission)
        self.perform_delete(self.get_object(pk, self.write_permission))
        return Response(status=204)

    def perform_update(self, obj: Any, data: dict[str, Any]) -> Model:
        raise NotImplementedError

    def perform_delete(self, obj: Any) -> None:
        raise NotImplementedError


def paginated(serializer: type[serializers.Serializer[Any]]) -> Any:
    return inline_serializer(
        f"Paginated{serializer.__name__.removesuffix('Out')}List",
        fields={
            "next": serializers.URLField(allow_null=True),
            "previous": serializers.URLField(allow_null=True),
            "results": serializer(many=True),
        },
    )


def document_resource[V: type[TenantAPIView]](view: V) -> V:
    """Attach the OpenAPI description derived from the view's declaration."""
    perms = view.permission_map()  # type: ignore[attr-defined]
    key = getattr(view, "resource").name  # noqa: B009
    name = key.replace("_", " ")
    tags = [getattr(view, "tag", "") or "domain"]
    out = getattr(view, "output_serializer")  # noqa: B009
    docs: dict[str, Any] = {}

    def scope_note(permission: str) -> str:
        return f"Requires `{permission}`; results are limited to the caller's data scope for it."

    if issubclass(view, ResourceListView):
        docs["get"] = extend_schema(
            tags=tags,
            operation_id=f"{key}_list",
            summary=f"List {name}s",
            description=scope_note(perms["GET"]),
            parameters=[TENANT_HEADER, *(f.openapi() for f in view.filters), *PAGINATION_PARAMETERS],
            responses={200: paginated(out), **errors(400, 401, 403)},
        )
        if "POST" in perms:
            docs["post"] = extend_schema(
                tags=tags,
                operation_id=f"{key}_create",
                summary=f"Create a {name}",
                description=f"Requires `{perms['POST']}`.",
                parameters=[TENANT_HEADER],
                request=view.create_serializer,
                responses={201: out, **errors(400, 401, 403, 409)},
            )
    elif issubclass(view, ResourceDetailView):
        docs["get"] = extend_schema(
            tags=tags,
            operation_id=f"{key}_retrieve",
            summary=f"A {name}",
            description=scope_note(perms["GET"]) + " Outside the scope or school: `404`.",
            parameters=[TENANT_HEADER],
            responses={200: out, **errors(401, 403, 404)},
        )
        if "PATCH" in perms:
            docs["patch"] = extend_schema(
                tags=tags,
                operation_id=f"{key}_update",
                summary=f"Update a {name}",
                description=f"Requires `{perms['PATCH']}`.",
                parameters=[TENANT_HEADER],
                request=view.update_serializer,
                responses={200: out, **errors(400, 401, 403, 404, 409)},
            )
        if "DELETE" in perms:
            docs["delete"] = extend_schema(
                tags=tags,
                operation_id=f"{key}_destroy",
                summary=f"Delete a {name}",
                description=(
                    f"Requires `{perms['DELETE']}`. `409` while other records still use it: "
                    "archive it instead."
                ),
                parameters=[TENANT_HEADER],
                responses={204: None, **errors(401, 403, 404, 409)},
            )
    return extend_schema_view(**docs)(view)
