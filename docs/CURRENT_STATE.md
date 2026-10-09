# EduFlow: Current State (analysis, 2026-10-08)

This is a snapshot of what exists before the new backend is built. It is the input to
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) and [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md).
Nothing here is target state.

Paths like `src/api/client.ts` refer to the `origin/eduflow-new` branch unless stated otherwise.

---

## 0. Security notice: `origin/master` contains malware

> **Update 2026-10-09:** this repository (`legendary-waffle-skl`) also has the payload, on **both** `origin/master` (`217219d`) and `origin/eduflow-new` (`568f400`). The clean `eduflow-new` described below belongs to the client repository `miniature-pancake-app`. See [SECURITY_INCIDENT.md](SECURITY_INCIDENT.md), Incident 2.

`origin/master` (older Expo client, commit `fda7e16`) contains a supply-chain payload. Verified directly:

| File | What it does |
|---|---|
| `package.json` | `"start": "node api.js && expo start"`, so running `npm start` executes `api.js` first |
| `api.js` | 29 KB of obfuscated JavaScript (`_0x…` identifiers, hex strings, whitespace padding) |
| `public/fonts/fa-solid-700.fml` | Not a font. Obfuscated JavaScript. |
| `.vscode/tasks.json` | Hidden task `"eslint-check"` runs `node ./public/fonts/fa-solid-700.fml` with `runOn: folderOpen`, `reveal: never`, `hide: true` |
| `.vscode/settings.json` | `task.allowAutomaticTasks: true`, `terminal.integrated.hideOnStartup: always`, so the task runs silently when the folder is opened in VS Code |

Consequences:

- **Do not check out, open in an editor, or `npm install`/`npm start` `origin/master`.**
- Anyone who opened that branch in VS Code (with workspace trust) or ran `npm start` should treat that machine as compromised: rotate credentials, tokens and SSH keys, and run a malware scan.
- `origin/eduflow-new` and the current `main` were scanned for the same markers (`runOn`, `folderOpen`, `allowAutomaticTasks`, `.fml`, `node api.js`, `_0x` obfuscation, `child_process`, `eval(`). No matches. `eduflow-new`'s `start` script is plain `expo start`.
- Recommendation: delete `origin/master` from the remote once the owner confirms, and never merge from it.
- The local `.gitignore` on `main` lists `temp_auto_push.bat` and `branch_structure.json`, files that appear alongside this malware family. Neither file exists in the working tree.

---

## 1. Repository map

| Location | What it is | Status |
|---|---|---|
| `main` (this repo, checked out) | `.gitignore` only, one commit | Empty |
| `origin/eduflow-new` | Expo / React Native client, about 300 files and 63k lines of TS/TSX | **Primary client.** Clean on scan. |
| `origin/master` | Older Expo client, plus committed `dist-web/` and `dist-native/` build output | **Malicious, see §0.** Not to be used. |
| `Downloads/School Om nammah shivaya/eduflow-app` | Vite + React 19 prototype, using Zustand and localStorage | UI/product reference only |
| `Downloads/School Om nammah shivaya/EDUflow.docx` | Screen and access specification | Reference |
| `Downloads/EduFlow_Enterprise_Architecture_HLD_Free_Stack.pdf` | High-level architecture document | Reference (its NestJS backend suggestion is superseded by the Django decision) |
| `legendary-waffle-skl` | The Django backend the client was written against | **Unavailable.** Not used, and its API is not reconstructed. |

`eduflow-new/README.md` refers to a "main README" at `../README.md`. The client was originally a sub-folder of a monorepo that contained the missing backend.

## 2. Existing applications

One Expo codebase (`origin/eduflow-new`, Expo SDK 57, React Native 0.86, React 19.2) builds:

| App | How it is built | Users |
|---|---|---|
| EduFlow (iOS/Android) | `expo start` | Parent, student, teacher/staff, principal (mobile) |
| EduFlow Driver | `APP_VARIANT=driver` | Bus driver and attendant. GPS runs as a foreground service. |
| Web: role apps | `expo start --web` | Same role apps in the browser |
| Web: principal console | `/console`, web ≥1024px only | Principal |
| Web: platform admin | `/platform`, web only | EduFlow staff (`user.platform = true`) |

