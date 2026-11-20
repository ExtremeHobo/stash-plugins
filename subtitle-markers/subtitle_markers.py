#!/usr/bin/env python3
"""
subtitle-markers — Create Stash scene markers from subtitle phrase matches.

Reads phrase groups from config.json (next to this plugin by default), finds
subtitle sidecars for scenes, and creates markers at matching cue start times.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import stashapi.log as log
from stashapi.stashapp import StashInterface

from phrase_match import CompiledMarker, compile_markers, cue_matches, normalize_text
from scene_subtitles import find_subtitle_paths_for_scene
from subtitle_parse import parse_subtitle_file

PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PLUGIN_DIR / "config.json"
DEFAULT_DEDUPE_SECONDS = 30.0
DEFAULT_PRIMARY_TAG = "Subtitle Marker"

SCENE_FRAGMENT = """
id
title
files { path basename }
captions { language_code caption_type }
scene_markers { id title seconds primary_tag { id name } }
"""


def _as_bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _as_float(val, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _as_int(val, default: int | None = None) -> int | None:
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def load_phrase_config(config_path: Path) -> list[CompiledMarker]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    markers = data.get("markers") if isinstance(data, dict) else None
    if not isinstance(markers, list):
        raise ValueError(f"config must contain a 'markers' list: {config_path}")
    compiled = compile_markers(markers)
    if not compiled:
        raise ValueError(f"no usable marker phrase groups in {config_path}")
    return compiled


def resolve_primary_tag(stash: StashInterface, tag_name: str) -> dict:
    tag = stash.find_tag(tag_name, create=True)
    if not tag or not tag.get("id"):
        raise RuntimeError(f"could not find/create primary tag: {tag_name}")
    return tag


def marker_exists_near(
    existing: list[dict],
    title: str,
    seconds: float,
    window: float,
) -> bool:
    """True if a same-title marker already sits within ±window seconds."""
    title_l = title.lower()
    for m in existing:
        if (m.get("title") or "").lower() != title_l:
            continue
        try:
            sec = float(m.get("seconds") or 0)
        except (TypeError, ValueError):
            continue
        if abs(sec - seconds) <= window:
            return True
    return False


Hit = tuple[CompiledMarker, float, str, Path]


def filter_deduped_hits(
    hits: list[Hit],
    existing: list[dict],
    window: float,
) -> tuple[list[Hit], int]:
    """
    Keep the earliest same-title hit; drop later ones inside the window.

    Also drops candidates that fall within the window of an existing same-title
    marker (Stash or already accepted earlier in this pass). Returns
    (to_create, skipped_dup_count).
    """
    working = [
        {
            "title": m.get("title"),
            "seconds": m.get("seconds"),
        }
        for m in existing
    ]
    to_create: list[Hit] = []
    skipped = 0
    for hit in sorted(hits, key=lambda h: (h[1], h[0].title)):
        cm, sec, _norm, _sub = hit
        if marker_exists_near(working, cm.title, sec, window):
            skipped += 1
            continue
        to_create.append(hit)
        working.append({"title": cm.title, "seconds": sec})
    return to_create, skipped


def collect_matches_for_scene(
    scene: dict,
    compiled: list[CompiledMarker],
) -> list[Hit]:
    """Return list of (marker, seconds, matched_cue_norm, subtitle_path)."""
    subs = find_subtitle_paths_for_scene(scene.get("files") or [])
    hits: list[Hit] = []
    for sub in subs:
        try:
            cues = parse_subtitle_file(sub)
        except Exception as e:
            log.warning(f"failed to parse {sub}: {e}")
            continue
        for start, text in cues:
            norm = normalize_text(text)
            if not norm:
                continue
            for cm in compiled:
                if cue_matches(norm, cm.phrases_norm):
                    hits.append((cm, float(start), norm, sub))
    return hits


def process_scene(
    stash: StashInterface,
    scene: dict,
    compiled: list[CompiledMarker],
    *,
    dry_run: bool,
    dedupe_window: float,
    primary_tag_id: str,
) -> dict:
    stats = {
        "scene_id": scene.get("id"),
        "title": scene.get("title"),
        "subs": 0,
        "matches": 0,
        "created": 0,
        "skipped_dup": 0,
        "would_create": 0,
    }
    subs = find_subtitle_paths_for_scene(scene.get("files") or [])
    stats["subs"] = len(subs)
    if not subs:
        log.info(f"scene {scene.get('id')}: no subtitle sidecars found")
        return stats

    existing = list(scene.get("scene_markers") or [])
    # also refresh from API if missing (CLI path)
    if not existing and scene.get("id"):
        try:
            existing = stash.get_scene_markers(
                scene["id"],
                fragment="id title seconds primary_tag { id name }",
            ) or []
        except Exception:
            existing = []

    hits = collect_matches_for_scene(scene, compiled)
    accepted, skipped_dup = filter_deduped_hits(hits, existing, dedupe_window)
    stats["matches"] = len(accepted) + skipped_dup
    stats["skipped_dup"] = skipped_dup

    for cm, sec, norm, sub in accepted:
        if dry_run:
            stats["would_create"] += 1
            # Mirror a create into existing so later accepted hits stay consistent
            # if callers reuse existing; filter_deduped_hits already accounted for them.
            existing.append({"title": cm.title, "seconds": sec})
            log.info(
                f"[dry-run] scene {scene.get('id')} ({scene.get('title')}): "
                f"would create '{cm.title}' @ {sec:.3f}s from {sub.name} cue≈'{norm[:80]}'"
            )
            continue
        created = stash.create_scene_marker(
            {
                "title": cm.title,
                "seconds": sec,
                "scene_id": str(scene["id"]),
                "primary_tag_id": str(primary_tag_id),
            },
            fragment="id title seconds",
        )
        stats["created"] += 1
        existing.append(
            {
                "id": created.get("id") if created else None,
                "title": cm.title,
                "seconds": sec,
            }
        )
        log.info(
            f"scene {scene.get('id')}: created '{cm.title}' @ {sec:.3f}s from {sub.name}"
        )
    return stats


def iter_caption_scenes(stash: StashInterface, *, per_page: int = 50, max_scenes: int | None = None):
    page = 1
    yielded = 0
    while True:
        result = stash.find_scenes(
            f={"captions": {"modifier": "NOT_NULL", "value": ""}},
            filter={"page": page, "per_page": per_page},
            fragment=SCENE_FRAGMENT,
            get_count=True,
        )
        if isinstance(result, tuple):
            count, scenes = result
        else:
            scenes = result or []
            count = len(scenes)
        if not scenes:
            break
        for sc in scenes:
            yield sc
            yielded += 1
            if max_scenes is not None and yielded >= max_scenes:
                return
        if page * per_page >= count:
            break
        page += 1


def run_process(
    stash: StashInterface,
    settings: dict,
    args: dict,
) -> None:
    plugin_settings = settings or {}
    # Task/CLI args only — no dryRun plugin setting (Dry Run task vs Run Now).
    dry_run = _as_bool(args.get("dry_run"), False)
    config_path = Path(
        args.get("config_path")
        or plugin_settings.get("configPath")
        or str(DEFAULT_CONFIG)
    )
    if not config_path.is_absolute():
        config_path = (PLUGIN_DIR / config_path).resolve()
    dedupe_window = _as_float(
        args.get("dedupe_window") or plugin_settings.get("dedupeWindowSeconds"),
        DEFAULT_DEDUPE_SECONDS,
    )
    primary_tag_name = (
        args.get("primary_tag")
        or plugin_settings.get("primaryTagName")
        or DEFAULT_PRIMARY_TAG
    ).strip() or DEFAULT_PRIMARY_TAG
    max_scenes = _as_int(
        args.get("max_scenes")
        if args.get("max_scenes") is not None
        else plugin_settings.get("maxScenes"),
        None,
    )
    if max_scenes is not None and max_scenes <= 0:
        max_scenes = None
    scene_id = args.get("scene_id") or plugin_settings.get("sceneId")
    if scene_id is not None and str(scene_id).strip() != "":
        scene_id = str(scene_id).strip()
    else:
        scene_id = None

    log.info(f"subtitle-markers config: {config_path}")
    log.info(
        f"dry_run={dry_run} dedupe_window={dedupe_window}s primary_tag='{primary_tag_name}' "
        f"scene_id={scene_id or '*'} max_scenes={max_scenes}"
    )

    compiled = load_phrase_config(config_path)
    for cm in compiled:
        log.info(
            f"marker group '{cm.title}': {len(cm.phrases_norm)} expanded phrase(s) "
            f"e.g. {list(cm.phrases_raw)[:4]}"
        )

    primary_tag_id = None
    if not dry_run:
        primary_tag = resolve_primary_tag(stash, primary_tag_name)
        primary_tag_id = str(primary_tag["id"])
    else:
        log.info(f"dry-run: skipping primary tag ensure for '{primary_tag_name}'")

    totals = {
        "scenes": 0,
        "with_subs": 0,
        "matches": 0,
        "created": 0,
        "would_create": 0,
        "skipped_dup": 0,
    }

    if scene_id:
        scene = stash.find_scene(int(scene_id) if scene_id.isdigit() else scene_id, fragment=SCENE_FRAGMENT)
        if not scene:
            log.error(f"scene not found: {scene_id}")
            return
        scenes = [scene]
    else:
        scenes = list(iter_caption_scenes(stash, max_scenes=max_scenes))

    total_n = len(scenes)
    for i, scene in enumerate(scenes):
        if total_n:
            log.progress(i / total_n)
        totals["scenes"] += 1
        stats = process_scene(
            stash,
            scene,
            compiled,
            dry_run=dry_run,
            dedupe_window=dedupe_window,
            primary_tag_id=primary_tag_id or "",
        )
        if stats["subs"]:
            totals["with_subs"] += 1
        totals["matches"] += stats["matches"]
        totals["created"] += stats["created"]
        totals["would_create"] += stats["would_create"]
        totals["skipped_dup"] += stats["skipped_dup"]

    if total_n:
        log.progress(1.0)
    log.info(
        "done: "
        f"scenes={totals['scenes']} with_subs={totals['with_subs']} "
        f"matches={totals['matches']} created={totals['created']} "
        f"would_create={totals['would_create']} skipped_dup={totals['skipped_dup']} "
        f"dry_run={dry_run}"
    )


def read_stash_stdin() -> dict | None:
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    return json.loads(raw)


def build_cli_stash(url: str, api_key: str | None) -> StashInterface:
    # StashInterface accepts server_connection dict like plugins receive
    conn = {
        "Scheme": "http",
        "Port": 9998,
        "Host": "127.0.0.1",
        "Dir": str(Path.home() / ".stash"),
        "PluginDir": str(PLUGIN_DIR),
    }
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme:
        conn["Scheme"] = u.scheme
    if u.hostname:
        conn["Host"] = u.hostname
    if u.port:
        conn["Port"] = u.port
    if api_key:
        conn["ApiKey"] = api_key
    return StashInterface(conn)


def load_api_key_from_config_yml() -> str | None:
    cfg = Path.home() / ".stash" / "config.yml"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.startswith("api_key:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def main() -> None:
    # Plugin mode: Stash pipes JSON on stdin
    stash_data = None
    try:
        stash_data = read_stash_stdin()
    except json.JSONDecodeError:
        stash_data = None

    if stash_data and "server_connection" in stash_data:
        args = stash_data.get("args") or {}
        action = (args.get("action") or "run").strip()
        stash = StashInterface(stash_data["server_connection"])
        stash_config = stash.get_configuration()
        plugin_settings = (stash_config.get("plugins") or {}).get("subtitle-markers", {}) or {}

        if action in ("run", "run-now", "dry-run"):
            # Dry Run task → always dry; Run Now / run → always write.
            args = {**args, "dry_run": action == "dry-run"}
            run_process(stash, plugin_settings, args)
        else:
            log.error(f"unknown action: {action}")
        return

    # CLI mode for local testing
    parser = argparse.ArgumentParser(description="subtitle-markers CLI / dry-run helper")
    parser.add_argument("--url", default="http://127.0.0.1:9998/graphql")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--scene-id", default=None)
    parser.add_argument("--max-scenes", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--write", action="store_true", help="Actually create markers (disables dry-run)")
    parser.add_argument("--dedupe-window", type=float, default=DEFAULT_DEDUPE_SECONDS)
    parser.add_argument("--primary-tag", default=DEFAULT_PRIMARY_TAG)
    cli = parser.parse_args()

    api_key = cli.api_key or load_api_key_from_config_yml()
    stash = build_cli_stash(cli.url, api_key)
    run_process(
        stash,
        {},
        {
            "dry_run": not cli.write,
            "config_path": cli.config,
            "scene_id": cli.scene_id,
            "max_scenes": cli.max_scenes,
            "dedupe_window": cli.dedupe_window,
            "primary_tag": cli.primary_tag,
        },
    )


if __name__ == "__main__":
    main()
