# One-Time Codes (phone sign-in)

Decisions: ADR-005, ADR-021. Code: `backend/eduflow/identity/otp/`.

## Architecture

```
POST /auth/otp/request ─► otp.service.request_code ─► SmsProvider.send(phone, message)
                                                          ├── ConsoleSmsProvider   (development)
                                                          ├── MemorySmsProvider    (tests)
                                                          ├── DisabledSmsProvider  (default)
                                                          └── <your gateway>       (production)
POST /auth/otp/verify  ─► otp.service.verify_code ─► session (token-lifecycle.md)
```

The service owns every security rule. An adapter only delivers a message. `OTP_SMS_PROVIDER` (a dotted path) selects the adapter.

## Rules

| Rule | Value | Setting |
|---|---|---|
| Code | 6 digits from `secrets.randbelow` | `OTP_LENGTH` |
| Expiry | 5 minutes | `OTP_TTL_SECONDS` |
| One-time use | `consumed_at` is set on success; a used challenge is dead | — |
| Attempts | 5 per challenge, then locked (even the right code fails) | `OTP_MAX_ATTEMPTS` |
| Resend cooldown | 30 seconds per number (`429` with `retry_after_seconds`), checked under a per-number advisory lock so parallel requests cannot all pass | `OTP_RESEND_SECONDS` |
| New code | invalidates the number's earlier open challenges | — |
| Rate limits | 3 requests / 10 min per number, 10 / hour per IP; 30 verifies / 10 min per IP, 10 per challenge | `RATE_LIMITS` |
| Concurrency | the challenge row is locked during verification, so parallel guesses are counted | — |

## Storage

| Field | Stored as |
|---|---|
| Code | `HMAC-SHA256(k_code, "<challenge_id>:<code>")`. The key is derived from `SECRET_KEY` with a purpose label, and the hash is compared with `hmac.compare_digest`. A database dump does not reveal codes, and a 6-digit code cannot be brute-forced offline without `SECRET_KEY`. |
| Phone number | `HMAC-SHA256(k_phone, phone)`, used only for cooldown and invalidation lookups |
| Account | `user_id`, or `NULL` when the number has no active account |

## Account enumeration

- `/auth/otp/request` answers identically for registered and unregistered numbers: `{challenge_id, expires_in, resend_in}`.
- For an unregistered or inactive number, a challenge is still created (so cooldowns behave the same), **no SMS is sent**, and the challenge can never be verified.
- Every verification failure is the same `401 invalid_code`.
- A gateway failure while sending to a registered number is logged and audited (`delivered: false`), and the response is the normal `200`, exactly as for an unknown number. A `503` there would reveal the account.
- When no gateway is configured (`DisabledSmsProvider`, `enabled = False`), **every** request gets `503`, before the number is looked up.
- A successful verification marks the phone as verified (`identity.phone.verified`).
- **Residual timing difference:** with a real gateway, a request for a registered number waits for the provider's API call; one for an unknown number does not. Removing it means sending asynchronously from a job, which needs the code to cross the broker. That is deferred, and recorded in the threat model.

## Never logged, never returned

- The code is never logged, audited or returned, except `dev_code`, which is returned only when **both** `DEBUG` and `OTP_ECHO_DEV_CODE` are true. Production refuses to start with `OTP_ECHO_DEV_CODE`.
- The log redactor masks `otp`, `code`, `dev_code` and `otp=…` patterns as a second line of defence.
- Phone numbers are not logged. The console adapter prints only the last two digits.
- `test_otp_never_appears_in_logs_or_audit` checks the real rendered logs and the audit table.

## Development

`config.settings.dev` uses `ConsoleSmsProvider`, which writes the message to the backend log:

```bash
docker compose logs backend | grep dev_sms
```

It refuses to run unless `DEBUG` is on, and production settings refuse it at startup. Alternatively set `OTP_ECHO_DEV_CODE=true` locally to receive `dev_code` in the response.

Tests use `MemorySmsProvider` and read `MemorySmsProvider.outbox`. **No test sends a real SMS.**

## Adding a production gateway (MSG91, Twilio, …)

1. Write an adapter with `send(phone: str, message: str) -> None`. Read credentials from settings or environment variables, never from code. Use HTTPS with short connect and read timeouts. Raise `SmsUnavailable` on any delivery failure; the user sees the normal response (and can request a new code after the cooldown), and the failure is logged and audited.
2. Do not log the message body, and do not put the phone number or message in exception text.
3. Set `OTP_SMS_PROVIDER=eduflow.identity.otp.providers_msg91.Msg91Provider` (for example) in the deployment's secret store.
4. For India, register the message template with the DLT platform; the message text is in `otp/service.py`.

Production uses `DisabledSmsProvider` until this is done: OTP sign-in returns `503`, and password sign-in keeps working.