There is no backend, no tests (zero test files), no CI, and no Docker.

## 3. Existing frontend architecture

- **Routing:** Expo Router, file-based routes under `src/app/`.
- **Server state:** TanStack Query v5, about 89 files use it. Mutations invalidate broadly; switching school or role calls `queryClient.clear()`.
- **Client state:** Zustand.
  - `state/session.ts` holds tokens, user, memberships, the active school and the active role.
  - Other stores: `lastAccount`, `preferences`, `offline` (a list of saved study materials).
- **Persistence:**
  - Tokens are kept in `expo-secure-store` on native, and **in `localStorage` on web**.
  - The profile is kept in AsyncStorage.
  - No business data is persisted as source of truth. The client is API-driven, which is good.
- **HTTP:** `src/api/client.ts` wraps `fetch`.
  - Sends `Authorization: Bearer`, `X-School-Id` and an optional `Idempotency-Key`.
  - Makes a single-flight token refresh on a 401.
  - Parses a typed `ApiError` from an error envelope.
- **API surface:**
  - `src/api/endpoints.ts`: about 100 role-app functions.
  - `src/features/console/**/api.ts` and `src/features/platform/api.ts`: about 120 more.
  - Types are hand-written in `src/api/types.ts` (1,137 lines) plus per-feature types. There is no OpenAPI generation.
- **Realtime:** `centrifuge` (the Centrifugo client).
  - The token comes from `GET /realtime/connection`, which can answer `{enabled:false, poll_interval_seconds}`.
  - Channels are `personal:#<userId>` and `trip:<tripId>`.
  - Every realtime feature falls back to polling.
- **Other pieces:**
  - i18n: i18next, English and Hindi.
  - Theme: per-school branding loaded after sign-in.
  - Maps: react-native-maps on native, Leaflet on web.
  - Push: `expo-notifications`, registered via `POST /me/push-devices`.

## 4. Existing navigation

- **Root (`app/_layout.tsx`):**
  - `Stack.Protected` splits `(auth)` (signed out) from everything else (signed in).
  - Once signed in, `SignedInEffects` calls `GET /me`, registers for push and starts the personal realtime channel.
- **`app/index.tsx` routing:**
  - `must_change_password` goes to `/set-password`.
  - A platform user with no school goes to `/platform`.
  - Otherwise the user goes to the experience for their role (`ROLE_EXPERIENCE`):
    - `teacher`, `admin`, `accountant`, `transport_manager` → `/staff`
    - `parent` → `/parent`
    - `student` → `/student`
    - `driver`, `attendant` → `/driver`
    - `principal` → `/console` on wide web, `/principal` otherwise
- **Tabs:**
  - Parent: Home, Attendance, Results, Bus, More.
  - Student: Home, Schedule, Tasks, Results, Me.
  - Staff: Home, Classes, Attendance, Messages, Me.
  - Principal (mobile): Pulse, Approvals, Attendance, Broadcast.
- **Console sidebar:**
  - Dashboard
  - People: Students, Admissions, Staff
  - Academics: Academics, Timetable, Exams, Attendance
  - Operations: Fees, Transport, Approvals
  - Engage: Communication, Documents, Reports
  - Settings
- **All guards are client-side and cosmetic.**
  - Layouts redirect when `experience !== X`.
  - Shared routes (`class/[id]/*`, `trip/[id]`, `review/[id]`, `chat/*`) have no role check.
  - The backend must authorize everything.

## 5. Existing API assumptions (the contract the backend must meet or deliberately change)

Conventions:

