# Shift Scheduler

Flask + MySQL (Cloud SQL) shift fulfillment system with token-based worker
acceptance, designed to run containerized on Google Cloud Run.

## How it works (weekly flow)

1. **Admin builds the week** — create jobs and shifts on the dashboard.
2. **Send week to workers** — one click on the Shifts page broadcasts every
   open shift in a 7-day window to all active workers via WhatsApp (Twilio).
3. **Workers respond** — each message contains a private link where the worker
   taps **"I'm available — accept"** or **"I'm not available"**. Accepts are
   first-come-first-served and can never over-fill a shift.
4. **Understaffing alerts** — an hourly sweep (Cloud Scheduler →
   `POST /tasks/check-staffing`) finds shifts starting in 24–48h that still
   lack workers and WhatsApps the admin (`ADMIN_WHATSAPP_NUMBER`) so they can
   adjust in time. Each shift alerts only once (re-armed if a worker is
   removed).
5. **Day-before worker reminders** — the same sweep reminds each confirmed
   worker ~24h before their shift starts (window configurable with
   `REMINDER_HOURS`), reducing no-shows. One reminder per worker per shift.
6. **Manager tools** — remove a worker from a shift (seat reopens), copy a
   whole week's shifts to the next week, urgency-sorted dashboard with
   staffing badges, and per-shift yes/no/waiting response counts.

## Directory structure

```
shift-scheduler/
├── app.py                 # Application factory + Gunicorn entrypoint
├── config.py              # Env-driven config; Cloud SQL pooling
├── extensions.py          # Shared SQLAlchemy instance (db)
├── models.py              # Employee, Job, Shift, ShiftAssignment
├── migrations/            # Alembic migrations (flask db upgrade)
├── routes/
│   ├── admin.py           # CRUD + per-shift & weekly dispatch (Blueprint: /admin)
│   ├── auth.py            # /login + /logout (admin password session)
│   ├── tasks.py           # /tasks/check-staffing for Cloud Scheduler
│   └── worker.py          # /accept/<token> + /decline/<token>
├── utils/
│   ├── alerts.py          # 24-48h understaffing sweep + reminders
│   ├── cli.py             # flask admin import-workers / check-staffing
│   └── sms.py             # Twilio WhatsApp integration (content templates)
├── templates/
│   ├── base.html
│   ├── admin/{login,employees,jobs,shifts,shift_detail}.html
│   └── worker/{accept,confirmed,declined,full,closed}.html
├── tests/                 # pytest smoke suite (python -m pytest tests/)
├── requirements.txt
├── Dockerfile
├── .dockerignore
└── .env.example
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in DB credentials (or DATABASE_URL=sqlite:///local.db)
flask --app app db upgrade    # create/upgrade tables (Alembic migrations)
python app.py                 # http://localhost:8080
```

Run the tests with `pip install -r requirements-dev.txt && python -m pytest tests/`.

The InnoDB concurrency suite (simultaneous Twilio webhooks hitting real
`SELECT … FOR UPDATE` row locks) only runs against a disposable MySQL
database whose name contains `test`:

```bash
TEST_MYSQL_URL="mysql+pymysql://user:pass@127.0.0.1:3306/shift_scheduler_test" \
  python -m pytest tests/test_punch_concurrency_mysql.py -v
```

## SMS check-in / check-out

Workers text **IN** when they arrive (from 60 min before a confirmed shift,
`PUNCH_EARLY_MINUTES`) and **OUT** when they leave. Twilio POSTs each text to
`/webhooks/twilio/sms`; the app verifies `X-Twilio-Signature` against
`PUBLIC_BASE_URL`, locks the worker's row then the assignment row
(`with_for_update()`), stores the punch in UTC, and replies via TwiML.
Every inbound text — including malformed ones — is logged in `inbound_sms`,
keyed by Twilio's MessageSid so webhook retries are never double-counted.
Timesheets export from the Workers page in each venue's local time.

## Vendor shift emails

