# Client (eduflow-new) Known Issues and Fix Log

These issues were found in the Phase 0 analysis. Each one is fixed in the phase where the related backend behaviour exists, so a fix is never covered up by fake backend behaviour.

The client lives on `origin/eduflow-new` until it is imported into `apps/mobile/` (Phase 6, ADR-002).

| # | Issue | Where | Fix | Target phase | Status |
|---|---|---|---|---|---|
| C1 | Re-saving attendance turns `excused` into `absent` and `half_day` into `late` | `src/app/staff/attendance.tsx` (`toMark`) | Keep the full status set in the draft, and send unchanged statuses back as they were | 6 (attendance) | Open |
| C2 | Every attendance retry sends a new `client_id`, so retries are not idempotent | `src/app/staff/attendance.tsx` | Create one `client_id` per draft, reuse it on retry, and rotate it only after the server accepts. The server dedupes on it (ADR-008). | 6 | Open |
| C3 | Parent "payment succeeds/fails" simulation controls are not gated to development builds | `src/app/parent/fees.tsx` | Gate them behind `__DEV__`. The server never honours `simulate` outside test mode. | 10 (fees) | Open |
| C4 | `downloadFile`/`openFile` send the bearer token and `X-School-Id` to any absolute URL | `src/lib/download.ts` | Attach auth headers only when the URL's origin equals the API origin. Signed storage URLs need no auth (ADR-009). | 6 (first download in the slice), or 7 at the latest | Open |
| C5 | Sign-out does not revoke the refresh token or unregister push | `src/state/session.ts`, `src/features/notifications/push.ts` | Call `POST /auth/logout` (Phase 2 endpoint) and `DELETE /me/push-devices` before clearing local state. Sign out locally even if those calls fail. | 2 (endpoint) + 6 (client) | Open |

Each fix records the commit, the test that covers it, and the date here when it lands.
