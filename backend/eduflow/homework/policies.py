"""Homework follows its section (people.scoping): a teacher sees the homework of sections they teach, a
parent their children's sections, a student their own. Submissions follow their student."""

from __future__ import annotations

from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import section_rules, student_rules

from .models import Homework, Submission

homework: ScopedResource[Homework] = ScopedResource("homework", Homework)
submissions: ScopedResource[Submission] = ScopedResource("homework_submission", Submission)

section_rules(homework)
student_rules(submissions)
