"""Helpers for school-domain services (academics, people and later modules).

* :func:`resolve` turns a client-supplied ID into an object **of the actor's school**. An ID from another
  school answers exactly like an unknown ID, so references cannot be used to probe other tenants.
* :func:`save` and :func:`delete` turn database integrity errors (unique, exclusion, protected foreign keys)
  into clean ``409 conflict`` responses. The constraints stay the source of truth; services pre-check only
  to give friendlier messages.
* :func:`apply_changes` sets fields and reports what changed, for the audit record.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from django.db import IntegrityError, models, transaction
from django.db.models import ProtectedError
from rest_framework.exceptions import ValidationError

from eduflow.audit import services as audit
from eduflow.core.api import Conflict

from .models import School


def resolve[M: models.Model](
    model: type[M], school: School, pk: Any, field: str, *, label: str | None = None
) -> M:
    obj = model._default_manager.filter(school_id=school.pk, pk=pk).first() if pk is not None else None
    if obj is None:
        raise ValidationError({field: [f"Unknown {label or model._meta.verbose_name}."]})
    return obj


def apply_changes(obj: models.Model, data: Mapping[str, Any]) -> list[str]:
    changed = []
    for name, value in data.items():
        if getattr(obj, name) != value:
            setattr(obj, name, value)
            changed.append(name)
    return changed


def save[M: models.Model](obj: M, *, conflict: str, update_fields: Iterable[str] | None = None) -> M:
    fields = list(update_fields) if update_fields is not None else None
    if fields is not None and hasattr(obj, "updated_at"):
        fields.append("updated_at")
    try:
        with transaction.atomic():
            obj.save(update_fields=fields)
    except IntegrityError:
        raise Conflict(conflict) from None
    return obj


def delete(obj: models.Model) -> None:
    pk = obj.pk
    try:
        with transaction.atomic():
            obj.delete()
    except (ProtectedError, IntegrityError):
        raise Conflict("Other records still use this. Archive it instead.") from None
    obj.pk = pk  # Django clears it on delete; the audit record still needs it.


def entity(obj: models.Model | type[models.Model]) -> str:
    """``AcademicYear`` -> ``academic_year``: the name used in audit actions and ``target_type``."""
    cls = obj if isinstance(obj, type) else type(obj)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()


def record(action: str, obj: models.Model, **metadata: Any) -> None:
    audit.record(
        action,
        target_type=entity(obj),
        target_id=obj.pk,
        metadata={k: v for k, v in metadata.items() if v not in (None, [], {})} or None,
    )
