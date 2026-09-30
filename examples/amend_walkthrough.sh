#!/usr/bin/env bash
# License-scope amendment walkthrough for the Temporary Event Radio
# Spectrum API. Uses the same guard band as the main walkthrough (2 MHz).
#
# Scenario: device A is approved at the origin; after relocating the
# equipment the operator atomically replaces its coverage circle/band/
# window with one new revision. Old revisions still show the old scope and
# later approvals adjudicate against the new scope.
#
# Usage: ./examples/amend_walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

submit() {
  # submit <name> <cx> <cy> <radius> <freq_low> <freq_high> -> new id
  post /api/applications "{
    \"name\":\"$1\",
    \"circle\":{\"cx\":$2,\"cy\":$3,\"radius\":$4},
    \"frequency\":{\"low\":$5,\"high\":$6},
    \"time_window\":{\"start\":\"2026-10-01T08:00:00Z\",\"end\":\"2026-10-01T12:00:00Z\"}
  }" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'
}

echo "== 1. submit and approve A at the origin (revision 1) =="
A=$(submit "Stage A" 0 0 1000 100 110)
echo "A id: $A"
post "/api/applications/$A/approve" '{"expected_revision":0}' | J

echo "== 2. submit and approve B far away on [100,110] (revision 2) =="
B=$(submit "Stage B" 9000 0 1000 100 110)
echo "B id: $B"
post "/api/applications/$B/approve" '{"expected_revision":1}' | J

echo "== 3. amend A: relocate equipment to (4000,0), same band/window =="
echo "   (own old scope excluded; new circle stays clear of B at x=9000)"
post "/api/applications/$A/amendments" "{
  \"expected_revision\":2,
  \"reason\":\"equipment relocated\",
  \"circle\":{\"cx\":4000,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":100,\"high\":110},
  \"time_window\":{\"start\":\"2026-10-01T08:00:00Z\",\"end\":\"2026-10-01T12:00:00Z\"}
}" | J

echo "== 4. GET A now serves the amended scope (id/status unchanged) =="
curl -s "$BASE/api/applications/$A" | J

echo "== 5. old revision 2 still renders A's ORIGINAL parameters =="
curl -s "$BASE/api/revision/2" | python3 -m json.tool

echo "== 6. later single approval reuses the freed space at the origin =="
C=$(submit "Stage C" 0 0 1000 100 110)
echo "C id: $C (preview should show no conflicts)"
post "/api/applications/$C/preview" | J
post "/api/applications/$C/approve" '{"expected_revision":3}' | J

echo "== 7. failure: amend A onto B's exact site -> 409 interference =="
echo "   old scope stays effective, no revision consumed (status line:"
curl -s -i -X POST "$BASE/api/applications/$A/amendments" \
  -H 'Content-Type: application/json' \
  -d "{
    \"expected_revision\":4,
    \"circle\":{\"cx\":9000,\"cy\":0,\"radius\":1000},
    \"frequency\":{\"low\":100,\"high\":110},
    \"time_window\":{\"start\":\"2026-10-01T08:00:00Z\",\"end\":\"2026-10-01T12:00:00Z\"}
  }" | sed -n '1p'
post "/api/applications/$A/amendments" "{
  \"expected_revision\":4,
  \"circle\":{\"cx\":9000,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":100,\"high\":110},
  \"time_window\":{\"start\":\"2026-10-01T08:00:00Z\",\"end\":\"2026-10-01T12:00:00Z\"}
}" | J || true

echo "== 8. failure: stale expected revision -> 409 stale_revision =="
post "/api/applications/$A/amendments" "{
  \"expected_revision\":0,
  \"circle\":{\"cx\":4000,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":100,\"high\":110},
  \"time_window\":{\"start\":\"2026-10-01T08:00:00Z\",\"end\":\"2026-10-01T12:00:00Z\"}
}" | J || true

echo "== 9. failure: invalid new scope -> 422 validation_failed =="
post "/api/applications/$A/amendments" '{
  "expected_revision":4,
  "circle":{"cx":4000,"cy":0,"radius":-5},
  "frequency":{"low":200,"high":100},
  "time_window":{"start":"2026-10-01T10:00:00Z","end":"2026-10-01T08:00:00Z"}
}' | J || true

echo "== 10. current revision unchanged at 4; A still at its amended site =="
curl -s "$BASE/api/revision/current" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision:", d["current_revision"], "effective:", d["effective_application_ids"], "amendments:", [e["revision"] for e in d["amendments"]])'
