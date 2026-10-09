# White-Label Operations

How to run school subdomains and custom domains in an environment. Design: [architecture/white-label.md](../architecture/white-label.md), ADR-029.

**Not verified end to end.** Nothing below has been exercised against real DNS, certificates or a production edge. The code paths are tested with an in-memory DNS verifier and in-memory storage, plus a Docker smoke test against local RustFS. Every item marked *operator* must be set up and tested in the target environment.

## Settings

| Variable | Example | Meaning |
|---|---|---|
| `WHITE_LABEL_BASE_DOMAIN` | `eduflow.app` | Schools at `<code>.eduflow.app`. Empty: no subdomains. Production refuses wildcards or a single label. |
| `WHITE_LABEL_PLATFORM_HOSTS` | `app.eduflow.app` | Hosts that show the platform's own branding |
| `DJANGO_ALLOWED_HOSTS` | `api.eduflow.app,.eduflow.app` | Hosts the API answers on. `.eduflow.app` covers the school subdomains. No `*` (refused in production). |
| `DOMAIN_VERIFIER` | `eduflow.branding.verification.DnsOverHttpsVerifier` | Custom-domain proof. Default `DisabledVerifier`: manual verification by platform staff. The test verifier is refused in production. |
| `DOMAIN_VERIFICATION_DOH_URL` | `https://cloudflare-dns.com/dns-query` | A JSON DNS-over-HTTPS resolver. Must be `https://`. Outbound HTTPS from workers and the API to it must be allowed. |
| `BRANDING_DEFAULT_*` | | Platform name and colours |
| Storage (`STORAGE_*`) | | Brand images use the private bucket under `branding/`. Nothing is public in the bucket: the API serves the images. |

## School subdomains (*operator*)

1. DNS: a wildcard record `*.eduflow.app` pointing at the edge or load balancer.
2. TLS: a wildcard certificate for `*.eduflow.app`, renewed automatically.
3. Add `.eduflow.app` to `DJANGO_ALLOWED_HOSTS` if the API is served on the subdomains; otherwise only the web app is.
4. `HSTS includeSubDomains` is already on in production: every subdomain must serve HTTPS.

## Custom domains (*operator*)

1. The school adds the domain (`POST /domains`) and publishes the TXT record it is given.
2. The school calls `POST /domains/{id}/verify` with an automatic verifier. Without one, EduFlow staff check the record themselves and call `POST /platform/domains/{id}/verify` with a note.
3. The school points the hostname at the platform, typically a `CNAME` to an edge host such as `custom.eduflow.app`.
4. **TLS for the custom hostname is an edge concern and is not built:**
   - on-demand certificates (for example Caddy's `on_demand_tls` with an "ask" endpoint, or a managed "SSL for SaaS" product);
   - issue a certificate **only** for hostnames that `GET /api/v1/branding/resolve?host=<h>` answers with `200` (verified domains of active schools).
5. **What the domain serves:**
   - The web app (static) on the custom domain, calling the API on a platform host: the recommended set-up.
   - The API itself on the custom domain, which requires adding that hostname to `DJANGO_ALLOWED_HOSTS` and redeploying. Wildcard hosts are refused by design (host-header spoofing).
6. A daily beat job (`branding.recheck_domains`) re-checks verified domains and disables one whose proof has been missing three times in a row. It is audited as `branding.domain.lapsed`.

## Removing a school's domain

- `DELETE /domains/{id}` (school) or `/platform/domains/{id}` (staff). The domain stops resolving at once; the server cache entry is dropped.
- Ask the school to remove its `CNAME`, so a dangling record cannot be claimed elsewhere. Another school still could not verify it without the TXT proof.

## Checks after deploying

- `GET /api/v1/branding/resolve?host=<platform host>` gives `host_kind: platform`.
- `GET /api/v1/branding/resolve?host=<code>.<base>` gives the school.
- A pending custom domain gives `404`; after verification, `200`.
- `GET /api/v1/branding/assets/{id}` returns the image with `Content-Security-Policy: …sandbox`.
