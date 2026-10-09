# Invitations and Account Linking

Decision: ADR-025. Code: `backend/eduflow/invitations/`, with `identity/otp/service.py` (codes) and `identity/delivery.py` (SMS and email).

An invitation is the **verified** way a person's account joins a school as staff, a student or a guardian. It is also the **only** way an account is linked to a student or guardian record. Until it is accepted it grants nothing: no account, no membership, no role, no data scope.

## Lifecycle

```
            create (admin)                accept (recipient + code)
   ───────────────────────────► pending ──────────────────────────► accepted   (terminal)
                                │  ▲  │
                         revoke │  │  │ deadline passes (job every 15 min, or on access)
                                ▼  │  ▼
                          revoked  └─ expired ──revoke──► revoked              (terminal)
                         (terminal)  resend: new secret, new deadline
```

| Transition | Who | Rule |
|---|---|---|
| create → `pending` | member with the permissions below | The message must be handed to a provider, or nothing is saved (`503`) |
| `pending`/`expired` → `pending` (resend) | same | New secret (old link dies), new deadline, at most once per `INVITATION_RESEND_SECONDS` (60); the resender must be able to grant the invitation's roles, and the target must still be linkable |
| `pending`/`expired` → `revoked` | same | Never removes accounts or memberships |
| `pending` → `expired` | `invitations.expire_due` beat job, or lazily on access | After `INVITATION_TTL_HOURS` (72) |
| `pending` → `accepted` | the recipient | Valid secret + code sent to the invited address |

## Kinds

| Kind | Role(s) given | Linked on acceptance | Inviter also needs (school-wide) |
|---|---|---|---|
| `staff` | chosen by the inviter (at least one) | new `StaffProfile` (employee ID, type, designation, department, campus from the invitation) | `staff.create` |
| `student` | `student` | the existing `Student` record (must be active and unlinked) | `student.update` |
| `guardian` | `parent` | the existing `Guardian` record (must be unlinked), and through its `StudentGuardian` links, the children | `guardian.manage` |

Every kind also needs `invitation.manage`, `user.create` and `user.update` school-wide. The Phase 2 escalation rule applies: the inviter must hold every permission of every role the invitation gives.

## Recipient flow

```
link https://app…/invite#token=SECRET   (the secret stays in the browser: it is after '#')
  POST /invitations/preview       {token}            → school, kind, roles, masked address, deadline
  POST /invitations/verification  {token}            → code sent to the invited address → {challenge_id}
  POST /invitations/accept        {token, challenge_id, code}
        no bearer token  → new account for the invited address, signed in (session in the response)
        bearer token     → the signed-in account accepts (no new session)
```

- **The secret** is 32 random bytes (`secrets.token_urlsafe`). Only its SHA-256 is stored, and it is never returned by any management endpoint. It travels in request bodies, never in URLs.
- **Lookup without a school context.** `find_by_token` does one indexed lookup by digest under the explicit, logged RLS bypass (`invitation_lookup`) and returns only `(id, school_id)`. Everything else runs in the invitation's own school context, and the digest is checked again under the row lock, so a link superseded by a concurrent resend cannot slip through.
- **Verification** reuses the one-time-code rules from [otp.md](otp.md) with purpose `invitation` and the invitation as subject:
  - 6 digits, 5 minutes, 5 attempts, single use.
  - The code is sent to the invitation's own address, never to one the caller supplies.
  - It proves control of exactly that channel: an email code verifies only the email.
  - A sign-in code, or another invitation's code, never accepts this invitation.
  - Cooldowns are per invitation.

## Acceptance rules

All in one transaction, with the invitation row locked:

1. The invitation must be pending, unexpired, in an active school, and the code correct. A wrong code is counted and committed, but nothing else happens.
2. **The inviter is re-checked as they are now.** An inviter whose membership or account is inactive, or who no longer holds the required permissions, voids the invitation (`404 invitation_invalid`). Roles and links are applied with the inviter's current authority, never with the recipient's.
3. **The account:**

   | Request | Address already belongs to… | Outcome |
   |---|---|---|
   | anonymous | nobody | new account: the address is its verified identifier, no password; signed in |
   | anonymous | an account nobody can sign in to (no password, no verified identifier, e.g. created by a school's "add member") | that account is claimed: the address becomes verified; signed in |
   | anonymous | any other account | `409 account_exists`: sign in, then accept |
   | signed in | the signed-in account, or nobody | the signed-in account accepts; the address becomes its verified identifier if it had none |
   | signed in | a different account | `409` |

4. The inviter can never be the account that accepts (self-linking).
5. **Membership:**
   - An existing active membership in the school is reused, and roles are added to it.
   - A deactivated membership is **never** reactivated (`409`).
   - Otherwise a membership is created.
6. **Profile link:** goes through `people.services` (`link_student_account`, `link_guardian_account`, `create_staff`), which re-check that the record is unlinked, active, of the same school, and not the inviter's own.
7. The invitation becomes `accepted`, and the event is audited.

Existing credentials and unrelated memberships are never touched. A pending invitation is not visible to the recipient's account in any way.

**First password.** Accounts created by acceptance have no password. They can set one with `POST /auth/password/change` without `current_password`, but only from a sign-in session created in the last 15 minutes (an invitation acceptance or an OTP sign-in). Otherwise the answer is `403` and the person signs in by code again.

## Management endpoints

All school-scoped (`X-School-Id`):

- `GET /invitations` (filters `status`, `kind`, `channel`)
- `GET /invitations/{id}`
- `POST /invitations`
- `POST /invitations/{id}/resend`
- `POST /invitations/{id}/revoke`

Reading needs `invitation.read`; changing needs `invitation.manage`. Both apply school-wide only, because there are no narrower scope rules. Responses show a masked address hint (`+91********01`, `as***@example.test`), never the address, secret, digest or a code. Create and resend are limited per member (`invitation_manage_user`, 60/hour).

## Delivery

| Channel | Adapter | Dev | Tests | Production |
|---|---|---|---|---|
| phone | `OTP_SMS_PROVIDER` | console (redacted log line) | memory outbox | a gateway adapter; the console and memory adapters are refused |
| email | `EMAIL_BACKEND` | Django console backend (prints to stdout) | locmem | SMTP or a provider backend; console, locmem, file and dummy backends are refused |

- A disabled channel (`DisabledSmsProvider`, or the default `DisabledEmailBackend`) answers `503` before anything is saved.
- A provider failure rolls the operation back.
- `INVITATION_LINK_BASE` must be `https://` with the secret after `#` in production (enforced at startup).

## Rate limits

| Scope | Default | Applies to |
|---|---|---|
| `invitation_ip` | 30 / 10 min | preview, verification, accept: per client IP |
| `invitation_token` | 10 / 10 min | the same endpoints: per secret (hashed), across IPs |
| `invitation_manage_user` | 60 / hour | create, resend, revoke: per member |
| code cooldown | 30 s | per invitation |
| resend cooldown | 60 s | per invitation |

## Known limitations

- **Sending inside transactions.**
  - The message is sent inside the creating transaction. If the commit itself fails after the provider accepted the message, the recipient holds a link that does not work.
  - Verification sends while holding the invitation's row lock, bounded by the provider timeout (`EMAIL_TIMEOUT`, 10 s).
  - An outbox-based design (ADR-010) will remove both when notifications arrive.
- **Unlinking an account** from a student or guardian record has no API yet (a deliberate gap; it needs its own audited flow).
- **No invite-and-accept for verified accounts added directly** through `POST /memberships` (Phase 2). That flow remains for staff without profile linking.
