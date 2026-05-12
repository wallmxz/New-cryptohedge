#!/usr/bin/env bash
# Migrate local automoney.db to Fly volume /data/automoney.db.
#
# Run AFTER:
#   1. fly volumes create automoney_data --size 1 --region iad
#   2. First fly deploy (so /data exists in the running machine)
#   3. stop.bat (so local bot is NOT writing to automoney.db during copy)
#
# Usage: bash scripts/flyio_migrate_db.sh

set -euo pipefail

if [[ ! -f automoney.db ]]; then
    echo "ERROR: automoney.db not found in cwd. Run from project root." >&2
    exit 1
fi

if ! command -v fly &> /dev/null; then
    echo "ERROR: fly CLI not in PATH." >&2
    exit 1
fi

# Snapshot what we're about to migrate, so user can verify post-deploy
echo "=== Local DB snapshot (op activa) ==="
"C:/Users/Wallace/Python313/python.exe" -c "
import sqlite3, json
c = sqlite3.connect('automoney.db')
c.row_factory = sqlite3.Row
op = c.execute('SELECT id, status, baseline_deposit_usd, pnl_window_since_ts, funding_paid_token0, funding_paid_token1 FROM operations WHERE status=\"active\"').fetchone()
if op:
    print(json.dumps(dict(op), indent=2))
else:
    print('NO ACTIVE OPERATION')
"
echo ""

read -r -p "Continue with migration? (yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Copying automoney.db -> Fly volume /data/automoney.db..."
echo "put automoney.db /data/automoney.db" | fly ssh sftp shell

echo ""
echo "Done. Restart the machine to load the migrated DB:"
echo "  fly machine list"
echo "  fly machine restart <machine_id>"
echo ""
echo "Then verify with:"
echo "  fly logs --no-tail | grep -E 'Restored|HedgeModel'"
