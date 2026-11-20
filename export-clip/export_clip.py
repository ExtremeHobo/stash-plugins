#!/usr/bin/env python3
"""
export-clip — Export an MP4 clip from a Stash scene between begin and end times.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import stashapi.log as log
from stashapi.stashapp import StashInterface

PLUGIN_DIR = Path(__file__).resolve().parent
STASH_DIR = Path.home() / ".stash"
DEFAULT_CLIPS_DIR = STASH_DIR / "clips"
BESIDE_SOURCE = "beside-source"

SCENE_FRAGMENT = """
id
title
files {
  path
  basename
  duration
  width
  height
  video_codec
  audio_codec
}
"""

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


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


def parse_time(value) -> float:
    """Parse seconds, MM:SS, or HH:MM:SS into float seconds."""
    if value is None:
        raise ValueError("time value is empty")
    if isinstance(value, (int, float)):
        sec = float(value)
        if sec < 0:
            raise ValueError(f"negative time: {value}")
        return sec

    s = str(value).strip()
    if not s:
        raise ValueError("time value is empty")

    # Plain number (int or float seconds)
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)

    parts = s.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    raise ValueError(f"unrecognized time format: {value!r} (use seconds, MM:SS, or HH:MM:SS)")


def format_time_for_name(seconds: float) -> str:
    """Compact timestamp for filenames: 0s, 1m30s, 1h02m03s (decimals as _ms)."""
    if seconds < 0:
        seconds = 0.0
    whole = int(seconds)
    frac = seconds - whole
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h:
        base = f"{h}h{m:02d}m{s:02d}s"
    elif m:
        base = f"{m}m{s:02d}s"
    else:
        base = f"{s}s"
    if frac >= 0.001:
        ms = int(round(frac * 1000))
        if ms:
            base = f"{base}_{ms:03d}"
    return base


def sanitize_filename(name: str, max_len: int = 80) -> str:
    name = (name or "").strip() or "scene"
    # Scene titles are often the basename including extension
    name = re.sub(
        r"\.(mp4|mkv|avi|mov|wmv|m4v|webm|ts|m2ts)$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "scene"


def find_ffmpeg(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit and str(explicit).strip():
        candidates.append(Path(str(explicit).strip()))

    candidates.append(STASH_DIR / "ffmpeg.exe")
    candidates.append(STASH_DIR / "ffmpeg")

    # PATH lookup
    from shutil import which

    for name in ("ffmpeg.exe", "ffmpeg"):
        w = which(name)
        if w:
            candidates.append(Path(w))

    # Common Windows installs
    winget_root = (
        Path.home()
        / "AppData"
        / "Local"
        / "Microsoft"
        / "WinGet"
        / "Packages"
    )
    if winget_root.is_dir():
        for p in winget_root.glob("**/ffmpeg.exe"):
            candidates.append(p)

    seen: set[str] = set()
    for c in candidates:
        key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            continue

    raise FileNotFoundError(
        "ffmpeg not found. Install ffmpeg, place ffmpeg.exe in "
        f"{STASH_DIR}, or set plugin setting FFmpeg Path."
    )


def run_ffmpeg(ffmpeg: Path, argv: list[str], *, label: str) -> subprocess.CompletedProcess:
    cmd = [str(ffmpeg), *argv]
    log.info(f"{label}: {' '.join(cmd)}")
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if CREATE_NO_WINDOW:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def probe_ok(ffmpeg: Path) -> str:
    proc = run_ffmpeg(ffmpeg, ["-version"], label="ffmpeg -version")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg -version failed (code {proc.returncode}): {proc.stderr or proc.stdout}")
    first = (proc.stdout or proc.stderr or "").splitlines()
    return first[0] if first else str(ffmpeg)


def export_clip_file(
    ffmpeg: Path,
    source: Path,
    output: Path,
    begin: float,
    end: float,
    *,
    force_reencode: bool = False,
) -> Path:
    duration = end - begin
    if duration <= 0:
        raise ValueError(f"invalid duration: begin={begin} end={end}")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    common_tail = [
        "-t",
        f"{duration:.3f}",
        "-movflags",
        "+faststart",
        "-y",
        str(output),
    ]

    if not force_reencode:
        copy_args = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{begin:.3f}",
            "-i",
            str(source),
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            *common_tail,
        ]
        proc = run_ffmpeg(ffmpeg, copy_args, label="stream-copy")
        if proc.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            log.info(f"stream-copy OK → {output} ({output.stat().st_size} bytes)")
            return output
        log.warning(
            "stream-copy failed or empty output; falling back to re-encode. "
            f"code={proc.returncode} stderr={(proc.stderr or '').strip()[:500]}"
        )
        if output.exists():
            try:
                output.unlink()
            except OSError:
                pass

    reenc_args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{begin:.3f}",
        "-i",
        str(source),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        *common_tail,
    ]
    proc = run_ffmpeg(ffmpeg, reenc_args, label="re-encode")
    if proc.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg re-encode failed (code {proc.returncode}): {err[:800]}")
    log.info(f"re-encode OK → {output} ({output.stat().st_size} bytes)")
    return output


def resolve_output_path(
    output_dir_setting: str | None,
    scene: dict,
    source: Path,
    begin: float,
    end: float,
) -> Path:
    scene_id = str(scene.get("id") or "unknown")
    title = sanitize_filename(scene.get("title") or source.stem)
    begin_s = format_time_for_name(begin)
    end_s = format_time_for_name(end)
    filename = f"{scene_id}_{title}_{begin_s}-{end_s}.mp4"

    setting = (output_dir_setting or "").strip()
    if not setting:
        out_dir = DEFAULT_CLIPS_DIR
    elif setting.lower() in (BESIDE_SOURCE, "next-to-source", "beside", "source"):
        out_dir = source.parent
    else:
        out_dir = Path(setting)

    return out_dir / filename


def pick_source_file(scene: dict) -> tuple[Path, float | None]:
    files = scene.get("files") or []
    if not files:
        raise FileNotFoundError(f"scene {scene.get('id')} has no files")
    # Prefer first file with a path that exists
    for f in files:
        path = Path(f.get("path") or "")
        if path.is_file():
            dur = f.get("duration")
            try:
                duration = float(dur) if dur is not None else None
            except (TypeError, ValueError):
                duration = None
            return path, duration
    raise FileNotFoundError(
        f"scene {scene.get('id')}: none of the file paths exist on disk "
        f"({[f.get('path') for f in files]})"
    )


def validate_range(begin: float, end: float, duration: float | None) -> None:
    if begin < 0:
        raise ValueError(f"begin time must be >= 0 (got {begin})")
    if end <= begin:
        raise ValueError(f"begin ({begin}) must be less than end ({end})")
    if duration is not None:
        if begin >= duration:
            raise ValueError(
                f"begin ({begin}s) is beyond scene duration ({duration}s)"
            )
        if end > duration + 0.5:
            # soft: allow tiny float slack; hard fail if clearly past end
            raise ValueError(
                f"end ({end}s) is beyond scene duration ({duration}s)"
            )


def run_export(stash: StashInterface, plugin_settings: dict, args: dict) -> Path:
    def _arg(*keys):
        """Return first present arg (0 / 0.0 are valid — do not use truthiness)."""
        for k in keys:
            if k not in args:
                continue
            v = args.get(k)
            if v is None:
                continue
            if isinstance(v, str) and v.strip() == "":
                continue
            return v
        return None

    # Begin/end/scene are per-export only (player UI or CLI/GraphQL args) — not plugin settings.
    scene_id = _arg("scene_id", "sceneId")
    if scene_id is None or str(scene_id).strip() == "":
        raise ValueError(
            "scene_id is required (from the scene player Export Clip bar, CLI --scene-id, "
            "or GraphQL args_map). It is not a plugin setting."
        )
    scene_id = str(scene_id).strip()

    begin_raw = _arg("begin", "beginTime")
    end_raw = _arg("end", "endTime")
    if begin_raw is None:
        raise ValueError("begin is required (player Set Begin, or seconds / MM:SS / HH:MM:SS)")
    if end_raw is None:
        raise ValueError("end is required (player Set End, or seconds / MM:SS / HH:MM:SS)")

    begin = parse_time(begin_raw)
    end = parse_time(end_raw)

    output_dir = args.get("output_dir") or args.get("outputDir") or plugin_settings.get("outputDir")
    ffmpeg_path = args.get("ffmpeg_path") or args.get("ffmpegPath") or plugin_settings.get("ffmpegPath")
    force_reencode = _as_bool(
        args.get("force_reencode")
        if args.get("force_reencode") is not None
        else (
            args.get("forceReencode")
            if args.get("forceReencode") is not None
            else plugin_settings.get("forceReencode")
        ),
        False,
    )

    ffmpeg = find_ffmpeg(str(ffmpeg_path) if ffmpeg_path else None)
    ver = probe_ok(ffmpeg)
    log.info(f"using ffmpeg: {ffmpeg}")
    log.info(ver)

    sid = int(scene_id) if scene_id.isdigit() else scene_id
    scene = stash.find_scene(sid, fragment=SCENE_FRAGMENT)
    if not scene:
        raise ValueError(f"scene not found: {scene_id}")

    source, duration = pick_source_file(scene)
    log.info(
        f"scene {scene.get('id')} '{scene.get('title')}' "
        f"source={source} duration={duration}"
    )
    validate_range(begin, end, duration)

    output = resolve_output_path(
        str(output_dir) if output_dir else None,
        scene,
        source,
        begin,
        end,
    )
    log.info(f"exporting {begin:.3f}s–{end:.3f}s ({end - begin:.3f}s) → {output}")

    return export_clip_file(
        ffmpeg,
        source,
        output,
        begin,
        end,
        force_reencode=force_reencode,
    )


def run_locate_ffmpeg(plugin_settings: dict, args: dict) -> Path:
    ffmpeg_path = args.get("ffmpeg_path") or args.get("ffmpegPath") or plugin_settings.get("ffmpegPath")
    ffmpeg = find_ffmpeg(str(ffmpeg_path) if ffmpeg_path else None)
    ver = probe_ok(ffmpeg)
    log.info(f"ffmpeg found: {ffmpeg}")
    log.info(ver)
    return ffmpeg


def read_stash_stdin() -> dict | None:
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    return json.loads(raw)


def build_cli_stash(url: str, api_key: str | None) -> StashInterface:
    conn = {
        "Scheme": "http",
        "Port": 9998,
        "Host": "127.0.0.1",
        "Dir": str(STASH_DIR),
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
    cfg = STASH_DIR / "config.yml"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.startswith("api_key:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def main() -> None:
    stash_data = None
    try:
        stash_data = read_stash_stdin()
    except json.JSONDecodeError:
        stash_data = None

    if stash_data and "server_connection" in stash_data:
        args = stash_data.get("args") or {}
        action = (args.get("action") or "export-clip").strip()
        stash = StashInterface(stash_data["server_connection"])
        stash_config = stash.get_configuration()
        plugin_settings = (stash_config.get("plugins") or {}).get("export-clip", {}) or {}

        try:
            if action in ("export-clip", "export", "run"):
                out = run_export(stash, plugin_settings, args)
                log.info(f"done: {out}")
                # runPluginOperation (in-player Export) reads stdout Output
                log.exit(str(out))
            elif action in ("locate-ffmpeg", "locate"):
                path = run_locate_ffmpeg(plugin_settings, args)
                log.exit(str(path))
            else:
                log.error(f"unknown action: {action}")
                log.exit(None, err=f"unknown action: {action}")
        except Exception as e:
            log.error(f"export-clip failed: {e}")
            log.exit(None, err=str(e))
        return

    parser = argparse.ArgumentParser(description="export-clip CLI")
    parser.add_argument("--url", default="http://127.0.0.1:9998/graphql")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--scene-id", required=False)
    parser.add_argument("--begin", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--force-reencode", action="store_true")
    parser.add_argument(
        "--locate-ffmpeg",
        action="store_true",
        help="Only resolve/print ffmpeg path",
    )
    cli = parser.parse_args()

    if cli.locate_ffmpeg:
        path = run_locate_ffmpeg({"ffmpegPath": cli.ffmpeg}, {})
        print(path)
        return

    if not cli.scene_id or cli.begin is None or cli.end is None:
        parser.error("--scene-id, --begin, and --end are required (unless --locate-ffmpeg)")

    api_key = cli.api_key or load_api_key_from_config_yml()
    stash = build_cli_stash(cli.url, api_key)
    out = run_export(
        stash,
        {},
        {
            "scene_id": cli.scene_id,
            "begin": cli.begin,
            "end": cli.end,
            "output_dir": cli.output_dir,
            "ffmpeg_path": cli.ffmpeg,
            "force_reencode": cli.force_reencode,
        },
    )
    print(out)


if __name__ == "__main__":
    main()
