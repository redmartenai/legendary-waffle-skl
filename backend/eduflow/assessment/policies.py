"""Who sees which exam data (people.scoping).

* Exams and grade bands hold no personal data: read grants are school-wide.
* Mark sheets follow their section and marks their student. A teacher sees every state for the sections and
  students they teach; families (child, self) see **published** sheets and marks only.
* Corrections follow the student; families never see them.
"""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import (
    children,
    own_sections,
    own_student,
    sections_of_children,
    taught_sections,
    taught_students,
)

from .models import Exam, GradeBand, Mark, MarkCorrection, MarkSheet, SheetStatus

exams: ScopedResource[Exam] = ScopedResource("exam", Exam)
grade_bands: ScopedResource[GradeBand] = ScopedResource("grade_band", GradeBand)
sheets: ScopedResource[MarkSheet] = ScopedResource("mark_sheet", MarkSheet)
marks: ScopedResource[Mark] = ScopedResource("mark", Mark)
corrections: ScopedResource[MarkCorrection] = ScopedResource("mark_correction", MarkCorrection)

PUBLISHED_SHEET = Q(status=SheetStatus.PUBLISHED)
PUBLISHED_MARK = Q(sheet__status=SheetStatus.PUBLISHED)

sheets.rule(DataScope.SECTION)(lambda actor: taught_sections("section__", actor))
sheets.rule(DataScope.CHILD)(lambda actor: sections_of_children("section__", actor) & PUBLISHED_SHEET)
sheets.rule(DataScope.SELF)(lambda actor: own_sections("section__", actor) & PUBLISHED_SHEET)

marks.rule(DataScope.SECTION)(lambda actor: taught_students("student__", actor))
marks.rule(DataScope.CHILD)(lambda actor: children("student__", actor) & PUBLISHED_MARK)
marks.rule(DataScope.SELF)(lambda actor: own_student("student__", actor) & PUBLISHED_MARK)

corrections.rule(DataScope.SECTION)(lambda actor: taught_students("mark__student__", actor))
