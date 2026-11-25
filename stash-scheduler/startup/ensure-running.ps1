# ensure-running.ps1 — Keep the Stash Scheduler daemon alive across PC reboot
# and Stash-only restarts. Idempotent: exits 0 if the daemon is already running.
#
# Prefer starting the daemon directly (python --daemon) so we are not blocked
# behind a long Identifying/Scanning job in Stash's plugin task queue.
# Also calls runPluginTask("Start Scheduler") when the queue looks idle, so
# Stash's own plugin path stays warm.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File ensure-running.ps1
#   powershell ... ensure-running.ps1 -StashUrl http://127.0.0.1:9998 -ApiKey <key>

[CmdletBinding()]
param(
    [string]$StashUrl = "",
    [string]$ApiKey = "",
    [int]$MaxWaitSeconds = 180,
    [int]$PollSeconds = 5
)

$ErrorActionPreference = "Continue"
$PluginDir = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $env:TEMP "stash-scheduler-daemon.pid"
$ConfigFile = Join-Path $env:TEMP "stash-scheduler-config.json"
$EnsureLog = Join-Path $env:TEMP "stash-scheduler-autostart.log"
$StashConfig = Join-Path $env:USERPROFILE ".stash\config.yml"
$PythonCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
    "python.exe",
    "python3.exe",
    "py.exe"
)
$SchedulerScript = Join-Path $PluginDir "stash_scheduler.py"

