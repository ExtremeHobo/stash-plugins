# Trigger Subtitle Markers via Stash GraphQL (for Windows Task Scheduler).
# Usage:
#   .\schedule-run.ps1                 # Dry Run
#   .\schedule-run.ps1 -Task "Run Now" # live write
param(
    [ValidateSet("Dry Run", "Run Now")]
    [string]$Task = "Dry Run",
    [string]$GraphqlUrl = "http://127.0.0.1:9998/graphql",
    [string]$ConfigYml = "$env:USERPROFILE\.stash\config.yml"
)

$line = Select-String -Path $ConfigYml -Pattern '^api_key:\s*(.+)$' | Select-Object -First 1
if (-not $line) {
    throw "api_key not found in $ConfigYml"
}
$apiKey = $line.Matches.Groups[1].Value.Trim()

$query = "mutation { runPluginTask(plugin_id: `"subtitle-markers`", task_name: `"$Task`") }"
$body = @{ query = $query } | ConvertTo-Json -Compress

Write-Host "Running plugin task: $Task"
$resp = Invoke-RestMethod -Uri $GraphqlUrl -Method Post -Headers @{
    ApiKey = $apiKey
    "Content-Type" = "application/json"
} -Body $body
$resp | ConvertTo-Json -Depth 6
