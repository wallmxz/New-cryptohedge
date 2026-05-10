# SessionStart hook - prints structured project state for Claude Code session injection.
# Output goes to stdout and becomes context in the new session.
# Pure ASCII only (Windows console is cp1252 by default).

$ErrorActionPreference = "SilentlyContinue"
$projectRoot = "C:\Users\Wallace\Desktop\NewHedgeBot"
Set-Location $projectRoot

Write-Output "=== SESSION BRIEF (auto-injected by SessionStart hook) ==="
Write-Output ""

# --- Git state ---
Write-Output "## Git"
$branch = (& git rev-parse --abbrev-ref HEAD).Trim()
Write-Output "Branch: $branch"
$ahead = (& git rev-list --count "@{upstream}..HEAD" 2>$null)
$behind = (& git rev-list --count "HEAD..@{upstream}" 2>$null)
if ($ahead -or $behind) {
    Write-Output "Upstream: $ahead ahead, $behind behind"
}
else {
    Write-Output "Upstream: in sync (or no upstream tracking)"
}
Write-Output ""
Write-Output "Status (porcelain):"
& git status --short
Write-Output ""
Write-Output "Last 10 commits:"
& git log --oneline -10
Write-Output ""

# --- WORKING_ON.md (live state) ---
if (Test-Path "WORKING_ON.md") {
    Write-Output "## WORKING_ON.md"
    [System.IO.File]::ReadAllText("$projectRoot\WORKING_ON.md", [System.Text.Encoding]::UTF8)
}
else {
    Write-Output "## WORKING_ON.md"
    Write-Output "(not present - create one to track current focus)"
}
Write-Output ""

# --- Uvicorn process check ---
Write-Output "## Uvicorn"
$uv = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($uv) {
    $pid_uv = $uv.OwningProcess | Select-Object -First 1
    Write-Output "Listening on :8000 (PID $pid_uv)"
}
else {
    Write-Output "Not running on :8000"
}
Write-Output ""

# --- Active operation in DB (if reachable) ---
Write-Output "## Active operation (DB peek)"
if (Test-Path "automoney.db") {
    $py = "C:/Users/Wallace/Python313/python.exe"
    $sqliteOut = & $py -c "import sqlite3; c=sqlite3.connect('automoney.db'); r=c.execute(\""SELECT id, status, baseline_deposit_usd, pnl_window_since_ts FROM operations WHERE status='active' ORDER BY id DESC LIMIT 1\"").fetchone(); print('op_id={}, status={}, baseline_usd={}, pnl_window_since_ts={}'.format(*r) if r else 'no active operation')" 2>$null
    if ($sqliteOut) {
        Write-Output $sqliteOut
    }
    else {
        Write-Output "(could not query DB - python or schema unavailable)"
    }
}
else {
    Write-Output "(automoney.db not present)"
}
Write-Output ""
Write-Output "=== END SESSION BRIEF ==="
