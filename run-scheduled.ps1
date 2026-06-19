# Wrapper invoked by Windows Task Scheduler.
# Logs combined stdout/stderr to logs/scheduled-wrapper-*.log so failures before
# Python startup remain visible (the Python entrypoint writes its own run-*.log).
# ASCII only -- PowerShell parses .ps1 files as Windows-1252 by default and
# non-ASCII characters can break the parser.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log = Join-Path $root "logs\scheduled-wrapper-$stamp.log"

# UTF-8 (no BOM): keeps wrapper logs readable side-by-side with the Python run-*.log files.
$utf8 = New-Object System.Text.UTF8Encoding($false)
$header = "=== pq-tracker scheduled run @ $(Get-Date -Format o) ===`r`ncwd: $root`r`nuser: $env:USERNAME on $env:COMPUTERNAME`r`n"
[System.IO.File]::WriteAllText($log, $header, $utf8)

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    [System.IO.File]::AppendAllText($log, "ERROR: venv python not found at $py. Run 'uv sync' from the project dir to rebuild .venv.`r`n", $utf8)
    exit 2
}

# Phase 1: Oireachtas ingest (writes its own logs/run-*.log too).
[System.IO.File]::AppendAllText($log, "--- phase 1: oireachtas ingest ---`r`n", $utf8)
$output = & $py -m pq_tracker 2>&1 | Out-String
$oireachtasCode = $LASTEXITCODE
[System.IO.File]::AppendAllText($log, $output, $utf8)
[System.IO.File]::AppendAllText($log, "--- oireachtas exit code: $oireachtasCode ---`r`n", $utf8)

# Phase 2: HSE live-site incremental walk (writes its own logs/hse-*.log too).
# The incremental command stops on consecutive known PDFs, so it's safe to run
# daily even though the live-site index has thousands of entries.
[System.IO.File]::AppendAllText($log, "`r`n--- phase 2: hse incremental ---`r`n", $utf8)
$output = & $py -m pq_tracker.hse_cli ingest 2>&1 | Out-String
$hseCode = $LASTEXITCODE
[System.IO.File]::AppendAllText($log, $output, $utf8)
[System.IO.File]::AppendAllText($log, "--- hse exit code: $hseCode ---`r`n", $utf8)

# Phase 3: HSE targeted backfill of answers back-published deep in the listing.
# The phase-2 page-walk can't reach answers HSE injects far down the date-ordered
# listing (they are back-dated to the PQ's original month). So we query
# about.hse.ie per-ref for answered PQs that still have no HSE PDF and whose
# answer defers to the HSE. The candidate set is bounded by that classifier
# (plus manual labels); a last-checked cadence + per-run cap spread the lookups
# politely. Ordered oldest-checked first, --limit 150/day x ~6.5 days covers the
# ~970-candidate set, and --recheck-days 7 then lets the oldest batch come round
# again -- a tidy weekly rotation in small daily slices (not one weekly flood).
[System.IO.File]::AppendAllText($log, "`r`n--- phase 3: hse backfill-missing (cadence) ---`r`n", $utf8)
$output = & $py -m pq_tracker.hse_cli backfill-missing --recheck-days 7 --limit 150 2>&1 | Out-String
$hseMissingCode = $LASTEXITCODE
[System.IO.File]::AppendAllText($log, $output, $utf8)
[System.IO.File]::AppendAllText($log, "--- hse backfill-missing exit code: $hseMissingCode ---`r`n", $utf8)

# Combined exit: if any phase failed, surface the first non-zero code so Task
# Scheduler shows the run as failed in the History panel.
$code = if ($oireachtasCode -ne 0) { $oireachtasCode }
        elseif ($hseCode -ne 0) { $hseCode }
        else { $hseMissingCode }
[System.IO.File]::AppendAllText($log, "`r`n--- final exit code: $code ---`r`n", $utf8)
exit $code
