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
├── routes/
│   ├── admin.py           # CRUD + per-shift & weekly dispatch (Blueprint: /admin)
│   ├── tasks.py           # /tasks/check-staffing for Cloud Scheduler
│   └── worker.py          # /accept/<token> + /decline/<token>
├── utils/
│   ├── alerts.py          # 24-48h understaffing sweep
│   ├── cli.py             # flask admin import-workers / check-staffing
│   └── sms.py             # Twilio WhatsApp integration + admin alerts
├── templates/
│   ├── base.html
│   ├── admin/{employees,jobs,shifts,shift_detail}.html
│   └── worker/{accept,confirmed,declined,full,closed}.html
├── requirements.txt
├── Dockerfile
├── .dockerignore
└── .env.example
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in DB credentials
export $(grep -v '^#' .env | xargs)
flask --app app init-db       # create tables (or use Alembic in prod)
python app.py                 # http://localhost:8080
```

## Concurrency

`/accept/<token>` (POST) opens a transaction, takes InnoDB row locks via
`with_for_update()` on the shift's assignment rows, counts accepted rows, and
only commits the acceptance if `accepted_count < required_headcount`. Concurrent
acceptors serialize on those locks, so a shift can never be over-filled. If full,
the transaction rolls back and a "Shift Full" view is returned (HTTP 409).

## Deploy to Cloud Run

```bash
gcloud run deploy shift-scheduler \
  --source . \
  --region us-central1 \
  --add-cloudsql-instances PROJECT:REGION:INSTANCE \
  --set-env-vars INSTANCE_CONNECTION_NAME=PROJECT:REGION:INSTANCE,DB_USER=app,DB_NAME=shift_scheduler \
  --set-secrets DB_PASS=db-pass:latest,SECRET_KEY=flask-secret:latest
```

When `INSTANCE_CONNECTION_NAME` is set, the app connects over the Cloud SQL
Unix socket (`/cloudsql/<name>`); otherwise it falls back to TCP for local dev.

## Twilio WhatsApp setup

1. Create a Twilio account and enable the WhatsApp sandbox (Messaging →
   Try it out → Send a WhatsApp message). Workers must join the sandbox once
   by texting the join code — fine for testing.
2. For production, register a WhatsApp sender on your own number via Twilio
   (requires Meta business verification, done through the Twilio console).
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`,
   and `PUBLIC_BASE_URL` (your Cloud Run URL, so links in messages work).
   Leave them blank and messages are logged instead of sent.

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

## Upgrading an existing database

The alert and reminder features add one column each. On an existing MySQL
database run:

```sql
ALTER TABLE shifts ADD COLUMN understaffed_alert_sent_at DATETIME NULL;
ALTER TABLE shift_assignments ADD COLUMN reminder_sent_at DATETIME NULL;
```

(Fresh databases created with `flask init-db` already include it.)

## Security

- **Admin login**: all `/admin` pages require the password set in
  `ADMIN_PASSWORD` (session-based, 12h lifetime, log out from the nav bar).
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

## Timezone

Set `APP_TIMEZONE` (e.g. `America/Denver`) so the 24–48h understaffing
window is computed in your business's local time — Cloud Run servers run
on UTC.
