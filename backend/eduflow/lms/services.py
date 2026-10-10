"""LMS writes and analytics. Transactional and audited (``lms.*``). Teacher writes are narrow: a section and
subject they teach (ADR-027)."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Avg, Count, Max, Q, QuerySet
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import Section, Subject
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.documents import files
from eduflow.notifications import services as notifications
from eduflow.people.models import Student
from eduflow.people.scoping import may_write
from eduflow.tenancy import domain

from .models import (
    Attempt,
    LearningPath,
    Lesson,
    LessonKind,
    LiveAttendance,
    LiveClass,
    LiveStatus,
    PathStep,
    Progress,
    PublishStatus,
    Question,
    Quiz,
)

MANAGE = "lms.manage"


def _place(actor: Actor, section_id: Any, subject_id: Any) -> tuple[Section, Subject | None]:
    section = domain.resolve(Section, actor.school, section_id, "section_id", label="section")
    subject = (
        domain.resolve(Subject, actor.school, subject_id, "subject_id", label="subject")
        if subject_id
        else None
    )
    if not may_write(actor, MANAGE, section, subject):
        raise PermissionDenied("You can publish learning content only for a section and subject you teach.")
    return section, subject


def _place_subject(actor: Actor, section_id: Any, subject_id: Any) -> tuple[Section, Subject]:
    section, subject = _place(actor, section_id, subject_id)
    if subject is None:
        raise ValidationError({"subject_id": ["Choose a subject."]})
    return section, subject


def _guard(actor: Actor, obj: Lesson | Quiz | LearningPath | LiveClass) -> None:
    if not may_write(actor, MANAGE, obj.section, obj.subject):
        raise PermissionDenied()


def _students(section: Section) -> QuerySet[Student]:
    return Student.objects.filter(enrollments__section=section, enrollments__status="active").distinct()


def _notify_class(actor: Actor, section: Section, title: str, link: tuple[str, Any]) -> None:
    members: list[Any] = []
    for student in _students(section):
        members += notifications.family_of(student)
    notifications.notify(actor.school, members, kind="learning", title=title, link=link)


def _secure_url(value: str, field: str) -> None:
    if value and not value.startswith("https://"):
        raise ValidationError({field: ["Use an https:// link."]})


# ------------------------------------------------------------------------------------------------ quizzes
def _questions(quiz: Quiz, rows: Iterable[dict[str, Any]]) -> None:
    quiz.questions.all().delete()
    for i, q in enumerate(rows):
        if not 0 <= q["answer"] < len(q["options"]):
            raise ValidationError({f"questions[{i}].answer": ["The answer is not one of the options."]})
        Question.objects.create(school_id=quiz.school_id, quiz=quiz, position=i + 1, **q)


@transaction.atomic
def create_quiz(
    actor: Actor,
    *,
    section_id: Any,
    subject_id: Any,
    title: str,
    questions: list[dict[str, Any]],
    max_attempts: int | None = None,
) -> Quiz:
    section, subject = _place_subject(actor, section_id, subject_id)
    quiz = Quiz.objects.create(
        school=actor.school,
        section=section,
        subject=subject,
        title=title,
        max_attempts=max_attempts,
        author=actor.membership,
    )
    _questions(quiz, questions)
    domain.record("lms.quiz.created", quiz, questions=len(questions))
    return quiz


@transaction.atomic
def update_quiz(actor: Actor, quiz: Quiz, **data: Any) -> Quiz:
    _guard(actor, quiz)
    if "questions" in data:
        if quiz.attempts.exists():
            raise Conflict("Students have attempted this quiz; its questions cannot change.")
        _questions(quiz, data.pop("questions"))
    was = quiz.status
    changed = domain.apply_changes(quiz, data)
    if changed:
        quiz.save()
    domain.record("lms.quiz.updated", quiz, fields=changed, status=quiz.status)
    if was != PublishStatus.PUBLISHED and quiz.status == PublishStatus.PUBLISHED:
        _notify_class(actor, quiz.section, f"New quiz: {quiz.title}", ("quiz", quiz.pk))
    return quiz


def _own_student(actor: Actor) -> Student:
    student = Student.objects.filter(school_id=actor.school.pk, membership=actor.membership).first()
    if student is None:
        raise PermissionDenied("Only students take part in lessons and quizzes.")
    return student


@transaction.atomic
def attempt(actor: Actor, quiz: Quiz, *, answers: list[int]) -> Attempt:
    student = _own_student(actor)
    if quiz.status != PublishStatus.PUBLISHED:
        raise Conflict("This quiz is not open.")
    questions = list(quiz.questions.all())
    if len(answers) != len(questions):
        raise ValidationError({"answers": [f"Answer all {len(questions)} questions (use -1 to skip)."]})
    # Lock the student row so concurrent submissions respect the attempt limit.
    Student.objects.select_for_update().filter(pk=student.pk).first()
    if quiz.max_attempts and quiz.attempts.filter(student=student).count() >= quiz.max_attempts:
        raise Conflict(f"You have used all {quiz.max_attempts} attempts.")
    score = sum(1 for q, a in zip(questions, answers, strict=True) if q.answer == a)
    result = Attempt.objects.create(
        school=actor.school, quiz=quiz, student=student, answers=answers, score=score, total=len(questions)
    )
    domain.record("lms.quiz.attempted", result, quiz=str(quiz.pk), score=score, total=len(questions))
    return result


def best_scores(quiz: Quiz) -> dict[Any, int]:
    return {
        r["student_id"]: r["best"] for r in quiz.attempts.values("student_id").annotate(best=Max("score"))
    }


# ------------------------------------------------------------------------------------------------ lessons
@transaction.atomic
def create_lesson(
    actor: Actor,
    *,
    section_id: Any,
    subject_id: Any,
    title: str,
    kind: str,
    chapter: str = "",
    body: str = "",
    video_url: str = "",
    minutes: int | None = None,
    quiz_id: Any = None,
    upload: Any = None,
) -> Lesson:
    section, subject = _place_subject(actor, section_id, subject_id)
    _secure_url(video_url, "video_url")
    if kind == LessonKind.VIDEO and not video_url:
        raise ValidationError({"video_url": ["A recorded lesson needs the link where the video is hosted."]})
    quiz = None
    if kind == LessonKind.QUIZ:
        quiz = Quiz.objects.filter(school_id=actor.school.pk, pk=quiz_id, section=section).first()
        if quiz is None:
            raise ValidationError({"quiz_id": ["Choose a quiz of this section."]})
    if kind == LessonKind.NOTES and not body and upload is None:
        raise ValidationError({"body": ["Write the notes or attach a file."]})
    stored = files.store(actor.school.pk, upload, actor.membership) if upload is not None else None
    lesson = Lesson.objects.create(
        school=actor.school,
        section=section,
        subject=subject,
        title=title,
        chapter=chapter,
        kind=kind,
        body=body,
        video_url=video_url,
        minutes=minutes,
        quiz=quiz,
        file=stored,
        author=actor.membership,
    )
    domain.record("lms.lesson.created", lesson, kind=kind)
    return lesson


@transaction.atomic
def update_lesson(actor: Actor, lesson: Lesson, **data: Any) -> Lesson:
    _guard(actor, lesson)
    _secure_url(data.get("video_url", ""), "video_url")
    was = lesson.status
    changed = domain.apply_changes(lesson, data)
    if lesson.status == PublishStatus.PUBLISHED and was != PublishStatus.PUBLISHED:
        lesson.published_at = timezone.now()
    if changed:
        lesson.save()
        domain.record("lms.lesson.updated", lesson, fields=changed, status=lesson.status)
    if was != PublishStatus.PUBLISHED and lesson.status == PublishStatus.PUBLISHED:
        _notify_class(actor, lesson.section, f"New lesson: {lesson.title}", ("lesson", lesson.pk))
    return lesson


@transaction.atomic
def record_progress(actor: Actor, lesson: Lesson, *, percent: int) -> Progress:
    """The signed-in student reports how far they got. Progress never goes back."""
    student = _own_student(actor)
    if lesson.status != PublishStatus.PUBLISHED:
        raise Conflict("This lesson is not published.")
    row, _ = Progress.objects.select_for_update().get_or_create(
        school_id=actor.school.pk, lesson=lesson, student=student
    )
    if percent > row.percent:
        row.percent = percent
        if percent == 100 and row.completed_at is None:
            row.completed_at = timezone.now()
        row.save()
    return row


# ------------------------------------------------------------------------------------------------ paths
def _steps(actor: Actor, path: LearningPath, lesson_ids: list[Any]) -> None:
    if len(set(lesson_ids)) != len(lesson_ids):
        raise ValidationError({"lesson_ids": ["A lesson appears twice."]})
    lessons = {
        row.pk: row
        for row in Lesson.objects.filter(school_id=actor.school.pk, section=path.section, pk__in=lesson_ids)
    }
    for i, lid in enumerate(lesson_ids):
        if lid not in lessons:
            raise ValidationError({f"lesson_ids[{i}]": ["Choose a lesson of this section."]})
    path.steps.all().delete()
    PathStep.objects.bulk_create(
        [
            PathStep(school_id=path.school_id, path=path, lesson=lessons[lid], position=i + 1)
            for i, lid in enumerate(lesson_ids)
        ]
    )


@transaction.atomic
def create_path(
    actor: Actor,
    *,
    section_id: Any,
    title: str,
    lesson_ids: list[Any],
    subject_id: Any = None,
    description: str = "",
) -> LearningPath:
    section, subject = _place(actor, section_id, subject_id)
    path = LearningPath.objects.create(
        school=actor.school,
        section=section,
        subject=subject,
        title=title,
        description=description,
        author=actor.membership,
    )
    _steps(actor, path, lesson_ids)
    domain.record("lms.learning_path.created", path, steps=len(lesson_ids))
    return path


@transaction.atomic
def update_path(actor: Actor, path: LearningPath, **data: Any) -> LearningPath:
    _guard(actor, path)
    if "lesson_ids" in data:
        _steps(actor, path, data.pop("lesson_ids"))
    changed = domain.apply_changes(path, data)
    if changed:
        path.save()
    domain.record("lms.learning_path.updated", path, fields=changed)
    return path


def path_progress(path: LearningPath, student: Student) -> dict[str, Any]:
    lesson_ids = list(path.steps.values_list("lesson_id", flat=True))
    done = Progress.objects.filter(student=student, lesson_id__in=lesson_ids, percent=100).count()
    return {
        "steps": len(lesson_ids),
        "completed": done,
        "percent": round(100 * done / len(lesson_ids)) if lesson_ids else 0,
    }


# ------------------------------------------------------------------------------------------------ live
@transaction.atomic
def schedule_live(actor: Actor, *, section_id: Any, subject_id: Any, **data: Any) -> LiveClass:
    section, subject = _place_subject(actor, section_id, subject_id)
    _secure_url(data["join_url"], "join_url")
    live = LiveClass.objects.create(
        school=actor.school, section=section, subject=subject, host=actor.membership, **data
    )
    domain.record("lms.live_class.scheduled", live, provider=live.provider)
    _notify_class(actor, section, f"Live class: {live.title}", ("live_class", live.pk))
    return live


@transaction.atomic
def update_live(actor: Actor, live: LiveClass, **data: Any) -> LiveClass:
    _guard(actor, live)
    _secure_url(data.get("join_url", ""), "join_url")
    changed = domain.apply_changes(live, data)
    if changed:
        live.save()
        domain.record("lms.live_class.updated", live, fields=changed, status=live.status)
    return live


@transaction.atomic
def join_live(actor: Actor, live: LiveClass) -> str:
    student = _own_student(actor)
    if live.status != LiveStatus.SCHEDULED:
        raise Conflict("This live class was cancelled.")
    LiveAttendance.objects.get_or_create(school_id=actor.school.pk, live_class=live, student=student)
    return live.join_url


# ------------------------------------------------------------------------------------------------ analytics
def analytics(section: Section, subject_id: Any = None) -> dict[str, Any]:
    """Completion, average progress and quiz results for a section (optionally one subject)."""
    lessons = Lesson.objects.filter(section=section, status=PublishStatus.PUBLISHED)
    quizzes = Quiz.objects.filter(section=section, status=PublishStatus.PUBLISHED)
    if subject_id:
        lessons, quizzes = lessons.filter(subject_id=subject_id), quizzes.filter(subject_id=subject_id)
    students = list(_students(section))
    n_students = len(students)
    lesson_rows: list[dict[str, Any]] = []
    for lesson in lessons.annotate(
        completed=Count("progress", filter=Q(progress__percent=100)), avg=Avg("progress__percent")
    ).order_by("created_at"):
        lesson_rows.append(
            {
                "lesson": lesson,
                "completed": lesson.completed,
                "completion_rate": lesson.completed / n_students if n_students else None,
                "average_progress": Decimal(lesson.avg or 0).quantize(Decimal("0.1")),
            }
        )
    quiz_rows = []
    for quiz in quizzes:
        best = best_scores(quiz)
        total = quiz.questions.count()
        quiz_rows.append(
            {
                "quiz": quiz,
                "attempted": len(best),
                "average_best_percent": round(100 * sum(best.values()) / (len(best) * total), 1)
                if best and total
                else None,
            }
        )
    lesson_ids = [r["lesson"].pk for r in lesson_rows]
    per_student = []
    for student in students:
        done = Progress.objects.filter(student=student, lesson_id__in=lesson_ids, percent=100).count()
        per_student.append({"student": student, "lessons_completed": done, "lessons_total": len(lesson_ids)})
    return {"students": n_students, "lessons": lesson_rows, "quizzes": quiz_rows, "per_student": per_student}
