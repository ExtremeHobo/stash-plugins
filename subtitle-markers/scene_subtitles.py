"""Locate subtitle sidecar files for a Stash scene's video files."""

from __future__ import annotations

from pathlib import Path

from subtitle_parse import SUB_EXTENSIONS


def video_stem_candidates(video_path: Path) -> list[str]:
    """Return stem prefixes to match sidecars (base stem; keep case as-is)."""
    return [video_path.stem]


def find_subtitle_paths_for_video(video_path: str | Path) -> list[Path]:
    """
    Find subtitle files next to a video.

    Matches:
      video.srt / video.vtt / video.ass
      video.en.srt / video.en.forced.srt / etc.
    """
    vp = Path(video_path)
    folder = vp.parent
    if not folder.is_dir():
        return []

    stem = vp.stem
    stem_lower = stem.lower()
    found: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.is_file():
            continue
        name_lower = entry.name.lower()
        if not name_lower.startswith(stem_lower + "."):
            continue
        # remainder after stem must end with a known subtitle extension
        # and the final extension must be a subtitle type
        if entry.suffix.lower() not in SUB_EXTENSIONS:
            continue
        found.append(entry)

    # stable unique by resolved path
    uniq: list[Path] = []
    seen: set[str] = set()
    for p in sorted(found, key=lambda x: x.name.lower()):
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def find_subtitle_paths_for_scene(files: list[dict]) -> list[Path]:
    """files: list of Stash VideoFile-like dicts with at least 'path'."""
    all_subs: list[Path] = []
    seen: set[str] = set()
    for f in files or []:
        path = f.get("path") if isinstance(f, dict) else None
        if not path:
            continue
        for sub in find_subtitle_paths_for_video(path):
            key = str(sub).lower()
            if key not in seen:
                seen.add(key)
                all_subs.append(sub)
    return all_subs
