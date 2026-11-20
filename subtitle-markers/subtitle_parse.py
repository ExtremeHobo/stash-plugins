"""Parse common subtitle formats into (start_seconds, text) cues."""

from __future__ import annotations

import re
from pathlib import Path


_SRT_TIME = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)
_VTT_TIME = re.compile(
    r"(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2})\.(?P<ms>\d{1,3})"
)
_ASS_TIME = re.compile(r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<cs>\d{1,2})")

SUB_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")


def _hms_to_seconds(h: int, m: int, s: int, frac: float) -> float:
    return h * 3600 + m * 60 + s + frac


def parse_srt_timestamp(ts: str) -> float | None:
    m = _SRT_TIME.search(ts.strip())
    if not m:
        return None
    ms = m.group("ms").ljust(3, "0")[:3]
    return _hms_to_seconds(int(m.group("h")), int(m.group("m")), int(m.group("s")), int(ms) / 1000.0)


def parse_vtt_timestamp(ts: str) -> float | None:
    m = _VTT_TIME.search(ts.strip())
    if not m:
        return None
    h = int(m.group("h") or 0)
    ms = m.group("ms").ljust(3, "0")[:3]
    return _hms_to_seconds(h, int(m.group("m")), int(m.group("s")), int(ms) / 1000.0)


def parse_ass_timestamp(ts: str) -> float | None:
    m = _ASS_TIME.search(ts.strip())
    if not m:
        return None
    cs = m.group("cs").ljust(2, "0")[:2]
    return _hms_to_seconds(int(m.group("h")), int(m.group("m")), int(m.group("s")), int(cs) / 100.0)


def parse_srt(content: str) -> list[tuple[float, str]]:
    cues: list[tuple[float, str]] = []
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        # optional index line
        i = 0
        if lines[0].strip().isdigit():
            i = 1
        if i >= len(lines):
            continue
        if "-->" not in lines[i]:
            continue
        start_raw = lines[i].split("-->")[0].strip()
        start = parse_srt_timestamp(start_raw)
        if start is None:
            continue
        text = " ".join(lines[i + 1 :])
        cues.append((start, text))
    return cues


def parse_vtt(content: str) -> list[tuple[float, str]]:
    cues: list[tuple[float, str]] = []
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    # drop header / NOTE / STYLE regions roughly by scanning cue blocks
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        # skip WEBVTT / NOTE / STYLE / REGION headers
        joined = "\n".join(lines)
        if lines[0].upper().startswith("WEBVTT") or lines[0].upper().startswith("NOTE"):
            continue
        if lines[0].upper().startswith("STYLE") or lines[0].upper().startswith("REGION"):
            continue
        i = 0
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            i = 1
        if i >= len(lines) or "-->" not in lines[i]:
            continue
        start_raw = lines[i].split("-->")[0].strip()
        start = parse_vtt_timestamp(start_raw)
        if start is None:
            continue
        body = " ".join(lines[i + 1 :])
        cues.append((start, body))
    return cues


def parse_ass(content: str) -> list[tuple[float, str]]:
    cues: list[tuple[float, str]] = []
    for raw in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line.lower().startswith("dialogue:"):
            continue
        # Dialogue: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
        payload = line.split(":", 1)[1].lstrip()
        parts = payload.split(",", 9)
        if len(parts) < 10:
            continue
        start = parse_ass_timestamp(parts[1].strip())
        if start is None:
            continue
        text = parts[9].replace("\\N", " ").replace("\\n", " ")
        cues.append((start, text))
    return cues


def parse_subtitle_file(path: Path) -> list[tuple[float, str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            content = None
    else:
        content = raw.decode("utf-8", errors="replace")

    ext = path.suffix.lower()
    if ext == ".srt":
        return parse_srt(content)
    if ext == ".vtt":
        return parse_vtt(content)
    if ext in (".ass", ".ssa"):
        return parse_ass(content)
    # fallback try srt
    return parse_srt(content)
