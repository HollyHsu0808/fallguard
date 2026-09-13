#!/usr/bin/env bash
# Usage:  N8N_WEBHOOK_URL=https://<team>.app.n8n.cloud/webhook-test/fallguard N8N_WEBHOOK_KEY=<secret> ./curl_test.sh fall_analysed
# Without an argument it sends the whole sequence with 3-second gaps.
set -e
: "${N8N_WEBHOOK_URL:?set N8N_WEBHOOK_URL}"; : "${N8N_WEBHOOK_KEY:?set N8N_WEBHOOK_KEY}"
cd "$(dirname "$0")"
send() { echo "→ $1"; curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$N8N_WEBHOOK_URL" \
  -H "Content-Type: application/json" -H "X-FallGuard-Key: $N8N_WEBHOOK_KEY" --data @"$1.json"; }
if [ -n "$1" ]; then send "$1"; exit 0; fi
for f in fall_analysed escalation_level2 escalation_level3 episode_closed; do send "$f"; sleep 3; done
