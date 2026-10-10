"""Storing and serving uploaded files, for every module (documents, homework, learning content).

* The **type comes from the bytes**: PDF, PNG, JPEG, WebP, Office Open XML (docx, xlsx, pptx) and plain
  text. Anything else is refused, including SVG, HTML and executables, whatever its name or declared type.
* At most ``MAX_BYTES`` (10 MB).
* Keys are ``documents/<school>/<file id>``: nothing from the client.
* Downloads are served by the API as attachments with an exact type, ``nosniff`` and a sandboxing CSP, and
  are never cached by shared caches. The caller's module decides *whether* the caller may download.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.http import HttpResponse
from rest_framework.exceptions import ValidationError

from eduflow.core.ids import uuid7

from .models import StoredFile

MAX_BYTES = 10 * 1024 * 1024
_OOXML = {
    "word/": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "xl/": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "ppt/": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
}


def storage() -> Any:
    return storages["documents"]


def _bad(message: str) -> ValidationError:
    return ValidationError({"file": [message]})


def identify(data: bytes) -> tuple[str, str]:
    """``(content_type, extension)`` from the content, or a ``400``."""
    if data.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile:
            raise _bad("This file is damaged.") from None
        if "[Content_Types].xml" in names:
            for prefix, kind in _OOXML.items():
                if any(n.startswith(prefix) for n in names):
                    return kind
        raise _bad("Only Word, Excel and PowerPoint files are accepted among archives.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    if (
        text
        and not re.search(r"<\s*(script|html|svg|iframe|object)", text, re.IGNORECASE)
        and "\x00" not in text
    ):
        return "text/plain; charset=utf-8", "txt"
    raise _bad("Upload a PDF, an image (PNG, JPEG, WebP), an Office document or plain text.")


def store(school_id: Any, upload: Any, uploaded_by: Any = None) -> StoredFile:
    """Validate and store an upload. If the surrounding transaction later fails, the object stays behind as
    an unreferenced private file (harmless; there is no rollback hook to remove it)."""
    if upload.size > MAX_BYTES:
        raise _bad("The file is larger than 10 MB.")
    data: bytes = upload.read()
    if not data:
        raise _bad("The file is empty.")
    content_type, extension = identify(data)
    file_id = uuid7()
    key = storage().save(f"documents/{school_id}/{file_id}.{extension}", ContentFile(data))
    name = (
        re.sub(r"[^\w.\- ]", "_", str(getattr(upload, "name", "") or f"file.{extension}"))[-200:]
        or f"file.{extension}"
    )
    return StoredFile.objects.create(
        id=file_id,
        school_id=school_id,
        filename=name,
        content_type=content_type,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        storage_key=key,
        uploaded_by=uploaded_by,
    )


def download(record: StoredFile) -> HttpResponse:
    with storage().open(record.storage_key, "rb") as handle:
        data = handle.read()
    response = HttpResponse(data, content_type=record.content_type)
    safe = record.filename.replace('"', "")
    response["Content-Disposition"] = f'attachment; filename="{safe}"'
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Cache-Control"] = "private, no-store"
    return response
