#!/usr/bin/env bash
# Point Twilio's incoming-SMS webhook at the Cloud Run service.
#
# Where the webhook belongs depends on how the number is configured:
#
#   * Number inside a Messaging Service (the A2P 10DLC setup) — the SERVICE's
#     InboundRequestUrl is used and the number's own SmsUrl is IGNORED unless
#     UseInboundWebhookOnNumber is true. Setting only the number therefore
#     looks correct and silently drops every inbound message: a worker texts
#     IN, nothing reaches the app, no punch is recorded and no error is
#     raised anywhere.
#   * Bare number, no service — the number's SmsUrl is used.
#
# This script configures whichever applies, based on whether
# TWILIO_MESSAGING_SERVICE_SID is set on the service.
#
# The URL must be byte-identical to PUBLIC_BASE_URL + the route path: Twilio
# signs the exact URL it calls, and the app validates signatures against
# PUBLIC_BASE_URL. Both are read from the live service so they cannot drift.
#
# Usage: deploy/configure_twilio_webhook.sh
# Options: AUTO_APPROVE=1
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
SERVICE_SID="$(env_value TWILIO_MESSAGING_SERVICE_SID)"
BASE_URL="$(env_value PUBLIC_BASE_URL)"
AUTH_TOKEN="$(gcloud secrets versions access latest --secret=twilio-auth-token --project="$PROJECT")"
WEBHOOK_URL="${BASE_URL%/}/webhooks/twilio/sms"

[[ -n "$ACCOUNT_SID" && -n "$BASE_URL" && -n "$AUTH_TOKEN" ]] \
  || { echo "Missing TWILIO_ACCOUNT_SID / PUBLIC_BASE_URL / twilio-auth-token"; exit 1; }

confirm() {
  [[ "${AUTO_APPROVE:-0}" == "1" ]] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
}

API="https://api.twilio.com/2010-04-01/Accounts/${ACCOUNT_SID}"

if [[ -n "$SERVICE_SID" ]]; then
  # ---------------------------------------------------------------- service
  echo "Messaging Service: $SERVICE_SID"
  echo "Webhook:           POST $WEBHOOK_URL"
  echo "(the number's own SmsUrl is ignored while it belongs to a service)"
  confirm "Set this as the Messaging Service's inbound webhook?"

  curl -sf -u "${ACCOUNT_SID}:${AUTH_TOKEN}" -X POST \
    "https://messaging.twilio.com/v1/Services/${SERVICE_SID}" \
    --data-urlencode "InboundRequestUrl=${WEBHOOK_URL}" \
    --data-urlencode "InboundMethod=POST" \
    --data-urlencode "UseInboundWebhookOnNumber=false" \
    | "$PYTHON" -c '
import json, sys
d = json.load(sys.stdin)
print("  inbound_request_url:", d.get("inbound_request_url"))
print("  use_inbound_webhook_on_number:", d.get("use_inbound_webhook_on_number"))'

  echo
  echo "Senders in this service:"
  curl -sf -u "${ACCOUNT_SID}:${AUTH_TOKEN}" \
    "https://messaging.twilio.com/v1/Services/${SERVICE_SID}/PhoneNumbers" \
    | "$PYTHON" -c '
import json, sys
nums = json.load(sys.stdin).get("phone_numbers", [])
print("  (none — add one in the console, or inbound will never arrive)") if not nums else None
for n in nums:
    print("  ", n.get("phone_number"), n.get("sid"))'
else
  # ----------------------------------------------------------------- number
  [[ -n "$SMS_NUMBER" ]] || { echo "Missing TWILIO_SMS_NUMBER"; exit 1; }
  ENCODED_NUMBER="$(printf '%s' "$SMS_NUMBER" | sed 's/+/%2B/')"

  PN_SID="$(curl -sf -u "${ACCOUNT_SID}:${AUTH_TOKEN}" "${API}/IncomingPhoneNumbers.json?PhoneNumber=${ENCODED_NUMBER}" \
    | grep -o '"sid": *"PN[0-9a-f]*"' | head -1 | grep -o 'PN[0-9a-f]*')" \
    || { echo "Could not look up $SMS_NUMBER on account $ACCOUNT_SID"; exit 1; }

  echo "Number:  $SMS_NUMBER ($PN_SID)"
  echo "Webhook: POST $WEBHOOK_URL"
  confirm "Set this as the number's incoming-SMS webhook?"

  curl -sf -u "${ACCOUNT_SID}:${AUTH_TOKEN}" -X POST "${API}/IncomingPhoneNumbers/${PN_SID}.json" \
    --data-urlencode "SmsUrl=${WEBHOOK_URL}" \
    --data-urlencode "SmsMethod=POST" \
    | grep -o '"sms_url": *"[^"]*"'
fi

echo
echo "Done. Text IN to ${SMS_NUMBER:-your campaign number} from a registered worker's phone to verify."
