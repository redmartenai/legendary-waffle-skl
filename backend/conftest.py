from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from django.conf import settings
from rest_framework.test import APIClient


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


class LogCapture:
    """Captures the real rendered log output (after redaction and JSON rendering)."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def text(self) -> str:
        return self._stream.getvalue()

    @property
    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]

    def find(self, event: str) -> list[dict[str, object]]:
        return [r for r in self.records if r.get("event") == event]


@pytest.fixture
def json_logs() -> Iterator[LogCapture]:
    """Attach a handler using the project's configured formatter to the root logger."""
    from logging.config import dictConfig

    config = settings.LOGGING
    dictConfig(config)  # ensure the formatter object exists exactly as configured
    root = logging.getLogger()
    formatter = root.handlers[0].formatter
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    try:
        yield LogCapture(stream)
    finally:
        root.removeHandler(handler)
