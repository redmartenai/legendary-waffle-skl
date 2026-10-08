# Local Setup

There are two ways to run the backend locally:

| Mode | Use it for |
|---|---|
| **A. Full Docker stack** (recommended) | Running the whole system: API, worker, beat, PostgreSQL, Redis, object storage |
| **B. Django on the host, infrastructure in Docker** | Fast test/lint loops, debugging in your IDE |

Both use the same `.env` file at the repository root.

## Prerequisites

- **Docker Desktop.** On Windows it needs WSL 2.
- **Python 3.13 and `uv`.** Needed for mode B and for running tests, lint and types on the host. Install `uv` with `pip install uv`, or see the uv docs.
- **Git.** The repository forces LF line endings (`.gitattributes`). Do not override this; CRLF breaks shell scripts inside Linux containers.

## First run

```bash
cp .env.example .env            # local-only values; .env is git-ignored
docker compose up --build -d --wait
scripts/smoke-test.sh           # verifies the stack end to end
```

`docker compose` is run from the repository root. The root `compose.yaml` includes `infra/docker/compose.yaml` and supplies `.env`.

### What starts

| Service | Purpose | Host address |
|---|---|---|
| `postgres` | PostgreSQL 17 | `127.0.0.1:5432` |
| `redis` | Cache, Celery broker and results | `127.0.0.1:6379` |
| `storage` | S3-compatible object storage (RustFS; see ADR-017) | API `127.0.0.1:9000`, console `127.0.0.1:9001` |
| `migrate` | One-shot: applies migrations and creates the private bucket, then exits | — |
| `backend` | Django dev server with auto-reload; the code is bind-mounted | `127.0.0.1:8000` |
| `worker` | Celery worker (queue `default`) | — |
| `beat` | Celery beat scheduler | — |

All ports bind to `127.0.0.1`. To let a phone on your Wi-Fi reach the API, set `API_BIND=0.0.0.0` in `.env`. Do this on trusted networks only.

### Useful URLs

- Health: http://127.0.0.1:8000/api/v1/health/live and `/api/v1/health/ready`
- API docs (Swagger UI, dev only): http://127.0.0.1:8000/api/v1/docs
- OpenAPI schema: http://127.0.0.1:8000/api/v1/schema
- Storage console: http://127.0.0.1:9001, signing in with `STORAGE_ACCESS_KEY` / `STORAGE_SECRET_KEY` from `.env`

### Everyday commands

```bash
docker compose ps                          # status and health
docker compose logs -f backend worker      # follow logs
docker compose exec backend python manage.py <command>
docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.test backend pytest
docker compose down                        # stop (data volumes kept)
docker compose down --volumes              # stop and DELETE all local data
```

## Mode B: Django on the host

```bash
docker compose up -d --wait postgres redis storage
cd backend
uv sync                                    # creates backend/.venv from uv.lock
uv run python manage.py migrate
uv run python manage.py ensure_storage_bucket
uv run python manage.py runserver
```

On the host, Django reads the repo-root `.env`. Its `DATABASE_URL` and `REDIS_URL` point at `127.0.0.1`. Containers get container-network addresses from Compose instead.

## Quality checks (same as CI)

```bash
cd backend
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run python manage.py makemigrations --check --dry-run
uv run pytest --cov                        # needs postgres + redis running
uv run pip-audit --strict
uv run python manage.py spectacular --file ../docs/api/openapi.yaml --validate --fail-on-warn
```

Tests need real PostgreSQL and Redis. They do not fall back to SQLite.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `env file .env not found` | `cp .env.example .env` |
| Port already in use | Change `POSTGRES_PORT`, `REDIS_PORT`, `STORAGE_PORT` or `API_PORT` in `.env` |
| `docker-credential-desktop` not found (Git Bash) | Add `C:\Program Files\Docker\Docker\resources\bin` to `PATH` |
| Readiness shows `"storage":"failed"` | Check `docker compose logs storage`, and that the `STORAGE_*` values match between `.env` and the running container. If credentials changed, recreate the container with `docker compose up -d --force-recreate storage`. |
| A changed `.env` has no effect | `docker compose up -d --force-recreate` |
