#!/usr/bin/env bash
# Push all .env entries as Fly secrets in a single bulk operation.
#
# Idempotent: re-running updates secrets that changed and triggers
# one machine restart (vs N restarts with N invocations).
#
# Usage: bash scripts/flyio_secrets.sh
# Prereq: fly auth login + fly app exists

set -euo pipefail

if [[ ! -f .env ]]; then
    echo "ERROR: .env not found in cwd. Run from project root." >&2
    exit 1
fi

if ! command -v fly &> /dev/null; then
    echo "ERROR: fly CLI not in PATH. Install from https://fly.io/docs/flyctl/install/" >&2
    exit 1
fi

# Build the args list. Skip blank lines, comments, and DB_PATH (set in fly.toml).
args=()
skipped_keys=()
while IFS= read -r line; do
    # Strip inline comment + trim
    line="${line%%#*}"
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [[ -z "$line" ]] && continue
    [[ "$line" != *=* ]] && continue

    key="${line%%=*}"
    # Skip keys set in fly.toml [env] block (DB_PATH overrides anything in secrets)
    if [[ "$key" == "DB_PATH" || "$key" == "LOG_FORMAT" || "$key" == "PYTHONUNBUFFERED" || "$key" == "START_ENGINE" ]]; then
        skipped_keys+=("$key")
        continue
    fi
    args+=("$line")
done < .env

if [[ ${#args[@]} -eq 0 ]]; then
    echo "No secrets to push (all keys filtered)." >&2
    exit 1
fi

echo "Pushing ${#args[@]} secret(s) to Fly..."
[[ ${#skipped_keys[@]} -gt 0 ]] && echo "  Skipped (set in fly.toml): ${skipped_keys[*]}"

fly secrets set "${args[@]}"

echo ""
echo "Done. Machines will restart automatically to pick up new secrets."
echo "Run: fly status   to confirm restart completes"
