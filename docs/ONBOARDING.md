# Developer Onboarding

Welcome to EduFlow. Read this first, then follow the links.

## 1. What we are building

EduFlow is a multi-tenant **School Operating System**: administration, academics, attendance, learning, assessment, communication, operations and, later, intelligence. One user can belong to several schools with several roles.

- **The backend is the security boundary.** Every endpoint enforces tenant, role, permission and data scope on the server.
- **We build in vertical slices.** The first slice is: admin, then school setup, teacher, class, attendance, and finally the parent sees the attendance. It runs end to end on real data before anything else is built ([IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)).

## 2. Read in this order

1. [CURRENT_STATE.md](CURRENT_STATE.md): what exists (the Expo client and its API contract) and the risks
2. [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md): ADR-001 to ADR-017
3. [architecture/overview.md](architecture/overview.md): system shape and module rules
4. [api/conventions.md](api/conventions.md), [database/conventions.md](database/conventions.md), [security/model.md](security/model.md)
5. [SECURITY_INCIDENT.md](SECURITY_INCIDENT.md): **mandatory**

## 3. Safety rules (non-negotiable)

- **Never check out, open or run the deleted `master` branch** (`fda7e16`). It contains malware. Use `eduflow-new` as the client baseline.
- Keep VS Code's `task.allowAutomaticTasks` **off**. Do not trust unknown workspaces.
- Never commit `.env`, real credentials or production data. `.env.example` holds placeholders only.
- Never log secrets or personal data (see [security/model.md](security/model.md#logging)).

## 4. Get running (about 10 minutes)

Follow [deployment/local-setup.md](deployment/local-setup.md):

```bash
cp .env.example .env
docker compose up --build -d --wait
scripts/smoke-test.sh
```

Then open http://127.0.0.1:8000/api/v1/docs.

## 5. How we work

- **Branches:** one per phase or slice (`phase-N/<name>`). Small, logically grouped commits. Pull requests into `main`.
- **Definition of done** for any feature: model, migration, API, authentication, authorization, tenant isolation, validation, error handling, tests, indexes, docs, logging, and audit where it applies. "The code exists" is not done.
- **Before pushing:** run the checks in [local-setup.md § Quality checks](deployment/local-setup.md#quality-checks-same-as-ci). CI runs the same checks plus the Docker smoke test, and a red CI blocks merge.
- **API changes:** regenerate and commit `docs/api/openapi.yaml`, and add a changelog line in [api/conventions.md](api/conventions.md).
- **New decisions:** append an ADR. Do not edit history.
- **Client bugs:** tracked in [client/known-issues.md](client/known-issues.md).

## 6. Where things are

| Path | What |
|---|---|
| `backend/config/` | Settings, URLs, Celery app |
| `backend/eduflow/core/` | Infrastructure: request IDs, logging, errors, health |
| `backend/eduflow/<module>/` | Domain modules (from Phase 2) |
| `infra/docker/compose.yaml` | Local stack |
| `scripts/smoke-test.sh` | End-to-end stack verification |
| `.github/workflows/backend.yml` | CI |

## 7. Getting help

Search the docs first. For anything security-related, contact the repository owner privately.