| Aspect | What the client expects |
|---|---|
| Base path | `EXPO_PUBLIC_API_URL`, e.g. `http://host:8010/api/v1` |
| Trailing slashes | **None** (`/auth/otp/request`, `/classes/{id}/roster`) |
| IDs | Strings (UUIDs work) |
| Field naming | `snake_case` |
| Dates | `YYYY-MM-DD`. Times of day are school-local `HH:MM`. Timestamps are ISO-8601. |
| Money | Decimal strings |
| Errors | `{"error": {"code", "message", "fields"?, "retry_after_seconds"?}}`. `fields` may use dotted keys (`principal.phone`). |
| Tenant | `X-School-Id` header on every authenticated call. The school is never in the URL. |
| Pagination | Mixed: `{items, page, page_size, pages, total}`, limit-based "load more", and unpaginated `{items}` |
| Uploads | Multipart. Array fields are sent as JSON strings and booleans as `'true'`/`'false'`. |
| Downloads | Authenticated GET returning a blob. The server returns download paths in fields like `file` and `pdf`. |
| Idempotency | `Idempotency-Key` header (used only by fee checkout), or `client_id` in the body (attendance, chat, boarding, GPS fixes) |
| Responses | Mostly screen-shaped aggregates (`/console/dashboard`, `/staff/home`, `/students/{id}/summary`), not plain resources |

The **MVP-relevant endpoints** are below. They are listed in the order the slice exercises them.

| Endpoint | Request | Response |
|---|---|---|
| `GET /schools/lookup?code=` (public) | — | `School` |
| `POST /auth/password/login` | `{identifier, password, remember, school_code?}` | `AuthSession {access, refresh, user, memberships}` |
| `POST /auth/otp/request` | `{phone, school_code?}` | `{challenge_id, expires_in, resend_in, dev_code?}` |
| `POST /auth/otp/verify` | `{challenge_id, code}` | `AuthSession` |
| `POST /auth/token/refresh` | `{refresh}` | `{access, refresh?}` |
| `GET /me` | — | `{user, memberships: [{school, roles: [{role, title, department}]}]}` |
| `PATCH /me` | `{language?, preferences?}` | `{user}` |
| `GET /staff/classes` | — | `{subject, classes: StaffClassCard[]}` |
| `GET /teacher/classes` | — | `{classes: TeacherClass[]}` (legacy screens) |
| `GET /classes/{id}/roster?date=` | — | `ClassRoster {class, date, marked, marked_at, cutoff, locked, students: [{id, name, initials, roll_no, status}]}` |
| `POST /classes/{id}/attendance` | `{entries: [{student_id, status}], client_id, date?}`. **Sends exceptions only; a missing student means present.** | `AttendanceSummary` |
| `GET /parent/children[?detail=1]` | — | `{children: StudentCard[] \| ChildDetail[]}` |
| `GET /students/{id}/attendance?month=YYYY-MM` | — | `AttendanceMonth {month, days[], summary, year?}` |
| `GET /realtime/connection` | — | `{enabled:false, poll_interval_seconds}` is acceptable |

Here "class" in the client means a **homeroom section**: `{id, label: "Grade 9 · B", short_label: "9B"}`.

The full contract inventory, about 220 calls, is grouped by domain in Appendix A.

## 6. Existing authentication assumptions

- **Methods:**
  - Phone + 6-digit OTP. The number format is hard-coded to India (`+91`, 10 digits).
  - Identifier (email or mobile) + password, with "remember me" giving 30 days.
  - Invite link with a password of at least 10 characters (`/auth/invite/{token}`).
  - Forced password change (`/auth/password/change`, `must_change_password`).
  - SSO, 2FA and forgot-password are **not implemented**; the client only shows info sheets.
- **Tokens:**
  - A JWT-style access/refresh pair.
  - On a 401 the client refreshes once. If that fails it signs out locally.
- **Logout is local only:**
  - No server logout and no refresh-token revocation.
  - The push token is never unregistered (`unregisterPushDevice` is defined but never called).
- **Multi-school:**
  - One phone number can belong to several schools (`memberships[]`).
  - The client picks the active school and role locally.
  - **It sends the school (`X-School-Id`) but never the active role.**
- **`dev_code`:** the OTP request may echo the code back. The client shows it only under `__DEV__`, but on web it passes through the URL.
- **Platform staff:** `user.platform = true` with no school membership.

## 7. Existing data models / types

These come from the client types plus the Vite prototype's `src/data/types.ts`.

