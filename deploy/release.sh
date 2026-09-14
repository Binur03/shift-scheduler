#!/usr/bin/env bash
# Release shift-scheduler to Google Cloud Run with a zero-downtime, reversible
# rollout:
#
#   1. Preflight   — clean git tree, full pytest suite, Alembic has one head.
#   2. Secrets     — create the inbound-email Basic Auth secrets if missing.
#   3. Backup      — on-demand Cloud SQL backup before any schema change.
#   4. Build       — immutable image tagged with the git SHA (Cloud Build).
#   5. Candidate   — deploy a new revision with NO traffic. Its container runs
#                    `flask db upgrade` on start (see Dockerfile), so the
#                    migration happens here; if it fails the revision never
#                    becomes ready and production keeps serving untouched.
#   6. Smoke test  — hit the candidate's tagged URL (health + webhook guards).
#   7. Promote     — shift 100% traffic to the candidate.
#
# The migration is additive, so the previous revision stays compatible with
# the migrated schema: rollback is a traffic switch (deploy/rollback.sh).
#
# Usage:   deploy/release.sh            (from the repo root, Git Bash / Linux / macOS)
# Options: SKIP_TESTS=1  SKIP_BACKUP=1  AUTO_APPROVE=1   (use sparingly)
set -euo pipefail

PROJECT="${PROJECT:-fransico-work}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-shift-scheduler}"
SQL_INSTANCE="${SQL_INSTANCE:-shift-scheduler-db}"
REPO="${REPO:-cloud-run-source-deploy}"
PYTHON="${PYTHON:-./.venv/Scripts/python.exe}"
[[ -x "$PYTHON" ]] || PYTHON="${PYTHON_FALLBACK:-python3}"

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
confirm() {
  [[ "${AUTO_APPROVE:-0}" == "1" ]] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || die "aborted by operator"
}
gc() { gcloud --project="$PROJECT" "$@"; }

cd "$(git rev-parse --show-toplevel)"

# --------------------------------------------------------------------------- #
bold "1/7 Preflight"
command -v gcloud >/dev/null || die "gcloud CLI not found"
[[ -z "$(git status --porcelain)" ]] || die "working tree is dirty — commit or stash first"
SHA="$(git rev-parse --short=12 HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${SHA}"
echo "project=$PROJECT region=$REGION service=$SERVICE image=$IMAGE"

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  "$PYTHON" -m pytest tests/ -q -p no:cacheprovider || die "tests failed"
fi

HEADS="$(FLASK_APP=app.py DATABASE_URL=sqlite:// "$PYTHON" -m flask db heads 2>/dev/null | grep -c '(head)' || true)"
[[ "$HEADS" == "1" ]] || die "expected exactly one Alembic head, found $HEADS (merge migrations first)"

PREVIOUS_REVISION="$(gc run services describe "$SERVICE" --region="$REGION" \
  --format='value(status.traffic[0].revisionName)')"
SERVICE_URL="$(gc run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
echo "currently serving: $PREVIOUS_REVISION at $SERVICE_URL"

# --------------------------------------------------------------------------- #
bold "2/7 Secrets"
ensure_secret() {
  local name="$1"
  if gc secrets describe "$name" >/dev/null 2>&1; then
    echo "secret $name exists"
  else
    # (not `head -c` on /dev/urandom: early pipe close + pipefail aborts the script)
    "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(30), end="")' \
      | gc secrets create "$name" --replication-policy=automatic --data-file=- >/dev/null
    echo "created secret $name (read it with: gcloud secrets versions access latest --secret=$name)"
  fi
}
ensure_secret inbound-email-username
ensure_secret inbound-email-password
gc secrets describe twilio-auth-token >/dev/null 2>&1 || die "secret twilio-auth-token is missing"

# --------------------------------------------------------------------------- #
bold "3/7 Cloud SQL backup"
if [[ "${SKIP_BACKUP:-0}" != "1" ]]; then
  gc sql backups create --instance="$SQL_INSTANCE" \
    --description="pre-release ${SHA} $(date -u +%Y-%m-%dT%H:%MZ)"
else
  echo "SKIP_BACKUP=1 — no backup taken"
fi

# --------------------------------------------------------------------------- #
bold "4/7 Build $IMAGE"
gc builds submit . --tag "$IMAGE"

# --------------------------------------------------------------------------- #
bold "5/7 Deploy candidate revision (no traffic; runs migrations on start)"
confirm "Deploy $SHA as a no-traffic candidate and migrate the production database?"
gc run deploy "$SERVICE" \
  --region="$REGION" \
  --image="$IMAGE" \
  --no-traffic \
  --tag=candidate \
  --update-env-vars="PUNCH_EARLY_MINUTES=${PUNCH_EARLY_MINUTES:-60}" \
  --update-secrets="TWILIO_AUTH_TOKEN=twilio-auth-token:latest,INBOUND_EMAIL_USERNAME=inbound-email-username:latest,INBOUND_EMAIL_PASSWORD=inbound-email-password:latest"

CANDIDATE_URL="$(gc run services describe "$SERVICE" --region="$REGION" --format=json \
  | "$PYTHON" -c 'import json,sys; print(next(t["url"] for t in json.load(sys.stdin)["status"]["traffic"] if t.get("tag")=="candidate"))')" \
  || die "could not find the candidate revision URL"
echo "candidate: $CANDIDATE_URL"

# --------------------------------------------------------------------------- #
bold "6/7 Smoke test candidate"
expect() {  # expect <status> <curl args...>
  local want="$1"; shift
  local got
  got="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@")"
  if [[ "$got" == "$want" ]]; then echo "  ok   $want  $*"; else echo "  FAIL got $got want $want  $*"; return 1; fi
}
SMOKE_OK=1
expect 200 "$CANDIDATE_URL/health"                                              || SMOKE_OK=0
expect 302 "$CANDIDATE_URL/admin/shifts"                                        || SMOKE_OK=0
expect 403 -X POST -d 'MessageSid=SMsmoke&From=%2B15550000000&Body=IN' "$CANDIDATE_URL/webhooks/twilio/sms" || SMOKE_OK=0
expect 401 -X POST -d 'text=smoke' "$CANDIDATE_URL/webhooks/email/smoke-token"   || SMOKE_OK=0
curl -s --max-time 30 "$CANDIDATE_URL/health" | grep -q '"ok"'                   || { echo "  FAIL health body"; SMOKE_OK=0; }

if [[ "$SMOKE_OK" != "1" ]]; then
  die "smoke test failed — candidate is NOT receiving traffic; production still on $PREVIOUS_REVISION"
fi

# --------------------------------------------------------------------------- #
bold "7/7 Promote"
confirm "Smoke test passed. Send 100% of traffic to $SHA?"
gc run services update-traffic "$SERVICE" --region="$REGION" --to-latest
curl -sf --max-time 30 "$SERVICE_URL/health" >/dev/null || die "production /health failed after promote — run deploy/rollback.sh $PREVIOUS_REVISION"

cat <<EOF

Released $SHA to $SERVICE_URL
Previous revision: $PREVIOUS_REVISION
Rollback (traffic only, schema stays):  deploy/rollback.sh $PREVIOUS_REVISION

One-time setup still required if not done yet:
  * Twilio number webhook:  deploy/configure_twilio_webhook.sh
  * SendGrid Inbound Parse: destination
      https://<inbound-email-username>:<inbound-email-password>@${SERVICE_URL#https://}/webhooks/email/<vendor token>
    (vendor tokens are shown on the admin Inbox page)
EOF
