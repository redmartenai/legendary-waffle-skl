"""Scoped resources for the academic structure.

Campuses, years, departments, grades and subjects hold no personal data; every role that reads them reads
them school-wide, so they need no scope rules. Sections are scoped by relationships owned by the ``people``
module (teaching assignments, enrollments, guardianship); those rules are registered in
``eduflow.people.policies`` so this module never depends on ``people``.
"""

from __future__ import annotations

from eduflow.authz.scopes import ScopedResource

from .models import AcademicYear, Campus, Department, Grade, Section, Subject

campuses: ScopedResource[Campus] = ScopedResource("campus", Campus)
academic_years: ScopedResource[AcademicYear] = ScopedResource("academic_year", AcademicYear)
departments: ScopedResource[Department] = ScopedResource("department", Department)
grades: ScopedResource[Grade] = ScopedResource("grade", Grade)
sections: ScopedResource[Section] = ScopedResource("section", Section)
subjects: ScopedResource[Subject] = ScopedResource("subject", Subject)