- **Tenant:**
  - School: `id, code, name, short_name, kind (school|junior_college|college), city, branding, campus (string), academic_year (string), term (string), languages`.
  - Organization appears only as a read-only string on the platform school detail.
  - **Campus is a free-text attribute, not an entity.**
  - There is no branch entity.
- **Academic structure:**
  - Grade → Section; the console calls sections "class groups" (`class_group_ids`).
  - Academic year with terms.
  - Subject: `{id, name, code, short}`.
  - Timetable `Period: {period, starts_at, ends_at, subject, teacher, room, class}`.
  - Timetable drafts and a publish step exist in the console.
- **People:**
  - Student card: `admission_no, roll_no, class, house, photo_url`.
  - Guardian: `name, relationship, phone`.
  - Staff: `employee_id, title, department`.
- **Attendance:**
  - Statuses: `present | absent | late | half_day | excused`.
  - Day statuses add `holiday | not_marked | upcoming`.
  - Registers have a `cutoff` and `locked`.
  - Student leave: `kind sick|family|other`, `half_day`, status `pending|approved|declined`.
  - Corrections go through approvals.
- **Elsewhere:** homework and submissions, assignments with milestones, mark sheets (`save`, then `submit`, then `moderate`, then `publish`), fees (invoices, payments, receipts), transport (routes, stops, trips, positions, boarding), chat (conversations, messages, meetings), announcements and circulars, documents with per-subject ACLs, approvals, and audit entries.
- **Prototype extras** (reference only): monitoring rules and thresholds, student risk weights, a teacher scorecard, admissions stages, and payroll runs.

## 8. Existing role definitions

- **Roles (`Role` union):** `parent, student, teacher, principal, admin, accountant, transport_manager, driver, attendant`. A user can hold several roles per school (`MembershipRole {role, title, department}`).
- **Permission model in the console** (`RolesPanel`): a matrix of module × action, plus a data scope per module.
  - Modules: `students, attendance, homework, assignments, exams, timetable, messages, documents, fees, transport, reports`.
  - Actions: `view, create, edit, delete, approve, export, download, publish`.
  - Scopes: `school | classes | own | none`.
  - Roles are either system roles (editable or locked by platform policy) or custom roles (cloned via `based_on`). Role keys are generated by the server.
- **Document ACLs** are separate from the matrix: per subject (`principal|staff|class_teacher|teachers|parents|students|accountant`), with `view/download/upload`.
- **The prototype matrix** (Principal, Admin, Teacher, Accountant, Parent, Student) gives a sensible default seed. Its scopes are: whole school; whole school · operations; assigned classes; finance; own children; own record.
- **Gaps:**
  - `admin`, `accountant` and `transport_manager` cannot open the console, because the client maps them to `staff`.
  - `ROLE_PRIORITY` puts `teacher` before `principal`, so a principal who also teaches lands in the staff app.

## 9. Existing screens (summary)

| Experience | Screens |
|---|---|
| Auth | login (OTP), otp, sign-in (password), welcome (school code lookup), invite, set-password |
| Parent | home, attendance (+ leave), results, bus, fees (mock gateway), homework (sign-off), documents and certificates, remarks, timetable, messages, notifications, children, more |
| Student | home, schedule, tasks, assignments, attendance, classes, exams, material, results, messages, me |
| Staff/Teacher | home (teacher or ops), classes, attendance, homework, assignments, marks, student profile, timetable/cover, leave, documents, messages, me |
| Principal (mobile) | pulse, approvals, attendance (+ cover, message absentees), broadcast |
| Driver | trip list, trip (start/end, roster/boarding, GPS, SOS) |
| Console (web) | dashboard, students and profile, admissions, staff, academics, timetable, exams, attendance, fees, transport, approvals, communication, documents, reports, settings (profile, calendar, roles, notifications, integrations, security, billing, audit) |
| Platform (web) | overview, schools list, 5-step school registration wizard, school detail |

## 10. Existing dependencies (client)

- **Runtime:**
  - Expo SDK 57 and React Native 0.86.3
  - expo-router, @tanstack/react-query 5, zustand 5, i18next and react-i18next
  - centrifuge 5, react-native-maps and leaflet
  - expo-secure-store, expo-notifications, expo-location and expo-task-manager
  - react-native-reanimated 4
