import uuid

from django.db import models

from .tenant import TenantContextMissing, bypass_active, current_school


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SchoolScopedManager(models.Manager):
    """Filters every query to the active school and fails closed without one."""

    def get_queryset(self):
        queryset = super().get_queryset()
        if bypass_active():
            return queryset
        school = current_school(required=False)
        if school is None:
            raise TenantContextMissing(
                f"{self.model.__name__} was queried without a school context."
            )
        return queryset.filter(school=school)


class SchoolScopedModel(TimeStampedModel):
    school = models.ForeignKey(
        "tenancy.School", on_delete=models.CASCADE, related_name="+", editable=False
    )

    objects = SchoolScopedManager()
    # Unfiltered access for platform code. Always pair with unscoped() or an explicit
    # school filter; grep for "all_objects" when reviewing tenant isolation.
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        school = current_school(required=False)
        if self.school_id is None:
            if school is None:
                raise TenantContextMissing(
                    f"Cannot save {type(self).__name__} without a school context."
                )
            self.school = school
        elif school is not None and self.school_id != school.id and not bypass_active():
            raise TenantContextMissing("Cross-school write blocked.")
        super().save(*args, **kwargs)
