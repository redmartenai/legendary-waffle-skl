"""Console: documents page (PDocuments).

A permission-controlled repository: folders (some locked to management), a permissions matrix per document
(role/audience x view/download/upload, with the cells role policy fixes), and an access log where every download
is recorded with the user, time and device (and written to the audit trail).
"""

import mimetypes

from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404
from django.urls import path
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.academics.models import ClassGroup, StudentGuardian
from apps.accounts.audit import audit
from apps.accounts.models import MANAGEMENT_ROLES, STAFF_ROLES, Membership, Role
from apps.core.api import SchoolAPIView
from apps.documents.models import Document, DocumentDownload, DocumentGrant, Folder
from apps.documents.views import _log

from .common import CONSOLE_ROLES
from apps.principal.views import _grade_key

from .communication import _all_sections, _range

DEFAULT_QUOTA_GB = 50
PAGE = 11
MAX_UPLOAD = 25 * 1024 * 1024
Subject = DocumentGrant.Subject
# Role policy: families never upload to the school's repository.
FIXED_OFF = {Subject.PARENTS: {"upload"}, Subject.STUDENTS: {"upload"}}


def file_type(doc: Document) -> str:
    ext = doc.file.name.rsplit(".", 1)[-1].lower() if doc.file and "." in doc.file.name else ""
    return {"pdf": "pdf", "doc": "doc", "docx": "doc", "xls": "xls", "xlsx": "xls", "csv": "xls", "png": "img", "jpg": "img", "jpeg": "img"}.get(ext, "file")


def groups_label(groups: list, by_grade: dict | None = None) -> str:
    """"6-B"; whole grades as a range ("6–10", "N–5"); otherwise the sections."""
    if not groups:
        return ""
    by_grade = by_grade if by_grade is not None else _all_sections()
    ids = {g.id for g in groups}
    grades = sorted({g.grade for g in groups})
    if len(groups) > 1 and all({x.id for x in by_grade.get(gr, [])} <= ids for gr in grades):
        return _range(grades)
    return ", ".join(sorted(g.short_label for g in groups))


def _scope(doc: Document) -> tuple[list, object]:
    """The classes (or route) a document is about: from its family grants, else its audience."""
    for g in doc.grants.all():
        if g.subject in (Subject.PARENTS, Subject.STUDENTS, Subject.CLASS_TEACHER, Subject.TEACHERS):
            groups = list(g.class_groups.all())
            if groups or g.route_id:
                return groups, g.route
    if doc.audience == Document.Audience.CLASSES:
        return list(doc.class_groups.all()), None
    return [], None


def access_of(doc: Document, by_grade: dict) -> dict:
    """The access pill: who can see it, in one phrase (the frontend words it)."""
    if doc.audience == Document.Audience.PRIVATE and not doc.grants.filter(can_view=True).exists():
        management = doc.owner_id and Membership.objects.filter(user_id=doc.owner_id, role__in=MANAGEMENT_ROLES).exists()
        return {"kind": "private", "label": "", "restricted": True, "owner_is_management": bool(management)}
    grants = [g for g in doc.grants.all() if g.can_view]
    if grants:
        family = [g for g in grants if g.subject in (Subject.PARENTS, Subject.STUDENTS)]
        for g in family:
            groups = list(g.class_groups.all())
            if not groups and not g.route_id:
                return {"kind": "school", "label": "", "restricted": False}
        for g in family:
            if g.route_id:
                return {"kind": "route", "label": g.route.name if g.route else "", "restricted": False}
            return {"kind": "parents" if g.subject == Subject.PARENTS else "students", "label": groups_label(list(g.class_groups.all()), by_grade), "restricted": False}
        staff = [g for g in grants if g.subject in (Subject.STAFF, Subject.TEACHERS, Subject.CLASS_TEACHER, Subject.ACCOUNTANT)]
        if any(g.subject == Subject.STAFF for g in staff):
            return {"kind": "staff", "label": "", "restricted": False}
        if staff:
            g = staff[0]
            return {"kind": g.subject, "label": groups_label(list(g.class_groups.all()), by_grade), "restricted": True}
        return {"kind": "private", "label": "", "restricted": True, "owner_is_management": True}
    if doc.audience in (Document.Audience.EVERYONE, Document.Audience.FAMILIES):
        return {"kind": "school", "label": "", "restricted": False}
    if doc.audience == Document.Audience.STAFF:
        return {"kind": "staff", "label": "", "restricted": False}
    if doc.audience == Document.Audience.CLASSES:
        return {"kind": "parents", "label": groups_label(list(doc.class_groups.all()), by_grade), "restricted": False}
    if doc.audience == Document.Audience.STUDENT and doc.student:
        return {"kind": "family", "label": doc.student.first_name, "restricted": True}
    return {"kind": "private", "label": "", "restricted": True, "owner_is_management": False}


