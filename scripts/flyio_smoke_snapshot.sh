#!/usr/bin/env bash
# Pre-cutover: capture local /sse/state for post-deploy diff.
# Output: tmp_out/snapshot_local.txt (gitignored)

set -euo pipefail
mkdir -p tmp_out

LOCAL_URL="http://127.0.0.1:8000/sse/state"
USER_PASS="admin:Wallace1"

echo "Fetching local state from $LOCAL_URL..."
curl -s -u "$USER_PASS" -N "$LOCAL_URL" 2>&1 | head -c 5000 > tmp_out/snapshot_local.txt

# Extract just the first SSE event JSON (deterministic, easier to diff)
"C:/Users/Wallace/Python313/python.exe" -c "
import sys, json, re
raw = open('tmp_out/snapshot_local.txt').read()
m = re.search(r'data: (\{.+?\})\n', raw)
if m:
    d = json.loads(m.group(1))
    # Strip volatile fields (timestamps, timings) for clean diff
    for k in ['last_update', 'last_iter_timings', 'log_lines']:
        d.pop(k, None)
    open('tmp_out/snapshot_local.json', 'w').write(json.dumps(d, indent=2, sort_keys=True))
    print('Saved tmp_out/snapshot_local.json (volatile fields stripped)')
else:
    print('FAILED to parse SSE event from local /sse/state')
    sys.exit(1)
"
