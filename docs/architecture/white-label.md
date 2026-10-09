# White-Label: School Branding and Domains

Every school on the shared EduFlow platform can present its own identity: name, colours, logo and favicon. It can do so at its own address: a platform subdomain or a verified custom domain.

The plan has no white-label phase. This builds on the Phase 2–3 tenancy and school-profile foundations and the client contract, where the `School` type carries `branding` and the theme is "per-school branding loaded after sign-in" (CURRENT_STATE §3, §7).

- **Decision:** ADR-029.
- **Operations:** [deployment/white-label.md](../deployment/white-label.md).
- **Scope:** backend only. The frontend requirements are below. No trusted client source tree was available (see SECURITY_INCIDENT.md, Incident 2), so no client code was changed.

## What already existed, and what was added

| Need | Existing | Added |
|---|---|---|
| School name | `School.name`, `short_name` | Nothing: branding uses them (`display_name` = short name, or name) |
| Theme | Nothing | `SchoolBranding`: primary and secondary colour (or platform defaults), a computed black/white text colour for each, `version` |
| Logo, favicon | Object storage (ADR-009), nothing for branding | `BrandAsset`: validated images, stored privately, served by the API |
| Addresses | `X-School-Id` header, `ALLOWED_HOSTS` | School subdomains `<code>.<base>`; `SchoolDomain` for custom domains with DNS proof; host binding |
| Public school discovery | `GET /schools/lookup?code=` | `branding` added to it, plus `GET /branding/resolve?host=` |
| Invitations | `POST /invitations/preview` | `branding` of the inviting school added |

## Model

```
School (tenancy)            name, short_name, code (immutable), is_active
 ├── SchoolBranding         primary_color, secondary_color, logo → BrandAsset, favicon → BrandAsset, version
 ├── BrandAsset             kind, content_type, size, width, height, sha256, storage_key (internal)
 └── SchoolDomain           hostname (unique platform-wide), status pending|verified|disabled, is_primary,
                            verification_token, check_failures, suspended_by_platform
```

All three tables are school-owned, under the standard `tenant_rw` RLS policy, with same-school composite foreign keys: branding can only point at its own school's assets. Public reads use named, logged bypasses: `branding_public_read`, `branding_host_lookup` and `branding_domain_recheck`.

## Hosts

| Host | Resolves to | Rule |
|---|---|---|
| A platform host (`WHITE_LABEL_PLATFORM_HOSTS`, or the base domain itself) | the platform's default branding | — |
| `<code>.<WHITE_LABEL_BASE_DOMAIN>` | the active school with that code | Codes are immutable. Reserved labels (`www`, `api`, `admin`, …) and nested labels never resolve. |
| A custom domain | its school | Only while **verified** and the school is active |
| Anything else | nothing (`404`) | Unknown, pending, disabled, malformed, IPs, wildcards |

**Host binding.** A tenant request made on a school's own host acts in that school.
- Without `X-School-Id`, the host's school is used.
- A header naming any other school answers `403 tenant_forbidden`.
- Membership is checked exactly as before: a host never grants anything.
- Platform administration endpoints refuse to run on a school's host.

**Trusted hosts.** Django still validates every request's Host against `DJANGO_ALLOWED_HOSTS`, and production forbids wildcards. A subdomain pattern (`.eduflow.app`) is allowed. So:
- Schools' subdomains can serve the API directly.
- Custom domains normally serve only the **web app** (static), which calls the API on a platform host and gets its branding from `GET /branding/resolve?host=<its own hostname>`.
- Serving the API itself on a custom domain requires adding that domain to `DJANGO_ALLOWED_HOSTS` (an operator action, see the deployment guide).

## Domain lifecycle

```
add (school or platform) ──► pending ──(TXT proof found)──► verified ──(owner disables)──► disabled
                                 ▲                              │  ▲        (re-verify to re-enable)
                                 └────────── re-verify ─────────┘  │
                      platform suspend ──► disabled + suspended ───┘ (only the platform lifts it)
                      daily recheck: proof missing 3 times in a row ──► disabled (verification_lapsed)
```

- **Proof:** TXT record `_eduflow-challenge.<hostname>` = `eduflow-domain-verification=<token>`.
- **Who checks it:** `DOMAIN_VERIFIER`.
  - `DnsOverHttpsVerifier` uses a fixed https resolver, with no new dependency.
  - `DisabledVerifier` is the default: `/verify` answers `503` and EduFlow staff verify by hand after an out-of-band check (audited, with a note).
