# Export Clip

Cut a begin–end range from a Stash scene video to an MP4.

## Install / reload

Path:

`%USERPROFILE%\.stash\plugins\export-clip\`

1. **Settings → Plugins → Reload Plugins**
2. Enable **Export Clip** if needed
3. **Hard-refresh** the browser (Ctrl+F5) so plugin JS/CSS reload
4. Open a scene — you should see **Set Begin / Set End / Export Clip** under the player

Requires `stashapp-tools` and `ffmpeg` (auto-detected from `%USERPROFILE%\.stash\ffmpeg.exe` or PATH).

## In-player export (only way to set times)

Begin/end are **not** in plugin settings. They are per-scene, from the player:

1. Seek to the start → **Set Begin**
2. Seek to the end → **Set End**
3. Click **Export Clip**

Uses the scene ID from the URL (`/scenes/<id>`) and Video.js current time. Status text under the buttons shows the output path.

If the bar is missing: hard-refresh, confirm the plugin is enabled, open DevTools console and look for `[export-clip] UI script loaded` / `mounted bar for scene …`.

## Global settings only

| Setting | Purpose |
| --- | --- |
| **Output Directory** | Blank → `%USERPROFILE%\.stash\clips\`; or `beside-source`; or any folder |
| **FFmpeg Path** | Optional override |
| **Force Re-encode** | Skip stream-copy; always H.264/AAC |

## Output

Filename: `{sceneId}_{title}_{begin}-{end}.mp4`

Stream copy first; re-encode fallback (or when Force Re-encode is on).

## Tasks

| Task | Behavior |
| --- | --- |
| **Locate FFmpeg** | Log the ffmpeg path (no export) |

Exports are started from the player bar (or CLI / `runPluginOperation` with `scene_id` + `begin` + `end`).

## CLI

```bat
python %USERPROFILE%\.stash\plugins\export-clip\export_clip.py --locate-ffmpeg

python %USERPROFILE%\.stash\plugins\export-clip\export_clip.py --scene-id 16518 --begin 0 --end 3
```
