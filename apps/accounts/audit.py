"""Write to the audit trail (``AuditLog``)."""

from .models import AuditLog


def audit(request, action: str, *, target=None, summary: str = "", detail: dict | None = None, module: str = "") -> AuditLog:
    """Record ``action`` by the request's user. ``target`` may be a model instance or ``(type, id)``."""
    if target is None:
        target_type, target_id = "", ""
    elif isinstance(target, tuple):
        target_type, target_id = target
    else:
        target_type, target_id = target._meta.model_name, str(target.pk)
    meta = getattr(request, "META", {}) or {}
    forwarded = (meta.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return AuditLog.objects.create(
        actor=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
        action=action,
        module=module or action.split(".")[0],
        target_type=target_type,
        target_id=str(target_id),
        summary=summary[:300],
        detail=detail or {},
        ip=forwarded or meta.get("REMOTE_ADDR") or None,
        device=(meta.get("HTTP_USER_AGENT") or "")[:200],
    )