def _access_filter(kind: str) -> Q:
    grants_view = Q(grants__can_view=True)
    if kind == "school":
        return Q(audience__in=["everyone", "families"], grants__isnull=True) | Q(grants__subject__in=["parents", "students"], grants__class_groups__isnull=True, grants__route__isnull=True) & grants_view
    if kind == "staff":
        return Q(audience="staff", grants__isnull=True) | Q(grants__subject="staff") & grants_view
    if kind == "parents":
        return Q(audience__in=["classes", "student"], grants__isnull=True) | (Q(grants__subject="parents") & grants_view & (Q(grants__class_groups__isnull=False) | Q(grants__route__isnull=False)))
    if kind == "restricted":
        return Q(audience="private", grants__isnull=True) | Q(audience="private", grants__can_view=False)
    return Q()


def _type_filter(kind: str) -> Q:
    exts = {"pdf": [".pdf"], "doc": [".doc", ".docx"], "xls": [".xls", ".xlsx", ".csv"], "img": [".png", ".jpg", ".jpeg"]}.get(kind)
    if not exts:
        return Q()
    q = Q()
    for ext in exts:
        q |= Q(file__iendswith=ext)
    return q


def _descendants(folder: Folder) -> list:
    out, frontier = [folder.id], [folder.id]
    while frontier:
        frontier = list(Folder.objects.filter(parent_id__in=frontier).values_list("id", flat=True))
        out += frontier
    return out


def _display_name(doc: Document) -> str:
    """"Circular 14.pdf": the title with the file's extension (storage names carry suffixes)."""
    ext = doc.file.name.rsplit(".", 1)[-1].lower() if doc.file and "." in doc.file.name.rsplit("/", 1)[-1] else ""
    return f"{doc.title}.{ext}" if ext and not doc.title.lower().endswith(f".{ext}") else doc.title


def _row(doc: Document, downloads: dict, by_grade: dict) -> dict:
    return {
        "id": str(doc.id),
        "title": doc.title,
        "name": _display_name(doc),
        "description": doc.subtitle,
        "type": file_type(doc),
        "pages": doc.pages,
        "size": doc.size,
        "version": doc.version,
        "owner": doc.owner.full_name if doc.owner else None,
        "date": (doc.issued_on or timezone.localtime(doc.created_at).date()).isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "access": access_of(doc, by_grade),
        "downloads": downloads.get(doc.id, 0),
        "folder_id": str(doc.folder_id) if doc.folder_id else None,
    }


