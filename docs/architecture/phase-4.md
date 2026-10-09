# Phase 4: Invitations, Identity Lifecycle and Role-Based Access

Phase 4 adds the verified onboarding flow deferred by Phase 3. Staff, students and guardians are invited, prove control of their email or phone, and join their school with exactly the roles and record links the invitation carries. Phase 4 adds no second authorization system: invitations are built from the Phase 2 identity, OTP, RBAC, tenancy, RLS and audit pieces, and the Phase 3 people services.

Decision: ADR-025. Details: [security/invitations.md](../security/invitations.md).

## What changed

| Area | Change |
|---|---|
| New module `invitations` | `Invitation`, `InvitationRole`, services (lifecycle and acceptance), selectors, policies, API, expiry job |
| `identity` | `delivery.py`: one boundary for SMS and email, with a disabled-by-default email backend. OTP service generalised: sign-in and invitation codes share `_issue`/`_consume`, bound by purpose and subject. `OtpChallenge.phone_hash` → `address_hash` (+ `subject_id`). `invitation_login`. First passwords need a fresh session. |
| `people` | `link_student_account`, `link_guardian_account`. Student and guardian write APIs no longer accept `membership_id` (ADR-025). |
| `tenancy` / `authz` | Small public hooks: `create_membership`, `membership_with_roles`, `ensure_can_assign` |
| Catalogue | `invitation.read`, `invitation.manage` (55 permissions). School admin and principal hold them by default. |
| Settings | `INVITATION_TTL_HOURS`, `INVITATION_RESEND_SECONDS`, `INVITATION_LINK_BASE`, `EMAIL_*`; production refuses dev/test/dummy email backends and a non-https or non-fragment link base |

## Module boundaries

```
invitations.services ──► identity.otp.service      (codes: request_invitation_code / consume_invitation_code)
        │           ──► identity.delivery          (link delivery: SMS or email)
        │           ──► identity.services          (invitation_login)
        │           ──► tenancy.services/selectors (create_membership, membership_with_roles)
        │           ──► authz.services             (ensure_can_assign, assign_role — escalation rules)
        │           ──► people.services            (link_student_account, link_guardian_account, create_staff)
        └─ never writes another module's tables directly
```

`invitations` depends on everything below it; nothing depends on `invitations`.

## Role experiences (backend authorization)

These are **backend** guarantees, exercised by tests over the real endpoints. No role-specific frontend screens are part of this phase; the client builds its screens on `/me`, `/me/permissions` and the domain APIs.

| Experience | How it is enforced |
|---|---|
| **School Admin** | `school_admin` role: every permission, school-wide (locked role). Can invite any kind with any role. |
| **Principal** | `principal` role: everything except `role.delete`, school-wide. Can invite, but not with `school_admin` (the escalation rule: it lacks `role.delete`). Never unrestricted. |
| **Teacher** | `teacher` role + teaching `StaffProfile`. Section scope over students, enrollments and guardians of sections with an **active** assignment; own staff record; no invitations or roles. |
| **Student** | `student` role, linked to their `Student` record by an accepted student invitation. Self scope only. |
| **Parent / Guardian** | `parent` role, linked to their `Guardian` record by an accepted guardian invitation. Child scope over exactly the students linked to that record. A parent cannot gain a child by guessing IDs (`404`), by changing parameters (filters only narrow), or by sharing a membership: the link exists only after the guardian proves control of the invited address. |
| **Other school roles** | Accountant, HR Manager, Librarian, Transport, Hostel, Staff and Driver keep their Phase 2/3 grants. None holds `invitation.*`. |
| **Platform Admin** | `/platform/*` only. No implicit school access, and no invitation rights. |

**Role resolution is unchanged from Phase 2.**
- Every request resolves user → session → membership of the `X-School-Id` school → union of role grants (cached per `rbac_version`) → scopes.
- Role names, school IDs and permissions supplied by the client are never trusted (tested with forged headers).
- Switching schools switches the whole context, and acceptance grants roles only in the inviting school (tested).
- `/me` and `/me/permissions` already give clients what they need to choose an experience. Phase 4 adds no endpoint for it.

## Security boundaries added

1. **Public recipient endpoints** reach a school's table only by the secret's digest, under a named and logged bypass, then switch to that school's context.
2. **The inviter's authority** is re-checked at resend and at acceptance, so revoking an admin also voids their pending invitations.
3. **Channel proof:**
   - A code sent to the invited address is required to accept.
   - It verifies only that channel.
   - It is bound to that invitation and purpose.
4. **Account safety:**
   - Anonymous acceptance never attaches an account someone can already sign in to.
   - A signed-in user cannot accept an address owned by another account.
   - The inviter cannot accept their own invitation.
5. **Membership protections:** deactivated memberships are not reactivated. Last-admin and escalation rules apply through `authz.services`.

## Database

- `invitations_invitation` and `invitations_invitation_role` are school-owned, with the standard `tenant_rw` RLS policy and composite foreign keys for every reference: inviter, accepted membership, revoker, student, guardian, department, campus and role.
- Constraints:
  - Kind ↔ target consistency.
  - Status ↔ timestamps.
  - One pending invitation per (school, kind, channel, recipient), per student and per guardian.
  - Unique token digest.
- Indexes: `(school, status)`, and a partial index on `expires_at` for pending rows (the expiry job).
- `identity.0003` renames the OTP address column and closes open codes at deploy, since the digest changed.

## Tests

Phase 4 adds 95 tests, in `invitations/tests`:

- every state transition
- new, claimed and existing-account acceptance
- the account takeover cases
- code binding and attempt limits
- resend and revoke rules
- inviter-authority loss
- self-linking
- guardian/child and student/self scope after acceptance
- cross-school matrix
- RLS
- a three-thread concurrent acceptance (exactly one wins)
- provider failure and disabled channels
- rate limits
- redaction of secrets, codes and addresses from logs and audit
- query counts
- production configuration
- the five role experiences

The Docker smoke test runs a full guardian invitation against the running stack.

## Deferred

- Outbox-based delivery (removes sending inside transactions).
- Unlinking accounts from records.
- Bulk invitations.
- Email verification for accounts created by `POST /memberships`.
- Role-specific frontend screens (Phase 6 onward).