- **One hostname, one school:** platform-wide, case-insensitive (`409`).
- **Never registrable:** platform hosts, the base domain and anything under it.
- **Suspensions:** a school cannot verify, disable or remove a domain the platform suspended, since removing and re-adding would otherwise lift the suspension.

## Assets

- **Accepted:**
  - Logo: PNG, JPEG or WebP; up to 1 MB; 16–4096 px.
  - Favicon: PNG or ICO; square; up to 256 KB; 16–512 px.
- **Identified by their bytes** (signature and header), never by filename or declared type.
- **SVG is refused** (it can carry scripts), and so is GIF.
- **Stored** in the `branding` storage (the private bucket by default) under `branding/<school>/<asset id>.<ext>`. No part of the key comes from the client.
- **Served** by `GET /branding/assets/{id}`, only while the asset is an active school's current logo or favicon, with:
  - the exact `Content-Type`, `nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`;
  - `Cache-Control: public, max-age=31536000, immutable` (IDs never change), and an ETag of the content hash;
  - `Cross-Origin-Resource-Policy: cross-origin`, so web apps on other domains can display them.
- **Replacing** a logo creates a new asset. The old object is deleted after commit; its row stays as history.
- **Not re-encoded** (there is no image library). The serving headers are the defence against polyglot files; see the threat model.

## Caching and isolation

| Response | Caching |
|---|---|
| `GET /branding/resolve`, `GET /schools/lookup` | `public, max-age=60`; ETag `"<school>:<version>"` |
| `GET /branding` (authenticated) | `private, no-cache`, `Vary: Authorization, X-School-Id`; same ETag, `304` on `If-None-Match` |
| Assets | immutable, one year |
| Host → school (server) | 5 minutes (1 minute for misses), key `branding:host:<sha256(host)>`; dropped on every domain status change |

## Permissions and audit

| Who | Can |
|---|---|
| Every member (`school.read`) | Read their school's branding |
| `branding.manage` (school-wide; school admin, principal) | Change colours, upload or remove the logo and favicon |
| `domain.manage` (school-wide; school admin, principal) | List, add, verify, disable, remove domains, choose the primary one |
| Platform administrators | The same for any school through `/platform/...`, plus manual verification and suspension. No access to school data through the ordinary endpoints. |
| Anyone | Public branding (resolve, lookup, assets, invitation preview): public fields only |

Audit actions:
- `branding.updated`
- `branding.logo.replaced|removed` and `branding.favicon.replaced|removed`
- `branding.domain.added|verified|verification_failed|disabled|primary_set|removed|lapsed`

Each records the target school and `by_platform`.

## Frontend integration requirements (not implemented here)

For the shared Expo app and the web console, once a trusted client tree is available:

1. **Before sign-in:**
   - On the web, call `GET /branding/resolve?host=<location.hostname>`.
   - On mobile, use the branding in `GET /schools/lookup?code=` after the school code is entered.
   - Before the answer arrives, show neutral platform defaults, never the last school's branding.
2. **After sign-in:** `GET /branding` with the active `X-School-Id`. Cache it per **school ID and `version`**, never globally.
3. **Switching school:**
   - Fetch the target school's branding (with its header) *before* switching the UI.
   - Clear the theme along with the existing `queryClient.clear()`.
   - Never render one school's data under another's theme.
4. **Invitations:** theme the accept screen from `branding` in the preview response.
5. **Fallbacks:**
   - A missing logo shows the `display_name` text.
   - Missing colours already come back as platform defaults.
   - A failed request keeps the defaults.
6. **Text colour:** use `on_primary` and `on_secondary` for legibility.
7. **Asset URLs** are API paths: prefix them with the API base URL. They are immutable and can be cached indefinitely.
8. **No authorization changes:** role-based navigation and authorization stay exactly as they are; branding is presentation only.

## Independently branded store apps (requirements only)

The shared app is themed dynamically. A school wanting **its own** app in the App Store or Google Play would need:
- a separate bundle ID and application ID;
- its own signing keys (an Apple team or certificates, an Android keystore) and EAS build profiles;
- store listings, icons and screenshots;
- a developer account (the school's or EduFlow's);
- a review and release pipeline per app;
- push credentials per app (APNs and FCM);
- a fixed school code or domain baked into the build.

None of this exists, and the product documents do not require it. It is a commercial and operational decision.
