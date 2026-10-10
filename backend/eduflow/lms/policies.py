"""LMS scopes (people.scoping). Lessons, quizzes, paths and live classes follow their section: a teacher sees
every state for sections they teach; families (child, self) see only published lessons, quizzes and paths
and scheduled live classes. Progress and attempts follow their student."""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import own_sections, sections_of_children, student_rules, taught_sections

from .models import Attempt, LearningPath, Lesson, LiveClass, Progress, Quiz

lessons: ScopedResource[Lesson] = ScopedResource("lesson_content", Lesson)
quizzes: ScopedResource[Quiz] = ScopedResource("quiz", Quiz)
paths: ScopedResource[LearningPath] = ScopedResource("learning_path", LearningPath)
live_classes: ScopedResource[LiveClass] = ScopedResource("live_class", LiveClass)
progress: ScopedResource[Progress] = ScopedResource("lesson_progress", Progress)
attempts: ScopedResource[Attempt] = ScopedResource("quiz_attempt", Attempt)

PUBLISHED = Q(status="published")
SCHEDULED = Q(status="scheduled")


def _register(resource: ScopedResource[Any], visible: Q) -> None:
    resource.rule(DataScope.SECTION)(lambda actor: taught_sections("section__", actor))
    resource.rule(DataScope.CHILD)(lambda actor: sections_of_children("section__", actor) & visible)
    resource.rule(DataScope.SELF)(lambda actor: own_sections("section__", actor) & visible)


_register(lessons, PUBLISHED)
_register(quizzes, PUBLISHED)
_register(paths, PUBLISHED)
_register(live_classes, SCHEDULED)
student_rules(progress)
student_rules(attempts)
