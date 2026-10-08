"""Production settings must refuse insecure configuration (ADR-014).

The subprocess tests import ``config.settings.prod`` for real, exactly as a deployed process would.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from eduflow.core.config_validation import production_problems, validate_production_settings

BACKEND_DIR = Path(__file__).resolve().parents[3]
STRONG_KEY = "k9#Qz7!vR2@mL5^tW8&yB3*nH6(pF1)xJ4_cD0+sG9=aE2-uK7"

SECURE = {
    "DEBUG": False,
    "SECRET_KEY": STRONG_KEY,
    "ALLOWED_HOSTS": ["api.eduflow.example"],
    "OTP_ECHO_DEV_CODE": False,
    "API_DOCS_ENABLED": False,
    "SESSION_COOKIE_SECURE": True,
    "CSRF_COOKIE_SECURE": True,
    "SECURE_SSL_REDIRECT": True,
    "DATABASES": {"default": {"PASSWORD": "a-long-random-db-password"}},
}


def test_secure_configuration_passes():
    assert production_problems(SECURE) == []
    validate_production_settings(SECURE)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"DEBUG": True}, "DJANGO_DEBUG"),
        ({"SECRET_KEY": "short"}, "DJANGO_SECRET_KEY"),
        ({"SECRET_KEY": "dev-only-insecure-change-me-" + "x" * 40}, "DJANGO_SECRET_KEY"),
        ({"SECRET_KEY": "a" * 60}, "DJANGO_SECRET_KEY"),
        ({"ALLOWED_HOSTS": []}, "DJANGO_ALLOWED_HOSTS"),
        ({"ALLOWED_HOSTS": ["*"]}, "wildcards"),
        ({"OTP_ECHO_DEV_CODE": True}, "OTP_ECHO_DEV_CODE"),
        ({"API_DOCS_ENABLED": True}, "API_DOCS_ENABLED"),
        ({"SESSION_COOKIE_SECURE": False}, "Secure"),
        ({"SECURE_SSL_REDIRECT": False}, "HTTPS"),
        ({"DATABASES": {"default": {"PASSWORD": "postgres"}}}, "database password"),
    ],
)
def test_each_insecure_setting_is_rejected(override, fragment):
    with pytest.raises(ImproperlyConfigured, match=fragment):
        validate_production_settings({**SECURE, **override})


def _run_prod(env_overrides: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "OTP_", "API_DOCS"))},
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_READ_DOT_ENV": "false",
        "DJANGO_SECRET_KEY": STRONG_KEY,
        "DJANGO_ALLOWED_HOSTS": "api.eduflow.example",
        "DATABASE_URL": "postgres://eduflow:a-long-random-db-password@127.0.0.1:5432/eduflow",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_prod_settings_refuse_to_start_with_debug():
    result = _run_prod({"DJANGO_DEBUG": "true"}, "check")
    assert result.returncode != 0
    assert "Insecure production configuration" in result.stderr
    assert "DJANGO_DEBUG" in result.stderr


def test_prod_settings_refuse_otp_echo_and_placeholder_key():
    result = _run_prod({"OTP_ECHO_DEV_CODE": "true", "DJANGO_SECRET_KEY": "change-me"}, "check")
    assert result.returncode != 0
    assert "OTP_ECHO_DEV_CODE" in result.stderr
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_prod_settings_pass_django_deploy_checks():
    result = _run_prod({}, "check", "--deploy", "--fail-level", "WARNING")
    assert result.returncode == 0, result.stdout + result.stderr
