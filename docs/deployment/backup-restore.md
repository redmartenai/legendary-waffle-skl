# Backup and Restore

**Status:** a plan with working local commands. Production backup automation is set up with the first shared environment. A backup counts as real only once a restore of it has been tested.

## What must be backed up

| Data | Where | Method |
|---|---|---|
| Relational data (system of record) | PostgreSQL | Production: continuous WAL archiving plus base backups (pgBackRest, or the managed provider's PITR). Ad hoc: `pg_dump`. |
| Files | S3-compatible object storage | Bucket versioning plus replication or `restic`/`rclone` copies to a second location |
| Redis | Cache, broker | **Not backed up.** Holds no source-of-truth data by design (ADR-010). Jobs are retry-safe and rebuilt from the outbox. |
| Secrets | Platform secret store | Owned by the platform. Never stored in backups of the app. |

## Targets (to confirm per environment)

- **RPO:** 15 minutes or less for PostgreSQL (WAL archiving); 24 hours or less for objects.
- **RTO:** 4 hours or less for a full restore.
- **Restore drill:** quarterly, with the result recorded below.

## Local commands

These work against the Compose stack.

```bash
# Backup (custom format, compressed)
docker compose exec -T postgres pg_dump -U eduflow -d eduflow -Fc > eduflow-$(date +%Y%m%d-%H%M).dump

# Restore into a fresh database
docker compose exec -T postgres createdb -U eduflow eduflow_restore
docker compose exec -T postgres pg_restore -U eduflow -d eduflow_restore --no-owner < eduflow-YYYYMMDD-HHMM.dump

# Verify: run Django's migration check against the restored DB
docker compose exec -e DATABASE_URL=postgres://eduflow:${POSTGRES_PASSWORD}@postgres:5432/eduflow_restore \
  backend python manage.py migrate --check
```

Dumps contain personal data. Store them encrypted, never in git, and delete local copies after use.

## Restore drill log

| Date | Environment | Backup age | Restore time | Verified by | Result |
|---|---|---|---|---|---|
| — | — | — | — | — | Not yet run (no shared environment yet) |