class DocumentsView(SchoolAPIView):
    """The folder tree, storage, and one folder's documents (filtered, searched, paged)."""

    allowed_roles = CONSOLE_ROLES
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        p = request.query_params
        folders = list(Folder.objects.all())
        counts = dict(Document.objects.filter(folder__isnull=False).values_list("folder_id").annotate(n=Count("id")))
        by_id = {f.id: f for f in folders}
        folder = by_id.get(_uuid(p.get("folder")))
        docs = Document.objects.select_related("owner", "folder", "student").prefetch_related("grants__class_groups", "grants__route", "class_groups")
        if folder:
            docs = docs.filter(folder_id__in=_descendants(folder))
        if p.get("q"):
            docs = docs.filter(Q(title__icontains=p["q"]) | Q(subtitle__icontains=p["q"]) | Q(file__icontains=p["q"]))
        if p.get("access") in ("school", "staff", "parents", "restricted"):
            docs = docs.filter(_access_filter(p["access"]))
        if p.get("type") in ("pdf", "doc", "xls", "img"):
            docs = docs.filter(_type_filter(p["type"]))
        docs = docs.distinct().order_by("-updated_at")
        total = docs.count()
        limit = min(int(p.get("limit") or PAGE), 200)
        page = list(docs[:limit])
        downloads = dict(DocumentDownload.objects.filter(document__in=page, action=DocumentDownload.Action.DOWNLOAD).values_list("document_id").annotate(n=Count("id")))
        by_grade = _all_sections()
        trail = []
        f = folder
        while f:
            trail.insert(0, {"id": str(f.id), "name": f.name})
            f = by_id.get(f.parent_id)
        used = Document.objects.aggregate(s=Sum("size"))["s"] or 0
        quota = int(((request.school.settings or {}).get("documents") or {}).get("quota_gb") or DEFAULT_QUOTA_GB)
        return Response(
            {
                "folders": [
                    {"id": str(x.id), "name": x.name, "parent_id": str(x.parent_id) if x.parent_id else None, "locked": x.locked, "count": counts.get(x.id, 0)}
                    for x in folders
                ],
                "total_files": Document.objects.count(),
                "storage": {"used": used, "quota": quota * 1024**3},
                "folder": {"id": str(folder.id), "name": folder.name, "locked": folder.locked, "path": trail} if folder else None,
                "items": [_row(d, downloads, by_grade) for d in page],
                "total": total,
                "limit": limit,
                "sections": [
                    {"id": str(g.id), "label": g.short_label, "grade": g.grade}
                    for g in sorted((g for gs in by_grade.values() for g in gs), key=lambda g: (_grade_key(g.grade), g.section))
                ],
            }
        )

    def post(self, request):
        """Upload a file into a folder. The same name in the same folder becomes a new version."""
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Choose a file to upload."})
        if upload.size > MAX_UPLOAD:
            raise ValidationError({"file": "Files can be up to 25 MB."})
        folder = Folder.objects.filter(id=_uuid(request.data.get("folder_id"))).first() if request.data.get("folder_id") else None
        access = request.data.get("access") or "staff"
        if access not in ("school", "staff", "parents", "private"):
            raise ValidationError({"access": "Choose who can see it."})
        groups = list(ClassGroup.objects.filter(id__in=_ids(request, "class_ids"))) if access == "parents" else []
        if access == "parents" and not groups:
            raise ValidationError({"class_ids": "Choose the classes whose parents can see it."})
        title = (request.data.get("title") or upload.name.rsplit(".", 1)[0]).strip()[:160]
        existing = Document.objects.filter(folder=folder, title=title).first()
        doc = existing or Document(kind=_kind_for(folder), title=title, owner=request.user)
        if existing:
            doc.version += 1
        doc.subtitle = (request.data.get("description") or doc.subtitle or "")[:160]
        doc.file = upload
        doc.size = upload.size
        doc.pages = _pages(upload)
        doc.folder = folder
        doc.audience = {"school": Document.Audience.EVERYONE, "staff": Document.Audience.STAFF, "parents": Document.Audience.CLASSES, "private": Document.Audience.PRIVATE}[access]
        doc.issued_on = timezone.localdate()
        doc.save()
        if access == "parents":
            doc.class_groups.set(groups)
        if not existing:
            _default_grants(doc, access, groups)
        _log(request, kind="document", ref=str(doc.id), document=doc, action=DocumentDownload.Action.UPLOAD)
        return Response(_row(doc, {}, _all_sections()), status=status.HTTP_201_CREATED)


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _ids(request, key) -> list:
    import json

    values = request.data.getlist(key) if hasattr(request.data, "getlist") else (request.data.get(key) or [])
    if len(values) == 1 and isinstance(values[0], str) and values[0].startswith("["):
        values = json.loads(values[0])
    return [v for v in (_uuid(x) for x in values) if v]


def _kind_for(folder) -> str:
    name = (folder.name if folder else "").lower()
    parent = (folder.parent.name if folder and folder.parent_id else "").lower()
    for word, kind in (("circular", "circular"), ("polic", "policy"), ("report", "report_card"), ("certificate", "certificate")):
        if word in name or word in parent:
            return kind
    return Document.Kind.OTHER


def _pages(upload) -> int:
    """Page count of a PDF (0 for anything else)."""
    if not upload.name.lower().endswith(".pdf"):
        return 0
    upload.seek(0)
    data = upload.read()
    upload.seek(0)
    return data.count(b"/Type /Page") - data.count(b"/Type /Pages") or data.count(b"/Type/Page") - data.count(b"/Type/Pages")


