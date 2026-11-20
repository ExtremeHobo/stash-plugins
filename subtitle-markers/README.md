# Subtitle Markers

Create Stash scene markers when subtitle cues match phrases from a JSON config.

## Install

Already lives at:

`%USERPROFILE%\.stash\plugins\subtitle-markers\`

1. **Settings → Plugins → Reload Plugins**
2. Enable **Subtitle Markers** if needed
3. Edit phrases in `config.json` (see below)
4. Prefer the **Dry Run** task for the first pass

Requires `stashapp-tools` (same as your other Python plugins):

```bat
pip install "stashapp-tools>=0.2.40"
```

## Edit phrases

File:

`%USERPROFILE%\.stash\plugins\subtitle-markers\config.json`

```json
{
  "markers": [
    {
      "title": "I'm cumming",
      "phrases": [
        "I'm cumming",
        "I'm going to cum",
        "I'm going to fucking cum",
        "You're going/gonna to make me cum"
      ]
    }
  ]
}
```

- **title** — marker title written to Stash
- **phrases** — any cue containing a phrase (after normalize) creates a marker at the **cue start** time
- **slash** — `going/gonna` expands to both alternatives (all combinations if multiple slashes)
- Matching is case-insensitive; HTML tags, ASS overrides, and most punctuation are stripped

Optional override path: plugin setting **Config Path**.

## Tasks

| Task | Behavior |
| --- | --- |
| **Dry Run** | Log matches only; never creates markers |
| **Run Now** | Always creates markers (no dry-run setting) |

Settings of note:

- **Primary Tag Name** (default `Subtitle Marker`) — required by Stash for markers
- **Dedupe Window** (default `30`) — keep only the earliest same-title match within ±N seconds; also skip if a same-title marker already exists in that window
- **Scene ID** — process one scene only (good for testing)
- **Max Scenes** — cap captioned scenes per run

Only scenes Stash already knows have captions are scanned (sidecars still resolved on disk next to the video: `.srt` / `.vtt` / `.ass` / `.ssa`, including `.en.srt` style names).

## Schedule (Windows)

`stash-scheduler` only schedules library scans, so use Task Scheduler + GraphQL `runPluginTask`.

Example (PowerShell) — dry-run first:

```powershell
$apiKey = (Select-String -Path "$env:USERPROFILE\.stash\config.yml" -Pattern '^api_key:\s*(.+)$').Matches.Groups[1].Value.Trim()
$body = @{
  query = 'mutation { runPluginTask(plugin_id: "subtitle-markers", task_name: "Dry Run") }'
} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:9998/graphql' -Method Post -Headers @{ ApiKey = $apiKey; 'Content-Type' = 'application/json' } -Body $body
```

For live runs, change `task_name` to `"Run Now"` after you are happy with dry-run logs.

A helper script is also included: `schedule-run.ps1`.

## CLI dry-run

```bat
python %USERPROFILE%\.stash\plugins\subtitle-markers\subtitle_markers.py --scene-id 14646 --dry-run
```

Add `--write` only when you intentionally want markers created from the CLI.
