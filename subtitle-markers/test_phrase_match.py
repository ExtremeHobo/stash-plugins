"""Local unit tests for phrase expansion / subtitle parsing (no Stash writes)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from phrase_match import (
    CompiledMarker,
    compile_markers,
    cue_matches,
    expand_slash_alternations,
    normalize_text,
)
from subtitle_markers import DEFAULT_DEDUPE_SECONDS, filter_deduped_hits, marker_exists_near
from subtitle_parse import parse_srt, parse_subtitle_file


class PhraseMatchTests(unittest.TestCase):
    def test_slash_expand(self):
        out = expand_slash_alternations("You're going/gonna to make me cum")
        self.assertIn("You're going to make me cum", out)
        self.assertIn("You're gonna to make me cum", out)
        self.assertEqual(len(out), 2)

    def test_multi_slash(self):
        out = expand_slash_alternations("a/b c/d")
        self.assertEqual(set(out), {"a c", "a d", "b c", "b d"})

    def test_normalize_quirks(self):
        raw = "I'm <i>going</i> to cum!!!"
        self.assertEqual(normalize_text(raw), "i'm going to cum")
        self.assertEqual(normalize_text("You're gonna to make me cum."), "you're gonna to make me cum")

    def test_user_example_matches(self):
        compiled = compile_markers(
            [
                {
                    "title": "I'm cumming",
                    "phrases": [
                        "I'm cumming",
                        "I'm going to cum",
                        "I'm going to fucking cum",
                        "You're going/gonna to make me cum",
                    ],
                }
            ]
        )
        self.assertEqual(len(compiled), 1)
        cm = compiled[0]
        samples = [
            "I'm cumming!",
            "oh god i'm going to cum",
            "I'm going to fucking cum right now",
            "You're going to make me cum",
            "You're gonna to make me cum",
            "<font>You're gonna to make me cum</font>",
        ]
        for s in samples:
            self.assertTrue(cue_matches(normalize_text(s), cm.phrases_norm), s)
        self.assertFalse(cue_matches(normalize_text("hello world"), cm.phrases_norm))


class SrtParseTests(unittest.TestCase):
    def test_parse_srt_cue_start(self):
        content = """1
00:01:02,500 --> 00:01:04,000
You're gonna to make me cum

2
00:02:00,000 --> 00:02:01,000
Nope
"""
        cues = parse_srt(content)
        self.assertEqual(cues[0][0], 62.5)
        self.assertIn("gonna", cues[0][1])

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.srt"
            p.write_text(content, encoding="utf-8")
            cues2 = parse_subtitle_file(p)
            self.assertEqual(cues2[0][0], 62.5)


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.cm = CompiledMarker(
            title="I'm cumming",
            phrases_raw=("i'm cumming",),
            phrases_norm=("i'm cumming",),
        )
        self.other = CompiledMarker(
            title="Other",
            phrases_raw=("hello",),
            phrases_norm=("hello",),
        )
        self.sub = Path("fake.srt")

    def _hit(self, cm: CompiledMarker, sec: float):
        return (cm, sec, "cue", self.sub)

    def test_default_window_is_30(self):
        self.assertEqual(DEFAULT_DEDUPE_SECONDS, 30.0)

    def test_cluster_keeps_earliest_only(self):
        # Unsorted on purpose: later cue listed first must still lose to earliest.
        hits = [
            self._hit(self.cm, 25.0),
            self._hit(self.cm, 5.0),
            self._hit(self.cm, 18.0),
            self._hit(self.cm, 50.0),  # 45s after first kept → outside 30s window
        ]
        kept, skipped = filter_deduped_hits(hits, existing=[], window=30.0)
        times = [h[1] for h in kept]
        self.assertEqual(times, [5.0, 50.0])
        self.assertEqual(skipped, 2)

    def test_existing_same_title_blocks_near_duplicate(self):
        hits = [self._hit(self.cm, 42.0), self._hit(self.cm, 100.0)]
        existing = [{"title": "I'm cumming", "seconds": 30.0}]
        kept, skipped = filter_deduped_hits(hits, existing=existing, window=30.0)
        self.assertEqual([h[1] for h in kept], [100.0])
        self.assertEqual(skipped, 1)

    def test_same_run_create_blocks_later_near_hit(self):
        hits = [self._hit(self.cm, 10.0), self._hit(self.cm, 35.0)]
        kept, skipped = filter_deduped_hits(hits, existing=[], window=30.0)
        self.assertEqual([h[1] for h in kept], [10.0])
        self.assertEqual(skipped, 1)

    def test_different_title_not_deduped(self):
        hits = [self._hit(self.cm, 10.0), self._hit(self.other, 12.0)]
        kept, skipped = filter_deduped_hits(hits, existing=[], window=30.0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(skipped, 0)

    def test_marker_exists_near_helper(self):
        existing = [{"title": "I'm cumming", "seconds": 100.0}]
        self.assertTrue(marker_exists_near(existing, "I'm cumming", 120.0, 30.0))
        self.assertFalse(marker_exists_near(existing, "I'm cumming", 140.0, 30.0))
        self.assertFalse(marker_exists_near(existing, "Other", 100.0, 30.0))


if __name__ == "__main__":
    unittest.main()
