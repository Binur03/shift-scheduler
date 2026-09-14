#!/usr/bin/env bash
# Point the Twilio number's incoming-SMS webhook at the Cloud Run service.
#
# The URL set here must be byte-identical to PUBLIC_BASE_URL + the route path:
# Twilio signs the exact URL it calls, and the app validates signatures
# against PUBLIC_BASE_URL. The script reads PUBLIC_BASE_URL from the live
# service so the two can't drift apart.
#
# Usage: deploy/configure_twilio_webhook.sh
set -euo pipefail

PROJECT="${PROJECT:-fransico-work}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-shift-scheduler}"
PYTHON="${PYTHON:-./.venv/Scripts/python.exe}"
[[ -x "$PYTHON" ]] || PYTHON="${PYTHON_FALLBACK:-python3}"

SERVICE_JSON="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json)"
env_value() {
  printf '%s' "$SERVICE_JSON" | "$PYTHON" -c '
import json, sys
env = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])
print(next((e.get("value", "") for e in env if e["name"] == sys.argv[1]), ""))' "$1"
}

ACCOUNT_SID="$(env_value TWILIO_ACCOUNT_SID)"
SMS_NUMBER="$(env_value TWILIO_SMS_NUMBER)"
BASE_URL="$(env_value PUBLIC_BASE_URL)"
AUTH_TOKEN="$(gcloud secrets versions access latest --secret=twilio-auth-token --project="$PROJECT")"
WEBHOOK_URL="${BASE_URL%/}/webhooks/twilio/sms"

[[ -n "$ACCOUNT_SID" && -n "$SMS_NUMBER" && -n "$BASE_URL" && -n "$AUTH_TOKEN" ]] \
  || { echo "Missing TWILIO_ACCOUNT_SID / TWILIO_SMS_NUMBER / PUBLIC_BASE_URL / twilio-auth-token"; exit 1; }

API="https://api.twilio.com/2010-04-01/Accounts/${ACCOUNT_SID}"
ENCODED_NUMBER="$(printf '%s' "$SMS_NUMBER" | sed 's/+/%2B/')"

PN_SID="$(curl -sf -u "${ACCOUNT_SID}:${AUTH_TOKEN}" "${API}/IncomingPhoneNumbers.json?PhoneNumber=${ENCODED_NUMBER}" \
  | grep -o '"sid": *"PN[0-9a-f]*"' | head -1 | grep -o 'PN[0-9a-f]*')" \
  || { echo "Could not look up $SMS_NUMBER on account $ACCOUNT_SID"; exit 1; }

echo "Number:  $SMS_NUMBER ($PN_SID)"
echo "Webhook: POST $WEBHOOK_URL"
read -r -p "Set this as the number's incoming-SMS webhook? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }

curl -sf -u "${ACCOUNT_SID}:${AUTH_TOKEN}" -X POST "${API}/IncomingPhoneNumbers/${PN_SID}.json" \
  --data-urlencode "SmsUrl=${WEBHOOK_URL}" \
  --data-urlencode "SmsMethod=POST" \
  | grep -o '"sms_url": *"[^"]*"'

echo "Done. Text IN to $SMS_NUMBER from a registered worker's phone to verify."