- **Dev:** TypeScript 6.0, cross-env.
- **Package manager:** `packageManager` declares pnpm 9.15, but the repo ships a `package-lock.json` (npm). This mismatch needs to be resolved.
- **Missing:** a test runner (jest/vitest), lint configuration beyond `expo lint`, an OpenAPI type generator, and CI.

## 11. What can be reused

- **The whole `eduflow-new` client.** Navigation, design system (`src/ui`, `src/theme`), i18n, TanStack Query usage, session store, HTTP client and error handling are all solid foundations.
- **The client conventions:** error envelope, `X-School-Id`, no trailing slashes, snake_case, the `client_id` idempotency pattern. They are reasonable, and the backend adopts them.
- **The console's permission model** (module × action + data scope, system and custom roles). It matches the required User → Role → Permission → Data Scope model.
- **The platform onboarding flow** (school + year + grades + principal → credential slip) as the seed path for a new tenant.
- **The realtime design:** realtime is optional, falls back to polling, and uses per-user and per-resource channels.
- **The driver GPS queue** (batched, `client_id` per fix) as the pattern for offline queues.
- **From the prototype:** domain vocabulary, the default role matrix, and monitoring rules and thresholds for Phase 11.

## 12. What should be replaced or changed

| Item | Why | When |
|---|---|---|
| Hand-written `api/types.ts` | Drifts from the server. Generate types from OpenAPI (`openapi-typescript`), one domain at a time. | From the MVP slice onwards |
| Web tokens in `localStorage` | Any XSS can steal them. Use httpOnly, `SameSite` cookies with CSRF protection for the web console. | Before any production web deploy |
| `lib/download.ts` sends the bearer token to absolute URLs | Leaks tokens to object storage or third-party hosts. Only attach auth to `API_URL` URLs. | With the documents/files work |
| Teacher attendance `client_id` regenerated on every retry | Retries are not idempotent | MVP slice |
| `toMark()` collapses `excused`→`absent` and `half_day`→`late` | Re-saving a register silently destroys data | MVP slice |
| No offline queue for attendance | Marks are lost when there is no signal | Shortly after the MVP |
| Logout is local only, and the push token stays registered | The previous user's pushes keep arriving on the device. The refresh token stays valid. | Phase 2 server and client |
| Fees `confirmPayment {simulate}` with no `__DEV__` gate | The client decides whether a payment succeeded | Phase 10 (the server will never honour `simulate` outside test mode) |
| Driver SOS sends `lat/lng: null` | The incident has no location | Transport phase |
| GPS queue keeps retrying a permanently failing batch forever | The queue never drains | Transport phase |
| Inconsistent decision verbs (`reject` vs `decline`) | Contract ambiguity | Approvals phase. Standardise on `approve`, `decline`, `send_back` and accept `reject` as an alias. |
| `origin/master` | Malware | Now (owner action) |
| pnpm/npm mismatch | Builds are not reproducible | When the client is imported |

## 13. What is missing

Everything server-side. In particular:

- **Platform:** the backend itself, the database schema and migrations, Docker, CI, and tests (client or server).
- **Identity and security:** server-side authentication and authorization, the tenant model, audit, and file storage.
- **Async and realtime:** background jobs and a realtime server.
- **Contract:** an OpenAPI contract.
- **Integrations:** SMS, WhatsApp, email, payment gateway, push sending, maps.
- **Documentation:** architecture, security, API, database and deployment docs.
- **Specification gaps:**
  - An admin UI for creating subjects and explicit parents. The console creates the guardian inline with the student.
  - Explicit enrollment. The student's `class_id` is set at creation.
  - Campus as an entity.
  - Organisation management.

