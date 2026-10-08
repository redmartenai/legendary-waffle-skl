import pytest
import yaml
from django.core.management import call_command


def test_schema_generates_and_validates(tmp_path):
    out = tmp_path / "openapi.yaml"
    call_command("spectacular", "--file", str(out), "--validate", "--fail-on-warn")
    schema = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.1")
    assert "/api/v1/health/live" in schema["paths"]
    assert "/api/v1/health/ready" in schema["paths"]


@pytest.mark.django_db
def test_schema_endpoint_served_when_docs_enabled(api_client):
    # API_DOCS_ENABLED is on in test settings and off by default in production (enforced).
    response = api_client.get("/api/v1/schema", HTTP_ACCEPT="application/vnd.oai.openapi+json")
    assert response.status_code == 200
