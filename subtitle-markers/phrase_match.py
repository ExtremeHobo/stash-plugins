"""Phrase expansion and normalized subtitle matching."""

from __future__ import annotations

import html
import itertools
import re
from dataclasses import dataclass


_TAG_RE = re.compile(r"<[^>]+>")
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s']+")
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, strip tags/overrides/punctuation, collapse whitespace."""
    if not text:
        return ""
    t = html.unescape(text)
    t = _TAG_RE.sub(" ", t)
    t = _ASS_OVERRIDE_RE.sub(" ", t)
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    t = t.lower()
    t = _NON_ALNUM_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def expand_slash_alternations(phrase: str) -> list[str]:
    """
    Expand slash alternatives inside a phrase into all combinations.

    Tokenizes on whitespace. A token containing '/' becomes alternatives for
    that token only (e.g. going/gonna). Nested slashes in one token all count
    as alternatives for that token.
    """
    if not phrase or not phrase.strip():
        return []
    tokens = phrase.strip().split()
    options: list[list[str]] = []
    for tok in tokens:
        if "/" in tok:
            parts = [p for p in tok.split("/") if p != ""]
            options.append(parts or [tok])
        else:
            options.append([tok])
    combos = [" ".join(parts) for parts in itertools.product(*options)]
    # stable unique
    seen: set[str] = set()
    out: list[str] = []
    for c in combos:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


@dataclass(frozen=True)
class CompiledMarker:
    title: str
    phrases_raw: tuple[str, ...]
    phrases_norm: tuple[str, ...]


def compile_markers(markers_cfg: list[dict]) -> list[CompiledMarker]:
    compiled: list[CompiledMarker] = []
    for entry in markers_cfg:
        title = (entry.get("title") or "").strip()
        phrases = entry.get("phrases") or []
        if not title or not phrases:
            continue
        expanded: list[str] = []
        for p in phrases:
            if not isinstance(p, str) or not p.strip():
                continue
            expanded.extend(expand_slash_alternations(p.strip()))
        norms = []
        seen_n: set[str] = set()
        for e in expanded:
            n = normalize_text(e)
            if n and n not in seen_n:
                seen_n.add(n)
                norms.append(n)
        if norms:
            compiled.append(
                CompiledMarker(
                    title=title,
                    phrases_raw=tuple(expanded),
                    phrases_norm=tuple(norms),
                )
            )
    return compiled


def cue_matches(normalized_cue: str, phrases_norm: tuple[str, ...]) -> bool:
    if not normalized_cue:
        return False
    for p in phrases_norm:
        if p and p in normalized_cue:
            return True
    return False
