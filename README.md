# stash-plugins

Personal / patched [Stash](https://github.com/stashapp/stash) plugins maintained for Windows and local workflows.

## Plugins

| Folder | Description |
| --- | --- |
| [`subtitle-markers`](subtitle-markers/) | Create scene markers when subtitle cues match phrase groups in `config.json` |
| [`export-clip`](export-clip/) | In-player begin/end controls to export an MP4 clip via ffmpeg |
| [`stash-scheduler`](stash-scheduler/) | Community Stash Scheduler with Windows-oriented fixes (detached daemon / ApiKey persistence) |
| [`rename-file-on-update`](rename-file-on-update/) | Rename files on scene update, including subtitle sidecar pairing fixes |

## Install

Copy (or symlink) a plugin folder into your Stash plugins directory, for example:

`%USERPROFILE%\.stash\plugins\`

Then **Settings → Plugins → Reload Plugins**.

Each plugin folder has its own README with settings, tasks, and dependencies (`stashapp-tools`, ffmpeg where noted).

## Secrets

This repository must not contain Stash API keys, session cookies, `config.yml`, or runtime scheduler config from `%TEMP%`. See `.gitignore`.

Phrase lists in `subtitle-markers/config.json` are example match rules only — not credentials.
