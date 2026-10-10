"""AI-generated assessments: only through a configured provider (``AI_QUESTION_PROVIDER``).

EduFlow ships no provider and never fabricates generated content. With no provider configured (the
default), generation answers ``503 ai_unavailable``. A provider is a class with::

    def generate(self, *, subject: str, topic: str, count: int, grade: str) -> list[dict]:
        # each: {"text": str, "options": [str, ...], "answer": int, "explanation": str}

Generated questions are returned as a **draft** for the teacher to review; nothing is published
automatically.
"""

from __future__ import annotations

from typing import Any, Protocol

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework.exceptions import APIException


class AiUnavailable(APIException):
    status_code = 503
    default_code = "ai_unavailable"
    default_detail = "AI question generation is not configured for this deployment."


class QuestionProvider(Protocol):
    def generate(self, *, subject: str, topic: str, count: int, grade: str) -> list[dict[str, Any]]: ...


def provider() -> QuestionProvider:
    path = getattr(settings, "AI_QUESTION_PROVIDER", "")
    if not path:
        raise AiUnavailable()
    instance: QuestionProvider = import_string(path)()
    return instance


def generate(*, subject: str, topic: str, count: int, grade: str) -> list[dict[str, Any]]:
    """Ask the configured provider; validate the shape of what comes back before anyone sees it."""
    raw = provider().generate(subject=subject, topic=topic, count=count, grade=grade)
    questions = []
    for item in raw[:count]:
        options = [str(o)[:300] for o in item.get("options", [])][:8]
        answer = item.get("answer")
        if (
            not item.get("text")
            or len(options) < 2
            or not isinstance(answer, int)
            or not 0 <= answer < len(options)
        ):
            continue  # malformed output is dropped, never repaired by guessing
        questions.append(
            {
                "text": str(item["text"])[:1000],
                "options": options,
                "answer": answer,
                "explanation": str(item.get("explanation", ""))[:1000],
            }
        )
    return questions
