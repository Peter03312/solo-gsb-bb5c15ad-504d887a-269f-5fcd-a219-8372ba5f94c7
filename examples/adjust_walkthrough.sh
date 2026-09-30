#!/usr/bin/env bash
# End-to-end curl walkthrough for the license scope-adjustment endpoint
# (equipment relocation). Assumes an empty database.
# Usage: ./examples/adjust_walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

echo "== 1. approve A at the origin, band [100,110] (revision 1) =="
A=$(post /api/applications '{
  "name":"Stage A",
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T10:00:00Z"}
}')
A_ID=$(echo "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$A_ID/approve" '{"expected_revision":0}' | J

echo "== 2. approve B far away on [200,210] (revision 2) =="
B=$(post /api/applications '{
  "name":"Stage B",
  "circle":{"cx":100,"cy":0,"radius":10},
  "frequency":{"low":200,"high":210},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T10:00:00Z"}
}')
B_ID=$(echo "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$B_ID/approve" '{"expected_revision":1}' | J

echo "== 3. relocate A to x=50 (revision 3); its own old scope is excluded =="
post "/api/applications/$A_ID/adjust" '{
  "expected_revision":2,
  "reason":"equipment moved to the new grandstand",
  "circle":{"cx":50,"cy":0,"radius":10},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T10:00:00Z"}
}' | J

echo "== 4. old revision 1 still shows the ORIGINAL scope; revision 3 the new one =="
curl -s "$BASE/api/revision/1" | J
curl -s "$BASE/api/revision/3" | J

echo "== 5. move A onto B's scope -> 409 interference; old scope stays, rev unchanged =="
post "/api/applications/$A_ID/adjust" '{
  "expected_revision":3,
  "circle":{"cx":100,"cy":0,"radius":10},
  "frequency":{"low":200,"high":210},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T10:00:00Z"}
}' | J || true

echo "== 6. stale expected revision -> 409 stale_revision =="
post "/api/applications/$A_ID/adjust" '{
  "expected_revision":2,
  "circle":{"cx":60,"cy":0,"radius":10},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T10:00:00Z"}
}' | J || true

echo "== 7. illegal new scope -> 422 validation_failed (whole request rejected) =="
post "/api/applications/$A_ID/adjust" '{
  "expected_revision":3,
  "circle":{"cx":60,"cy":0,"radius":0},
  "frequency":{"low":300,"high":200},
  "time_window":{"start":"2026-10-01T10:00:00Z","end":"2026-10-01T08:00:00Z"}
}' | J || true

echo "== 8. later approval at the OLD site uses the NEW scope and succeeds =="
C=$(post /api/applications '{
  "name":"Stage C at old site",
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T10:00:00Z"}
}')
C_ID=$(echo "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$C_ID/approve" '{"expected_revision":3}' | J

echo "== current state =="
curl -s "$BASE/api/revision/current" | J
