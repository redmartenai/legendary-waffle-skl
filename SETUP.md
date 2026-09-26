# EduFlow: running the services

EduFlow is two repos:
- `legendary-waffle-skl`, this repo: the Django API.
- `miniature-pancake-app`: the Expo app for phones and the web, which includes the principal's `/console` and the EduFlow team's `/platform`.

For local work you run up to three processes:

| Service | Port | Needed for |
| --- | --- | --- |
| Django API | 8010 | Everything |
| Expo (app + web) | 8130 (web) / 8081 (Metro) | The app |
| Centrifugo | 8001 | Live bus tracking and chat (optional; the app falls back to polling) |

## 1. Backend (Django)

Requirements: Python 3.13.

```bash
cd legendary-waffle-skl
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env          # then edit .env (see "Environment" below)
```

### Option A: local SQLite (simplest, for development and code review)

Leave `DATABASE_URL` and the `SUPABASE_S3_*` lines commented out in `.env`.

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo --reset      # two small demo schools (GHIS, SPS) + the EduFlow staff account
.venv/bin/python manage.py seed_design --reset    # Sunrise Public School (code SUNRISE), the full sample school
.venv/bin/python manage.py runserver 0.0.0.0:8010
```

`0.0.0.0` lets a phone on the same Wi-Fi reach the API. Use `127.0.0.1:8010` if you only use the browser on this machine.

### Option B: Supabase (Postgres + Storage)

Set these in `.env` (values come from your Supabase project; never commit them):

```bash
DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require
DATABASE_SCHEMA=eduflow
SUPABASE_S3_ENDPOINT=https://<ref>.storage.supabase.co/storage/v1/s3
SUPABASE_S3_REGION=ap-south-1
SUPABASE_S3_BUCKET=eduflow-media
SUPABASE_S3_ACCESS_KEY_ID=...
SUPABASE_S3_SECRET_ACCESS_KEY=...
```

Where to find them in the Supabase dashboard:
1. **Database URL:** Connect → Connection string → **Session pooler**. The direct `db.<ref>.supabase.co` URL is IPv6-only and many networks can't reach it.
2. **Bucket:** Storage → New bucket `eduflow-media`, with **Public** off.
3. **S3 endpoint, region and keys:** Storage → Settings → S3 Connection; click **New access key** for the key pair.
4. **Data API:** Project Settings → Data API → turn it off, or remove `public` from the exposed schemas. The tables must never be reachable through Supabase's REST API, because Django enforces who sees which school.

First run against a new project:

```bash
.venv/bin/python manage.py ensure_schema      # creates the private "eduflow" schema
.venv/bin/python manage.py migrate
```

Loading sample data: seeding straight into Supabase is very slow, because every row is a network round trip. Seed locally, then copy:

```bash
DATABASE_URL= SUPABASE_S3_ENDPOINT= .venv/bin/python manage.py migrate
DATABASE_URL= SUPABASE_S3_ENDPOINT= .venv/bin/python manage.py seed_demo --reset
DATABASE_URL= SUPABASE_S3_ENDPOINT= .venv/bin/python manage.py seed_design --reset
.venv/bin/python manage.py copy_sqlite_to_postgres --yes   # empties Supabase, copies every row, uploads the files
```

An empty `DATABASE_URL=` on the command line overrides `.env` for that one command, so it runs against SQLite.

Then start the API as in Option A: `.venv/bin/python manage.py runserver 0.0.0.0:8010`.

### Background jobs (optional)

```bash
.venv/bin/python manage.py deliver_announcements      # sends scheduled announcements; run every minute (cron)
.venv/bin/python manage.py run_scheduled_reports      # scheduled reports; run every few minutes
```

## 2. Realtime (Centrifugo, optional)

Install Centrifugo v5 (`brew install centrifugo` on macOS; the Windows binary is in `centrifugo/`), then:

```bash
centrifugo --config centrifugo/config.dev.json      # listens on :8001
```

`REALTIME_API_URL`, `REALTIME_API_KEY`, `REALTIME_TOKEN_SECRET` and `REALTIME_WS_URL` in `.env` must match that config. The values in `.env.example` are the development ones. Leave `REALTIME_API_URL` empty to run without Centrifugo.

## 3. App (Expo)

Requirements: Node 20+.

```bash
cd miniature-pancake-app
npm install
cp .env.example .env          # set EXPO_PUBLIC_API_URL (below)
npx expo start --clear --port 8130
```

- **Web:** press `w`, or open http://localhost:8130. At a window width of 1024px or more, principals get the web console.
- **Phone:** install **Expo Go** and scan the QR code. The phone must be on the same Wi-Fi as your computer.
- **The API URL is baked in when the app bundles.** After changing `.env` or `EXPO_PUBLIC_*`, restart Expo with `--clear`.
- **Restart Expo after code changes.** On some drives Metro doesn't notice edits.
- **Driver app variant:** `npm run start:driver`.

## Environment

### Backend `.env` (`legendary-waffle-skl/.env`)

| Variable | Example | What it does |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | long random string | Required in production |
| `DJANGO_DEBUG` | `1` | `0` in production |
| `DJANGO_ALLOWED_HOSTS` | `*` | Comma-separated hosts in production |
| `CORS_ALLOWED_ORIGINS` | `https://app.example.com` | Web app origins when `DJANGO_DEBUG=0` |
| `OTP_DEV_ECHO` | `1` | Returns the sign-in code in the API response (development only) |
| `WEB_URL` | `http://localhost:8130` | Link used in credential slips and invite links |
| `DATABASE_URL` | Supabase session pooler URL | Postgres; SQLite when empty |
| `DATABASE_SCHEMA` | `eduflow` | Postgres schema for Django's tables |
| `SUPABASE_S3_ENDPOINT` / `_REGION` / `_BUCKET` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | see Option B | File storage; local `media/` when empty |
| `REALTIME_API_URL` / `_API_KEY` / `_TOKEN_SECRET` / `_WS_URL` | see `.env.example` | Centrifugo |
| `EXPO_PUSH_ENABLED` / `EXPO_ACCESS_TOKEN` | `0` / empty | Push notifications (needs an EAS build) |
| `TRACCAR_SHARED_SECRET` | random string | GPS device ingestion |

