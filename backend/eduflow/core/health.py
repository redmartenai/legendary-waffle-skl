"""Liveness and readiness probes (ADR-012).

* ``live``: the process is up and serving. It makes no I/O, so an outage elsewhere never makes an
  orchestrator kill healthy pods.
* ``ready``: dependencies are reachable (PostgreSQL, Redis, object storage). It returns 503 when one
  is not. The response names the failing check but never includes hosts, versions or exception
  text; those go to the logs.

Both are unauthenticated and contain nothing sensitive.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config as BotoConfig
from django.conf import settings
from django.db import connection
from drf_spectacular.utils import extend_schema, inline_serializer
from redis import Redis
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .logging import get_logger

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

log = get_logger(__name__)

_TIMEOUT_SECONDS = 2.0


def check_database() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def check_redis() -> None:
    client = Redis.from_url(
        settings.REDIS_URL, socket_connect_timeout=_TIMEOUT_SECONDS, socket_timeout=_TIMEOUT_SECONDS
    )
    try:
        client.ping()
    finally:
        client.close()


def storage_client() -> S3Client:
    """An S3 client for the configured (S3-compatible) object store, with short timeouts."""
    return boto3.client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        region_name=settings.STORAGE_REGION,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY or None,
        aws_secret_access_key=settings.STORAGE_SECRET_KEY or None,
        config=BotoConfig(
            connect_timeout=_TIMEOUT_SECONDS,
            read_timeout=_TIMEOUT_SECONDS,
            retries={"max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def check_storage() -> None:
    storage_client().head_bucket(Bucket=settings.STORAGE_BUCKET)


def readiness_checks() -> dict[str, Callable[[], None]]:
    checks: dict[str, Callable[[], None]] = {"database": check_database, "cache": check_redis}
    if settings.STORAGE_HEALTHCHECK_ENABLED:
        checks["storage"] = check_storage
    return checks


_STATUS = inline_serializer("HealthStatus", fields={"status": serializers.ChoiceField(["ok", "unavailable"])})
_READY = inline_serializer(
    "ReadinessStatus",
    fields={
        "status": serializers.ChoiceField(["ok", "unavailable"]),
        "checks": serializers.DictField(child=serializers.ChoiceField(["ok", "failed"])),
    },
)


class LiveView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(tags=["health"], summary="Liveness probe", responses={200: _STATUS})
    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})


class ReadyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(tags=["health"], summary="Readiness probe", responses={200: _READY, 503: _READY})
    def get(self, request: Request) -> Response:
        results: dict[str, str] = {}
        for name, check in readiness_checks().items():
            started = time.perf_counter()
            try:
                check()
                results[name] = "ok"
            except Exception as exc:  # any failure means "not ready". The detail goes to logs only.
                results[name] = "failed"
                log.warning(
                    "readiness_check_failed",
                    check=name,
                    error_type=type(exc).__name__,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
        ok = all(v == "ok" for v in results.values())
        return Response(
            {"status": "ok" if ok else "unavailable", "checks": results}, status=200 if ok else 503
        )
