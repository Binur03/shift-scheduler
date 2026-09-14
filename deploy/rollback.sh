#!/usr/bin/env bash
# Instant rollback: route 100% of traffic back to an earlier revision.
#
# Schema migrations are additive and are NOT reverted — older revisions ignore
# the new tables/columns, so a traffic switch is all a rollback needs. Only run
# `flask db downgrade` deliberately (it deletes vendor emails, SMS logs, and
# punch timestamps).
#
# Usage: deploy/rollback.sh [REVISION]     (no argument = list recent revisions)
set -euo pipefail

PROJECT="${PROJECT:-fransico-work}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-shift-scheduler}"

if [[ $# -eq 0 ]]; then
  gcloud run revisions list --project="$PROJECT" --region="$REGION" --service="$SERVICE" \
    --limit=10 --format='table(metadata.name,metadata.creationTimestamp,status.conditions[0].status)'
  echo
  echo "Re-run with a revision name: deploy/rollback.sh shift-scheduler-000NN-xyz"
  exit 0
fi

TARGET="$1"
read -r -p "Route 100% of $SERVICE traffic to $TARGET? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }

gcloud run services update-traffic "$SERVICE" --project="$PROJECT" --region="$REGION" --to-revisions="$TARGET=100"
URL="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
curl -sf --max-time 30 "$URL/health" && echo && echo "Rolled back to $TARGET — healthy."