Vendors email shift requests; SendGrid Inbound Parse posts them to
`/webhooks/email/<vendor token>` (Basic Auth + per-vendor token + DKIM for the
vendor's domain). Each line like
`09/20/2026 | 4:00 PM - 11:00 PM | Concessions Stand 12 | 6` becomes a
**draft** shift that an admin approves on the **Inbox** page before dispatch.
Unreadable lines are listed with the reason. See `utils/email_parser.py` for
the accepted formats.

## Releasing

```bash
deploy/release.sh                   # tests → SQL backup → build → no-traffic candidate (migrates) → smoke test → promote
deploy/rollback.sh [REVISION]       # traffic-only rollback; migrations are additive
deploy/configure_twilio_webhook.sh  # one-time: point the Twilio number at /webhooks/twilio/sms
```

## Database migrations

Schema is managed with Flask-Migrate (Alembic). After changing `models.py`:

```bash
flask --app app db migrate -m "describe the change"   # generate
# review the file in migrations/versions/, then:
flask --app app db upgrade                            # apply
```

The container runs `flask db upgrade` automatically on startup, so deploying
a new image also applies its migrations.

## Concurrency

`/accept/<token>` (POST) opens a transaction, takes InnoDB row locks via
`with_for_update()` on the shift's assignment rows, counts accepted rows, and
only commits the acceptance if `accepted_count < required_headcount`. Concurrent
acceptors serialize on those locks, so a shift can never be over-filled. If full,
the transaction rolls back and a "Shift Full" view is returned (HTTP 409).

## Deploy to Google Cloud Run

One-time setup (project `fransico-work`; region `us-central1` assumed):

```bash
gcloud config set project fransico-work
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com

# 1. Cloud SQL (MySQL 8) with automated backups
gcloud sql instances create shift-scheduler-db \
  --database-version=MYSQL_8_0 --tier=db-f1-micro --region=us-central1 \
  --backup --backup-start-time=09:00
gcloud sql databases create shift_scheduler --instance=shift-scheduler-db
gcloud sql users create app --instance=shift-scheduler-db --password='<DB_PASS>'

# 2. Secrets (generate with: python -c "import secrets; print(secrets.token_urlsafe(32))")
printf '%s' '<DB_PASS>'         | gcloud secrets create db-pass --data-file=-
printf '%s' '<random>'          | gcloud secrets create flask-secret --data-file=-
printf '%s' '<strong password>' | gcloud secrets create admin-password --data-file=-
printf '%s' '<random>'          | gcloud secrets create cron-secret --data-file=-
printf '%s' '<twilio token>'    | gcloud secrets create twilio-auth-token --data-file=-
```

Deploy (repeat this step for every update):

```bash
gcloud run deploy shift-scheduler \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --max-instances 2 \
  --add-cloudsql-instances fransico-work:us-central1:shift-scheduler-db \
  --set-env-vars "INSTANCE_CONNECTION_NAME=fransico-work:us-central1:shift-scheduler-db,DB_USER=app,DB_NAME=shift_scheduler,APP_TIMEZONE=America/Denver,ADMIN_WHATSAPP_NUMBER=+1...,TWILIO_ACCOUNT_SID=AC...,TWILIO_WHATSAPP_NUMBER=+1...,TWILIO_CONTENT_SID_INVITE=HX...,TWILIO_CONTENT_SID_REMINDER=HX...,TWILIO_CONTENT_SID_ALERT=HX...,PUBLIC_BASE_URL=https://YOUR-SERVICE-URL" \
  --set-secrets "DB_PASS=db-pass:latest,SECRET_KEY=flask-secret:latest,ADMIN_PASSWORD=admin-password:latest,CRON_SECRET=cron-secret:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest"
```

Notes:
- The first deploy prints the service URL — set `PUBLIC_BASE_URL` to it (and
  use it in the invite template's URL button) and redeploy.
- Migrations run automatically at container startup (`flask db upgrade`),
  so keep `--max-instances` small to avoid concurrent DDL on cold starts.
- When `INSTANCE_CONNECTION_NAME` is set, the app connects over the Cloud SQL
  Unix socket (`/cloudsql/<name>`); otherwise it falls back to TCP for local dev.
- Seed the roster once deployed: `flask admin import-workers workers.csv`
  (run locally against Cloud SQL via the Cloud SQL Auth Proxy, or add
  employees in the dashboard).

## Messaging: WhatsApp or SMS

`MESSAGING_CHANNEL` selects the transport:

- **`sms`** — plain text messages from `TWILIO_SMS_NUMBER`. No Meta approval
  and no message templates, so this is the **fastest path to production**
  (buy a number in the Twilio console; US senders need toll-free
  verification or A2P 10DLC registration — a console form, typically days).
- **`whatsapp`** (default) — see **TWILIO_SETUP.md**: Part A–C for sandbox
  testing, Part D for the production sender + the three Meta-approved
  templates (`TWILIO_CONTENT_SID_*`). When the SIDs are blank the app falls
  back to free-form bodies, which only deliver in the sandbox.

The two channels share all message wording and links, so you can launch on
SMS today and flip to WhatsApp later by changing `MESSAGING_CHANNEL` — no
code changes. Leave the Twilio credentials blank entirely and messages are
logged instead of sent.

## Understaffing alerts (Cloud Scheduler)

Set `ADMIN_WHATSAPP_NUMBER` (who gets alerted) and `CRON_SECRET` (shared
secret), then create an hourly job:

```bash
gcloud scheduler jobs create http staffing-check \
  --schedule "0 * * * *" \
  --uri https://YOUR-SERVICE-URL/tasks/check-staffing \
  --http-method POST \
  --headers X-Tasks-Auth=YOUR_CRON_SECRET
```

The window is configurable with `ALERT_WINDOW_MIN_HOURS` /
`ALERT_WINDOW_MAX_HOURS` (default 24–48). You can also run a sweep manually:
`flask admin check-staffing`.

## Security

- **Admin login**: all `/admin` pages require the password set in
  `ADMIN_PASSWORD` (session-based, 12h lifetime, log out from the nav bar).
  Login attempts are rate-limited (10/min per IP).
- **CSRF**: all admin/login forms carry a CSRF token (Flask-WTF). Worker
  token pages and the cron endpoint are exempt — they authenticate by
  unguessable URL token and shared-secret header respectively.
- **Worker pages** (`/accept/<token>`, `/decline/<token>`) are public by
  design — the 43-char random token in each worker's private link is the
  credential.
- **Scheduled tasks** (`/tasks/check-staffing`) require the `CRON_SECRET`
  header.
- Session cookies are HttpOnly + SameSite=Lax; in production they are also
  HTTPS-only, and the app trusts Cloud Run's proxy headers (ProxyFix) so
  generated URLs are https.
- Before deploying, set strong values for `SECRET_KEY`, `ADMIN_PASSWORD`,
  and `CRON_SECRET` (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`).

## Observability

- `/health` probes the database (`SELECT 1`) and returns 503 if it's
  unreachable. (The conventional `/healthz` path is reserved by Google's
  frontend on `*.run.app` domains — it never reaches the app.)
- Set `SENTRY_DSN` to enable error reporting via Sentry; `LOG_LEVEL`
  controls log verbosity (default INFO). Cloud Run captures stdout/stderr
  into Cloud Logging automatically.

## Timezone

Set `APP_TIMEZONE` (e.g. `America/Denver`) so the 24–48h understaffing
window is computed in your business's local time — Cloud Run servers run
on UTC.
