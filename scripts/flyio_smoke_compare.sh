#!/usr/bin/env bash
# Post-cutover: fetch Fly state and diff vs local snapshot.

set -euo pipefail

if [[ ! -f tmp_out/snapshot_local.json ]]; then
    echo "ERROR: tmp_out/snapshot_local.json missing. Run flyio_smoke_snapshot.sh first." >&2
    exit 1
fi

FLY_URL="https://automoney.fly.dev/sse/state"
USER_PASS="admin:Wallace1"

echo "Fetching Fly state from $FLY_URL..."
curl -s -u "$USER_PASS" -N "$FLY_URL" 2>&1 | head -c 5000 > tmp_out/snapshot_fly.txt

"C:/Users/Wallace/Python313/python.exe" -c "
import sys, json, re
raw = open('tmp_out/snapshot_fly.txt').read()
m = re.search(r'data: (\{.+?\})\n', raw)
if not m:
    print('FAILED to parse SSE event from Fly')
    print('First 500 bytes:', raw[:500])
    sys.exit(1)
d = json.loads(m.group(1))
for k in ['last_update', 'last_iter_timings', 'log_lines']:
    d.pop(k, None)
open('tmp_out/snapshot_fly.json', 'w').write(json.dumps(d, indent=2, sort_keys=True))
print('Saved tmp_out/snapshot_fly.json')
"

echo ""
echo "=== DIFF (local vs fly, after stripping volatile fields) ==="
diff tmp_out/snapshot_local.json tmp_out/snapshot_fly.json && echo "IDENTICAL ✓" || echo ""
echo ""
echo "Expected differences: NONE in critical fields. If you see"
echo "operation_state, hedge_positions, pool_value_usd, baseline_deposit_usd"
echo "diverging — investigate before committing to the deploy."