function Write-EnsureLog([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $EnsureLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Test-DaemonAlive {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $false }
    try {
        $procId = [int]((Get-Content -LiteralPath $PidFile -Raw).Trim())
    } catch {
        return $false
    }
    if ($procId -le 0) { return $false }
    return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

function Get-StashApiKey {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }

    if (Test-Path -LiteralPath $ConfigFile) {
        try {
            $cfg = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
            $k = $cfg.server_connection.ApiKey
            if ($k) { return [string]$k }
        } catch {}
    }

    if (Test-Path -LiteralPath $StashConfig) {
        foreach ($line in Get-Content -LiteralPath $StashConfig) {
            if ($line -match '^\s*api_key:\s*(.+)\s*$') {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return ""
}

function Get-StashPort {
    if (Test-Path -LiteralPath $StashConfig) {
        foreach ($line in Get-Content -LiteralPath $StashConfig) {
            if ($line -match '^\s*port:\s*(\d+)\s*$') { return $Matches[1] }
        }
    }
    return "9998"
}

function Get-StashUrl {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit.TrimEnd("/") }

    if (Test-Path -LiteralPath $ConfigFile) {
        try {
            $cfg = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
            $scheme = $cfg.server_connection.Scheme
            $port = $cfg.server_connection.Port
            if (-not $scheme) { $scheme = "http" }
            if ($port) { return ("{0}://127.0.0.1:{1}" -f $scheme, $port) }
        } catch {}
    }

    return "http://127.0.0.1:$(Get-StashPort)"
}

function Get-PythonExe {
    foreach ($c in $PythonCandidates) {
        if ($c -match '[\\/]' -and (Test-Path -LiteralPath $c)) { return $c }
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Invoke-StashGql {
    param(
        [string]$Url,
        [string]$Key,
        [string]$Body,
        [int]$TimeoutSec = 30
    )
    $headers = @{ "Content-Type" = "application/json" }
    if ($Key) { $headers["ApiKey"] = $Key }
    return Invoke-RestMethod -Uri "$Url/graphql" -Method POST -Headers $headers -Body $Body -TimeoutSec $TimeoutSec
}

function Save-SchedulerConfig {
    param(
        [string]$Url,
        [string]$Key,
        $PluginSettings
    )
    # Parse URL → scheme/port for server_connection
    $uri = [Uri]$Url
    $port = if ($uri.Port -gt 0) { $uri.Port } else { Get-StashPort }
    $scheme = $uri.Scheme

    $existing = $null
    if (Test-Path -LiteralPath $ConfigFile) {
        try {
            $rawExisting = [System.IO.File]::ReadAllText($ConfigFile)
            if ($rawExisting -and $rawExisting.Trim().Length -gt 0) {
                $existing = $rawExisting | ConvertFrom-Json
            }
        } catch {}
    }

    # Build a plain ordered tree (no leftover SessionCookie objects) so
    # ConvertTo-Json emits compact, valid JSON. Write UTF-8 without BOM —
    # Windows PowerShell Set-Content -Encoding UTF8 adds a BOM that has
    # raced with the daemon's json.load in the past.
    $runIdentify = $false
    $scanGeneratePreviews = $false
    $identifyTimeout = 120
    $rawFreq = "daily"
    if ($existing -and $existing.settings) {
        if ($null -ne $existing.settings.run_identify) { $runIdentify = [bool]$existing.settings.run_identify }
        if ($null -ne $existing.settings.scanGeneratePreviews) { $scanGeneratePreviews = [bool]$existing.settings.scanGeneratePreviews }
        if ($existing.settings.identify_timeout_minutes) { $identifyTimeout = [int]$existing.settings.identify_timeout_minutes }
        if ($existing.settings.frequency) { $rawFreq = [string]$existing.settings.frequency }
        if ($existing.settings.cron_expr -and $rawFreq -eq "cron") { $rawFreq = [string]$existing.settings.cron_expr }
    }
    if ($PluginSettings) {
        if ($null -ne $PluginSettings.run_identify) { $runIdentify = [bool]$PluginSettings.run_identify }
        if ($null -ne $PluginSettings.scanGeneratePreviews) { $scanGeneratePreviews = [bool]$PluginSettings.scanGeneratePreviews }
        if ($PluginSettings.identify_timeout_minutes) { $identifyTimeout = [int]$PluginSettings.identify_timeout_minutes }
        if ($PluginSettings.frequency) { $rawFreq = [string]$PluginSettings.frequency }
    }

    $frequency = "daily"
    $cronExpr = ""
    if ($rawFreq -match '^\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*$') {
        $frequency = "cron"
        $cronExpr = $rawFreq.Trim()
    } elseif ($rawFreq -in @("hourly", "daily", "weekly", "cron")) {
        $frequency = $rawFreq
        if ($existing -and $existing.settings -and $existing.settings.cron_expr) {
            $cronExpr = [string]$existing.settings.cron_expr
        }
    }

    $payload = [ordered]@{
        server_connection = [ordered]@{
            Scheme    = $scheme
            Host      = "127.0.0.1"
            Port      = [int]$port
            Dir       = (Join-Path $env:USERPROFILE ".stash")
            PluginDir = $PluginDir
            ApiKey    = $Key
        }
        settings = [ordered]@{
            frequency                   = $frequency
            time_of_day                 = "02:00"
            day_of_week                 = "sun"
            timezone                    = "UTC"
            run_identify                = $runIdentify
            identify_timeout_minutes    = $identifyTimeout
            scanPaths                   = ""
            scanGenerateCovers          = $false
            scanGeneratePreviews        = $scanGeneratePreviews
            scanGenerateImagePreviews   = $false
            scanGenerateSprites         = $false
            scanGeneratePhashes         = $false
            scanGenerateImagePhashes    = $false
            scanGenerateThumbnails      = $false
            scanGenerateClipPreviews    = $false
            rescan                      = $false
            cron_expr                   = $cronExpr
            hour                        = 2
            minute                      = 0
            scan_paths                  = @()
        }
    }

    $json = $payload | ConvertTo-Json -Depth 8 -Compress
    if (-not $json -or $json.Trim().Length -lt 20) {
        throw "Save-SchedulerConfig produced empty JSON"
    }
    # Validate round-trip before replacing the live config the daemon reads.
    $null = $json | ConvertFrom-Json
    $tmp = "$ConfigFile.tmp"
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding $false))
    [System.IO.File]::Copy($tmp, $ConfigFile, $true)
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

function Start-DaemonDirect {
    $py = Get-PythonExe
    if (-not $py) {
        Write-EnsureLog "ERROR: python executable not found."
        return $false
    }
    if (-not (Test-Path -LiteralPath $SchedulerScript)) {
        Write-EnsureLog "ERROR: missing $SchedulerScript"
        return $false
    }
    Write-EnsureLog "Starting daemon directly: $py $SchedulerScript --daemon"
    Start-Process -FilePath $py -ArgumentList @($SchedulerScript, "--daemon") -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-DaemonAlive) { return $true }
    }
    return $false
}

$StashUrl = Get-StashUrl -Explicit $StashUrl
$ApiKey = Get-StashApiKey -Explicit $ApiKey

Write-EnsureLog "ensure-running start url=$StashUrl daemonAlive=$(Test-DaemonAlive) hasApiKey=$([bool]$ApiKey)"

if (Test-DaemonAlive) {
    Write-EnsureLog "Daemon already running — nothing to do."
    exit 0
}

if (-not $ApiKey) {
    Write-EnsureLog "ERROR: No ApiKey found (need stash-scheduler-config.json or .stash/config.yml api_key)."
    exit 2
}

# Wait until Stash answers authenticated systemStatus.
$elapsed = 0
$stashUp = $false
while ($elapsed -le $MaxWaitSeconds) {
    try {
        $statusBody = '{"query":"{ systemStatus { status } }"}'
        $resp = Invoke-StashGql -Url $StashUrl -Key $ApiKey -Body $statusBody -TimeoutSec 5
        if ($resp.data.systemStatus.status) {
            $stashUp = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds $PollSeconds
    $elapsed += $PollSeconds
}

if (-not $stashUp) {
    Write-EnsureLog "ERROR: Stash not available at $StashUrl after ${MaxWaitSeconds}s."
    exit 1
}

Write-EnsureLog "Stash is up. Refreshing scheduler config from plugin settings…"

$pluginSettings = $null
try {
    $cfgResp = Invoke-StashGql -Url $StashUrl -Key $ApiKey -Body '{"query":"{ configuration { plugins } }"}' -TimeoutSec 30
    $pluginSettings = $cfgResp.data.configuration.plugins.'stash-scheduler'
} catch {
    Write-EnsureLog "WARNING: could not read plugin settings: $($_.Exception.Message)"
}

Save-SchedulerConfig -Url $StashUrl -Key $ApiKey -PluginSettings $pluginSettings

# Prefer direct daemon start — does not wait on Stash's job queue.
if (Start-DaemonDirect) {
    $procId = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    Write-EnsureLog "OK: scheduler daemon running (PID $procId) via direct start."
    # Best-effort: also poke Start Scheduler when the queue is idle so the
    # plugin task path remains exercised (ignore failures / queue backlog).
    try {
        $q = Invoke-StashGql -Url $StashUrl -Key $ApiKey -Body '{"query":"{ jobQueue { id status description } }"}' -TimeoutSec 10
        $busy = $false
        foreach ($job in @($q.data.jobQueue)) {
            $desc = [string]$job.description
            $st = [string]$job.status
            if ($st -in @('FINISHED','CANCELLED','FAILED')) { continue }
            if ($desc -match 'Identifying|Scanning|Generating|Start Scheduler') { $busy = $true; break }
        }
        if (-not $busy) {
            $mut = '{"query":"mutation { runPluginTask(plugin_id: \"stash-scheduler\", task_name: \"Start Scheduler\") }"}'
            Invoke-StashGql -Url $StashUrl -Key $ApiKey -Body $mut -TimeoutSec 15 | Out-Null
            Write-EnsureLog "Also queued Start Scheduler (queue idle) for config refresh."
        } else {
            Write-EnsureLog "Skipped runPluginTask Start Scheduler — library/plugin queue busy."
        }
    } catch {
        Write-EnsureLog "Optional runPluginTask skipped: $($_.Exception.Message)"
    }
    exit 0
}

Write-EnsureLog "Direct start failed — falling back to runPluginTask Start Scheduler…"
try {
    $mut = '{"query":"mutation { runPluginTask(plugin_id: \"stash-scheduler\", task_name: \"Start Scheduler\") }"}'
    $r = Invoke-StashGql -Url $StashUrl -Key $ApiKey -Body $mut -TimeoutSec 60
    Write-EnsureLog ("runPluginTask -> {0}" -f ($r | ConvertTo-Json -Compress -Depth 5))
} catch {
    Write-EnsureLog "ERROR: runPluginTask failed: $($_.Exception.Message)"
    exit 3
}

$confirmed = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-DaemonAlive) { $confirmed = $true; break }
}

if ($confirmed) {
    $procId = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    Write-EnsureLog "OK: scheduler daemon running (PID $procId) via plugin task."
    exit 0
}

Write-EnsureLog "ERROR: daemon PID not confirmed. See $env:TEMP\stash-scheduler-daemon.log"
exit 4
