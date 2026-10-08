# EduFlow

A multi-tenant School Operating System for web, mobile and (later) a Chrome extension.

| Part | Location | Status |
|---|---|---|
| Backend API (Django + DRF, PostgreSQL, Redis, Celery) | [`backend/`](backend/) | Phase 1: foundation |
| Mobile and web client (Expo) | `apps/mobile/` | Imported in Phase 6 from `origin/eduflow-new` |
| Local infrastructure | [`infra/docker/`](infra/docker/) | Compose stack |
| Documentation | [`docs/`](docs/) | Start with [ONBOARDING.md](docs/ONBOARDING.md) |

## Quick start

```bash
cp .env.example .env
docker compose up --build -d --wait
scripts/smoke-test.sh
```

- API health: http://127.0.0.1:8000/api/v1/health/ready
- API docs (dev only): http://127.0.0.1:8000/api/v1/docs

Details are in [docs/deployment/local-setup.md](docs/deployment/local-setup.md).

> **Security notice:** the former `master` branch contained malware and has been deleted. See [docs/SECURITY_INCIDENT.md](docs/SECURITY_INCIDENT.md).