def _default_grants(doc: Document, access: str, groups: list) -> None:
    rows = {
        "school": [(Subject.STAFF, True, True, False), (Subject.PARENTS, True, True, False), (Subject.STUDENTS, True, True, False), (Subject.ACCOUNTANT, True, True, False)],
        "staff": [(Subject.STAFF, True, True, False), (Subject.PARENTS, False, False, False), (Subject.STUDENTS, False, False, False), (Subject.ACCOUNTANT, True, True, False)],
        "parents": [(Subject.CLASS_TEACHER, True, True, True), (Subject.TEACHERS, True, True, False), (Subject.PARENTS, True, True, False), (Subject.STUDENTS, True, False, False), (Subject.ACCOUNTANT, False, False, False)],
        "private": [],
    }[access]
    for i, (subject, view, download, upload) in enumerate(rows):
        g = DocumentGrant.objects.create(document=doc, subject=subject, can_view=view, can_download=download, can_upload=upload, position=i)
        if subject != Subject.ACCOUNTANT and subject != Subject.STAFF and groups:
            g.class_groups.set(groups)


# ------------------------------------------------------------------ one document


def _canonical(doc: Document) -> list:
    groups, route = _scope(doc)
    if groups:
        return [Subject.CLASS_TEACHER, Subject.TEACHERS, Subject.PARENTS, Subject.STUDENTS, Subject.ACCOUNTANT]
    if route:
        return [Subject.STAFF, Subject.PARENTS, Subject.ACCOUNTANT]
    return [Subject.STAFF, Subject.PARENTS, Subject.STUDENTS, Subject.ACCOUNTANT]


def _implied(doc: Document, subject: str) -> tuple[bool, bool]:
    """(view, download) for a document with no matrix yet, from its audience."""
    a = doc.audience
    if subject in (Subject.PARENTS, Subject.STUDENTS):
        on = a in (Document.Audience.EVERYONE, Document.Audience.FAMILIES, Document.Audience.CLASSES, Document.Audience.STUDENT)
    else:
        on = a in (Document.Audience.EVERYONE, Document.Audience.STAFF, Document.Audience.FAMILIES, Document.Audience.CLASSES)
    return on, on


def permissions_of(doc: Document, by_grade: dict) -> list[dict]:
    groups, route = _scope(doc)
    scope = groups_label(groups, by_grade) if groups else (route.name if route else "")
    grants = {g.subject: g for g in doc.grants.all()}
    rows = [{"subject": "principal", "scope": "", "view": True, "download": True, "upload": True, "fixed": ["view", "download", "upload"]}]
    for subject in _canonical(doc) + [s for s in grants if s not in _canonical(doc)]:
        g = grants.get(subject)
        view, download = (g.can_view, g.can_download) if g else _implied(doc, subject)
        upload = g.can_upload if g else False
        fixed = sorted(FIXED_OFF.get(subject, set()))
        rows.append(
            {
                "subject": subject,
                "scope": "" if subject in (Subject.ACCOUNTANT, Subject.STAFF) else scope,
                "route": bool(route) and subject == Subject.PARENTS,
                "view": view,
                "download": download and view,
                "upload": upload and "upload" not in fixed,
                "fixed": fixed,
            }
        )
    return rows


def _audience_users(doc: Document) -> tuple[set, str]:
    """Who the document is for (to say "31 of 38 families"): guardians in its scope, else staff."""
    groups, route = _scope(doc)
    access = access_of(doc, _all_sections())
    if access["kind"] in ("school", "parents", "route", "students", "family"):
        links = StudentGuardian.objects.filter(student__is_active=True)
        if groups:
            links = links.filter(student__class_group__in=groups)
        elif route:
            from apps.transport.models import StudentTransport

            links = links.filter(student_id__in=StudentTransport.objects.filter(route=route, is_active=True).values("student_id"))
        elif doc.audience == Document.Audience.STUDENT and doc.student_id:
            links = links.filter(student_id=doc.student_id)
        return set(links.values_list("user_id", flat=True)), "families"
    staff = set(Membership.objects.filter(role__in=STAFF_ROLES, is_active=True).exclude(role__in=[Role.DRIVER, Role.ATTENDANT]).values_list("user_id", flat=True))
    return staff, "staff"


