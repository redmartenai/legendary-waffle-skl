import io

import boto3
import pytest
from botocore.stub import Stubber
from django.core.management import call_command
from django.core.management.base import CommandError

from eduflow.core.management.commands import ensure_storage_bucket


@pytest.fixture
def stubbed(monkeypatch, settings):
    settings.STORAGE_BUCKET = "eduflow-private"
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    monkeypatch.setattr(ensure_storage_bucket, "storage_client", lambda: client)
    with Stubber(client) as stub:
        yield stub


def _run() -> str:
    out = io.StringIO()
    call_command("ensure_storage_bucket", stdout=out)
    return out.getvalue()


def test_creates_missing_bucket_without_acl_or_policy(stubbed):
    stubbed.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    # Exactly these params: no ACL, so the bucket stays private.
    stubbed.add_response("create_bucket", {}, {"Bucket": "eduflow-private"})
    assert "created (private)" in _run()
    stubbed.assert_no_pending_responses()


def test_existing_bucket_is_left_alone(stubbed):
    stubbed.add_response("head_bucket", {}, {"Bucket": "eduflow-private"})
    assert "already exists" in _run()
    stubbed.assert_no_pending_responses()


def test_access_denied_is_an_error_not_a_create(stubbed):
    stubbed.add_client_error("head_bucket", service_error_code="403", http_status_code=403)
    with pytest.raises(CommandError, match="cannot access"):
        _run()