## 14. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Malware on `origin/master` | Developer machine compromise | §0. Delete the branch and audit machines that touched it. |
| The client contract is screen-shaped (about 220 calls, many aggregates) | Building it all is a large effort, and aggregates couple the backend to the UI | Canonical resource APIs in domain modules, plus a thin "experience" layer for client-shaped reads (ADR-006). Build only what the current slice needs. |
| The client never sends the active role | Ambiguity about which role authorizes a call | Authorize against the union of the user's roles in the school. Role-specific endpoint families check the role explicitly (ADR-004). |
| `X-School-Id` comes from the client | Cross-tenant access if trusted | Validate it against active memberships on every request. Querysets are always school-filtered (ADR-003). |
| Realtime: the client uses Centrifugo, but the requirement is Django Channels | Rework of either client or server | The MVP needs no realtime (`enabled:false` gives polling). Decide before Phase 9 (ADR-011, open). |
| No Docker, PostgreSQL or Redis on the development machine | Phase 1 cannot be run end-to-end locally | Install Docker Desktop, or rely on CI service containers for verification. See the implementation plan. |
| The scope of the client UI is far ahead of the backend | Pressure to build everything at once | Strict vertical slices. Unbuilt screens show error/empty states until their phase lands. |
| India-specific assumptions (`+91`, Indian academic year, DLT SMS) | Hard to expand to other countries later | Keep phone handling and calendars configurable per school on the server side |
| OTP needs an SMS provider | Sign-in is blocked in dev | A pluggable SMS adapter (a logging adapter in dev). `dev_code` is echoed only when `DEBUG` and an explicit flag are both set. |
| The missing `legendary-waffle-skl` may reappear with conflicting contracts | Duplicated effort | The new backend's OpenAPI document becomes the source of truth |

---

## Appendix A: Client contract inventory by domain

Counts are approximate.

| Domain | Representative endpoints | Phase |
|---|---|---|
| Auth/me | `/auth/otp/*`, `/auth/password/{login,change}`, `/auth/token/refresh`, `/auth/invite/{token}`, `/schools/lookup`, `/me`, `/me/push-devices` | 2 |
| Platform | `/platform/overview`, `/platform/schools[/{id}[/people[/{uid}/credentials]]]`, `/platform/schools/check-code` | 3 |
| Academics/console | `/console/context`, `/console/academics[/syllabus,/allocate]`, `/console/timetable[/draft…,/publish,/cover]` | 3–5 |
| People | `/console/students[/{id}…,/export,/message]`, `/console/staff[/export,/leave/{id}/decide]`, `/console/admissions/*`, `/parent/children`, `/student/me`, `/staff/me`, `/staff/students/{id}` | 4 |
| Timetable | `/students/{id}/timetable`, `/staff/timetable`, `/teacher/today`, `/principal/cover`, `/staff/covers/{id}/note` | 5 |
| Attendance | `/staff/classes`, `/teacher/classes`, `/classes/{id}/roster`, `/classes/{id}/attendance`, `/students/{id}/attendance`, `/students/{id}/leave`, `/principal/attendance`, `/console/attendance[/absentees,/alerts,/contacts,/handoff,/corrections/*]` | 6 |
| Learning | `/classes/{id}/homework`, `/homework/{id}/{done,signoff,submissions}`, `/assignments/*`, `/students/{id}/{homework,assignments,materials}`, `/teacher/{homework,assignments}` | 7 |
| Assessment | `/teacher/marks`, `/marksheets/{id}[/submit]`, `/students/{id}/{results,exams}`, `/exams/prep/{id}/toggle`, `/console/exams/*` | 8 |
| Communication | `/announcements[/estimate,/{id}/ack]`, `/notifications[/read]`, `/chat/*`, `/meetings/{id}/*`, `/console/communication/*`, `/realtime/connection` | 9 |
| Operations | fees (`/students/{id}/fees`, `/fees/*`, `/console/fees/*`), transport (`/transport/*`, `/driver/trips/*`, `/console/transport/*`), documents (`/students/{id}/documents`, `/staff/documents`, `/console/documents/*`), approvals (`/approvals/*`, `/console/approvals/*`), remarks, certificates | 10 |
| Intelligence/reports | `/principal/pulse`, `/console/dashboard`, `/console/reports/*`, `/console/search` | 11 |
| Settings/audit | `/console/settings[/profile,/calendar,/notifications,/roles…,/staff,/audit]` | 2–3 (roles, audit), later for the rest |