def _who(user_id, roles_by_user: dict) -> str:
    roles = roles_by_user.get(user_id, set())
    for role in (Role.PRINCIPAL, Role.ADMIN, Role.TEACHER, Role.ACCOUNTANT, Role.TRANSPORT_MANAGER, Role.PARENT, Role.STUDENT):
        if role in roles:
            return role
    return ""


def device_label(ua: str) -> str:
    """"Chrome · macOS" from a user agent; seeded and app entries are already short."""
    if not ua or len(ua) < 40:
        return ua
    browser = next((name for key, name in (("Edg/", "Edge"), ("EduFlow", "EduFlow app"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua), "Browser")
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "macOS"), ("Windows", "Windows"), ("Linux", "Linux")) if key in ua), "")
    return f"{browser} · {system}" if system else browser


def _log_rows(entries) -> list[dict]:
    entries = list(entries)
    users = {e.user_id for e in entries if e.user_id}
    roles: dict = {}
    for uid, role in Membership.objects.filter(user_id__in=users).values_list("user_id", "role"):
        roles.setdefault(uid, set()).add(role)
    return [
        {
            "id": str(e.id),
            "user": {"name": e.user.full_name, "initials": e.user.initials} if e.user else None,
            "role": _who(e.user_id, roles),
            "action": e.action,
            "at": e.created_at.isoformat(),
            "device": device_label(e.device),
        }
        for e in entries
    ]


def _detail(doc: Document) -> dict:
    by_grade = _all_sections()
    audience, unit = _audience_users(doc)
    downloaders = set(DocumentDownload.objects.filter(document=doc, action=DocumentDownload.Action.DOWNLOAD).values_list("user_id", flat=True))
    recent = DocumentDownload.objects.filter(document=doc).select_related("user").order_by("-created_at")[:3]
    return {
        **_row(doc, {doc.id: DocumentDownload.objects.filter(document=doc, action=DocumentDownload.Action.DOWNLOAD).count()}, by_grade),
        "reach": {"downloaded": len(downloaders & audience), "of": len(audience), "unit": unit},
        "permissions": permissions_of(doc, by_grade),
        "recent": _log_rows(recent),
        "mime": mimetypes.guess_type(doc.file.name)[0] if doc.file else None,
    }


def _doc_or_404(document_id) -> Document:
    doc = Document.objects.select_related("owner", "folder", "student").prefetch_related("grants__class_groups", "grants__route", "class_groups").filter(id=document_id).first()
    if doc is None:
        raise Http404
    return doc


class DocumentDetailView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def get(self, request, document_id):
        return Response(_detail(_doc_or_404(document_id)))


class PermissionRowSerializer(serializers.Serializer):
    subject = serializers.ChoiceField(choices=Subject.choices)
    view = serializers.BooleanField()
    download = serializers.BooleanField()
    upload = serializers.BooleanField()


class DocumentPermissionsView(SchoolAPIView):
    """Save the permissions matrix. Cells fixed by role policy can't change; downloading needs viewing."""

    allowed_roles = CONSOLE_ROLES

    def patch(self, request, document_id):
        doc = _doc_or_404(document_id)
        rows = PermissionRowSerializer(data=request.data.get("rows") or [], many=True)
        rows.is_valid(raise_exception=True)
        groups, route = _scope(doc)
        existing = {g.subject: g for g in doc.grants.all()}
        order = {s: i for i, s in enumerate(_canonical(doc))}
        changes = []
        for row in rows.validated_data:
            subject = row["subject"]
            for cell in FIXED_OFF.get(subject, set()):
                if row[cell]:
                    raise ValidationError({"rows": f"{Subject(subject).label} can't {cell}: that's fixed by role policy."})
            view, download, upload = row["view"], row["download"] and row["view"], row["upload"]
            g = existing.get(subject)
            if g is None:
                g = DocumentGrant.objects.create(document=doc, subject=subject, position=order.get(subject, 9), route=route if subject == Subject.PARENTS else None)
                if groups and subject not in (Subject.ACCOUNTANT, Subject.STAFF):
                    g.class_groups.set(groups)
            if (g.can_view, g.can_download, g.can_upload) != (view, download, upload):
                changes.append(f"{subject}: {'V' if view else '-'}{'D' if download else '-'}{'U' if upload else '-'}")
            g.can_view, g.can_download, g.can_upload = view, download, upload
            g.save(update_fields=["can_view", "can_download", "can_upload", "updated_at"])
        # Families reach it only through the matrix now; keep the audience in step for the staff app.
        if not DocumentGrant.objects.filter(document=doc, can_view=True).exists():
            doc.audience = Document.Audience.PRIVATE
        elif DocumentGrant.objects.filter(document=doc, subject=Subject.STAFF, can_view=True).exists() and doc.audience == Document.Audience.PRIVATE:
            doc.audience = Document.Audience.STAFF
        doc.save(update_fields=["audience", "updated_at"])
        _log(request, kind="document", ref=str(doc.id), document=doc, action=DocumentDownload.Action.PERMISSIONS)
        audit(request, "documents.permissions", target=doc, summary=f"{doc.title}: {'; '.join(changes) or 'no change'}", detail={"changes": changes})
        return Response(_detail(_doc_or_404(document_id)))