### App `.env` (`miniature-pancake-app/.env`)

| Variable | Example | What it does |
| --- | --- | --- |
| `EXPO_PUBLIC_API_URL` | `http://192.168.1.7:8010/api/v1` | The API. Use your computer's Wi-Fi IP (`ipconfig getifaddr en0` on macOS), not `localhost`, so phones can reach it |
| `EXPO_PUBLIC_WEB_URL` | `http://192.168.1.7:8130` | Lets the phone app open the web dashboard |
| `EXPO_PUBLIC_MAP_TILE_URL` | OpenStreetMap by default | Map tiles |
| `GOOGLE_MAPS_ANDROID_API_KEY` / `GOOGLE_MAPS_IOS_API_KEY` | empty | Native maps in device builds |

## Sample sign-ins (sample data only)

| Who | Mobile | Sign in with |
| --- | --- | --- |
| EduFlow staff (platform admin, `/platform`) | +91 90000 00000 | password `eduflow-admin` |
| Principal, Sunrise (`/console`) | +91 98400 00001 | password `sunrise-principal` |
| Teacher (Priya Menon) | +91 98400 00010 | one-time code |
| Parent (Rahul Sharma) | +91 98450 34521 | one-time code |
| Student (Aarav Sharma) | +91 98400 00003 | one-time code |
| Driver, Route 07 | +91 98400 00044 | one-time code |

School code: `SUNRISE`. With `OTP_DEV_ECHO=1` the one-time code comes back in the API response and is filled in for you in development.

## Tests

```bash
cd legendary-waffle-skl && .venv/bin/python -m pytest -q      # always local SQLite; never touches Supabase
cd miniature-pancake-app && npx tsc --noEmit -p .
```

## Common problems

| Symptom | Fix |
| --- | --- |
| Phone: "You're offline or the server can't be reached" | Django must run on `0.0.0.0:8010`; `EXPO_PUBLIC_API_URL` must use your Wi-Fi IP; allow Python through the macOS firewall; restart Expo with `--clear` |
| "That port is already in use" | An old server is still running: `lsof -nP -iTCP:8010 -sTCP:LISTEN`, then stop that process |
| Sign-in says "Something went wrong" after a code update | Expo is serving an old bundle: restart it with `--clear` and hard-reload the page |
| HTTP 429 on sign-in | Sign-in codes are rate-limited; wait a few minutes |
| Bus positions look stale | Live trip data is fixed when the sample data is created: re-run `seed_design --reset` before a demo |
