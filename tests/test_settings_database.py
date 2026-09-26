"""DATABASE_URL parsing for Postgres / Supabase (no network)."""

from eduflow.settings import _database_from_url


def test_supabase_url_gets_ssl_schema_and_health_checks():
    db = _database_from_url("postgresql://postgres.abc:s3cret@aws-0-ap-south-1.pooler.supabase.com:5432/postgres", "eduflow")
    assert db["HOST"] == "aws-0-ap-south-1.pooler.supabase.com" and db["NAME"] == "postgres"
    assert db["PASSWORD"] == "s3cret" and db["PORT"] == "5432"
    assert db["OPTIONS"]["sslmode"] == "require"
    assert db["OPTIONS"]["options"] == "-c search_path=eduflow"
    assert db["CONN_HEALTH_CHECKS"] is True
    assert "DISABLE_SERVER_SIDE_CURSORS" not in db


def test_query_params_pass_through_and_transaction_pooler():
    db = _database_from_url("postgres://u:p@db.example.com:6543/app?sslmode=verify-full&connect_timeout=5")
    assert db["OPTIONS"] == {"sslmode": "verify-full", "connect_timeout": "5"}
    assert db["DISABLE_SERVER_SIDE_CURSORS"] is True


def test_plain_local_postgres_has_no_ssl_or_schema():
    db = _database_from_url("postgres://eduflow:pw@localhost/eduflow")
    assert db["OPTIONS"] == {} and db["PORT"] == "5432"