class DocumentFileView(SchoolAPIView):
    """Download from the console. Logged (user, time, device) and audited."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, document_id):
        doc = _doc_or_404(document_id)
        if not doc.file:
            raise Http404
        _log(request, kind="document", ref=str(doc.id), document=doc)
        return FileResponse(doc.file.open("rb"), as_attachment=True, filename=_display_name(doc))


class DocumentLogView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def get(self, request, document_id):
        doc = _doc_or_404(document_id)
        entries = DocumentDownload.objects.filter(document=doc).select_related("user").order_by("-created_at")
        total = entries.count()
        return Response({"title": doc.title, "total": total, "items": _log_rows(entries[:200])})


class FolderSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    parent_id = serializers.UUIDField(required=False, allow_null=True)
    locked = serializers.BooleanField(default=False)


class FoldersView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def post(self, request):
        data = FolderSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        parent = Folder.objects.filter(id=v.get("parent_id")).first() if v.get("parent_id") else None
        name = v["name"].strip()
        if not name:
            raise ValidationError({"name": "Name the folder."})
        if Folder.objects.filter(parent=parent, name__iexact=name).exists():
            raise ValidationError({"name": "There's already a folder with that name here."})
        last = Folder.objects.filter(parent=parent).order_by("-position").values_list("position", flat=True).first() or 0
        folder = Folder.objects.create(name=name, parent=parent, locked=v["locked"] or bool(parent and parent.locked), position=last + 1, created_by=request.user)
        audit(request, "documents.folder_create", target=folder, summary=name)
        return Response({"id": str(folder.id), "name": folder.name, "parent_id": str(parent.id) if parent else None, "locked": folder.locked, "count": 0}, status=status.HTTP_201_CREATED)


class FolderView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def patch(self, request, folder_id):
        folder = Folder.objects.filter(id=folder_id).first()
        if folder is None:
            raise Http404
        if "name" in request.data:
            name = str(request.data["name"]).strip()[:80]
            if not name:
                raise ValidationError({"name": "Name the folder."})
            folder.name = name
        if "locked" in request.data:
            folder.locked = bool(request.data["locked"])
            audit(request, "documents.folder_lock" if folder.locked else "documents.folder_unlock", target=folder, summary=folder.name)
        folder.save()
        return Response({"id": str(folder.id), "name": folder.name, "locked": folder.locked})

    def delete(self, request, folder_id):
        folder = Folder.objects.filter(id=folder_id).first()
        if folder is None:
            raise Http404
        if Document.objects.filter(folder_id__in=_descendants(folder)).exists() or folder.children.exists():
            raise ValidationError({"folder": "Only empty folders can be deleted."})
        audit(request, "documents.folder_delete", target=folder, summary=folder.name)
        folder.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


urlpatterns = [
    path("console/documents", DocumentsView.as_view()),
    path("console/documents/folders", FoldersView.as_view()),
    path("console/documents/folders/<uuid:folder_id>", FolderView.as_view()),
    path("console/documents/<uuid:document_id>", DocumentDetailView.as_view()),
    path("console/documents/<uuid:document_id>/permissions", DocumentPermissionsView.as_view()),
    path("console/documents/<uuid:document_id>/file", DocumentFileView.as_view()),
    path("console/documents/<uuid:document_id>/log", DocumentLogView.as_view()),
]
