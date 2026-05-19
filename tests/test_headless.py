"""Unit tests for the headless-session scoring heuristics."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headless import (  # noqa: E402
    DEFAULT_CWD_PATTERNS,
    DEFAULT_DISPATCH_PATTERNS,
    DEFAULT_SCORE_THRESHOLD,
    SESSION_TYPE_HEADLESS,
    SESSION_TYPE_INTERACTIVE,
    score_from_config,
    score_session_headlessness,
)


def _ts(seconds_ago: int) -> str:
    """Helper: build a UTC ISO timestamp N seconds in the past."""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    # Render with the `Z` suffix Claude Code emits, so the parser exercise is real.
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ScoreSessionHeadlessnessTests(unittest.TestCase):
    def _make(self, **overrides):
        base = {
            "message_count": 12,
            "first_prompt": "Hey, can you help me with the report?",
            "cwd": "/Users/me/Development/some-real-repo",
            "first_timestamp": _ts(3600),
            "last_timestamp": _ts(0),
        }
        base.update(overrides)
        return score_session_headlessness(**base)

    def test_normal_interactive_session_scores_zero(self):
        result = self._make()
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["session_type"], SESSION_TYPE_INTERACTIVE)

    def test_single_signal_stays_interactive(self):
        # Short session by itself isn't enough — many real conversations are brief.
        result = self._make(message_count=2)
        self.assertEqual(result["score"], 1)
        self.assertIn("low_turn_count", result["signals"])
        self.assertEqual(result["session_type"], SESSION_TYPE_INTERACTIVE)

    def test_two_signals_flips_to_headless(self):
        # Worktree cwd + short session = classic VNX worker dispatch.
        result = self._make(
            message_count=2,
            cwd="/Users/me/Development/seocrawler-wt-T1",
        )
        self.assertEqual(result["score"], 2)
        self.assertEqual(set(result["signals"]), {"low_turn_count", "worktree_cwd"})
        self.assertEqual(result["session_type"], SESSION_TYPE_HEADLESS)

    def test_dispatch_first_message_is_one_signal(self):
        result = self._make(first_prompt="# Dispatch: rebuild the indexer schema")
        self.assertEqual(result["signals"], ["dispatch_first_message"])
        # On its own, still interactive — a human might paste a dispatch-style prompt.
        self.assertEqual(result["session_type"], SESSION_TYPE_INTERACTIVE)

    def test_dispatch_plus_short_duration_is_headless(self):
        result = self._make(
            first_prompt="# Dispatch: VNX_DISPATCH worker run",
            first_timestamp=_ts(40),
            last_timestamp=_ts(0),
        )
        self.assertEqual(result["score"], 2)
        self.assertEqual(result["session_type"], SESSION_TYPE_HEADLESS)

    def test_all_four_signals_max_score(self):
        result = self._make(
            message_count=1,
            first_prompt="## Critical rules: dispatch_id=abc",
            cwd="/Users/me/.claude/terminals/session-x",
            first_timestamp=_ts(10),
            last_timestamp=_ts(0),
        )
        self.assertEqual(result["score"], 4)
        self.assertEqual(
            set(result["signals"]),
            {"low_turn_count", "dispatch_first_message", "worktree_cwd", "short_duration"},
        )
        self.assertEqual(result["session_type"], SESSION_TYPE_HEADLESS)

    def test_missing_timestamps_do_not_crash(self):
        result = self._make(first_timestamp=None, last_timestamp=None)
        self.assertNotIn("short_duration", result["signals"])

    def test_malformed_timestamps_treated_as_missing(self):
        result = self._make(first_timestamp="not-a-date", last_timestamp="also-bad")
        self.assertNotIn("short_duration", result["signals"])

    def test_empty_first_prompt_no_dispatch_signal(self):
        result = self._make(first_prompt="")
        self.assertNotIn("dispatch_first_message", result["signals"])

    def test_none_cwd_no_worktree_signal(self):
        result = self._make(cwd=None)
        self.assertNotIn("worktree_cwd", result["signals"])

    def test_custom_threshold_can_demand_three_signals(self):
        # Tighter threshold means a 2-signal session stays interactive.
        result = score_session_headlessness(
            message_count=2,
            first_prompt="just a quick check",
            cwd="/Users/me/Development/repo-wt-feature",
            first_timestamp=_ts(3600),
            last_timestamp=_ts(0),
            score_threshold=3,
        )
        self.assertEqual(result["score"], 2)
        self.assertEqual(result["session_type"], SESSION_TYPE_INTERACTIVE)

    def test_custom_dispatch_patterns_respected(self):
        result = score_session_headlessness(
            message_count=8,
            first_prompt="MY_CUSTOM_MARKER hello",
            cwd="/Users/me/normal/path",
            first_timestamp=_ts(3600),
            last_timestamp=_ts(0),
            dispatch_patterns=("MY_CUSTOM_MARKER",),
        )
        self.assertIn("dispatch_first_message", result["signals"])

    def test_default_patterns_cover_roadmap_examples(self):
        # Sanity check: the roadmap's example markers all match the defaults.
        for marker in ("# Dispatch:", "## Critical rules", "dispatch_id"):
            self.assertTrue(
                any(marker in pattern or pattern in marker for pattern in DEFAULT_DISPATCH_PATTERNS),
                f"{marker!r} not covered by DEFAULT_DISPATCH_PATTERNS",
            )
        for marker in ("-wt-", "/worktree/", "/.claude/terminals/"):
            self.assertIn(marker, DEFAULT_CWD_PATTERNS)

    def test_threshold_default_is_two(self):
        self.assertEqual(DEFAULT_SCORE_THRESHOLD, 2)


class ScoreFromConfigTests(unittest.TestCase):
    def test_pulls_overrides_from_config(self):
        session = {
            "message_count": 2,
            "first_prompt": "ZZZ marker",
            "cwd": "/some/path",
            "first_message": _ts(3600),
            "last_message": _ts(0),
        }
        config = {
            "headless_max_turns": 4,
            "headless_max_duration_seconds": 60,
            "headless_score_threshold": 1,
            "headless_dispatch_patterns": ["ZZZ"],
            "headless_cwd_patterns": ["/never/matches"],
        }
        result = score_from_config(session, config)
        # low_turn_count + dispatch_first_message = 2, threshold = 1 → headless
        self.assertEqual(result["session_type"], SESSION_TYPE_HEADLESS)
        self.assertEqual(set(result["signals"]), {"low_turn_count", "dispatch_first_message"})

    def test_missing_config_keys_fall_back_to_defaults(self):
        session = {
            "message_count": 50,
            "first_prompt": "normal interactive question",
            "cwd": "/Users/me/repo",
            "first_message": _ts(3600),
            "last_message": _ts(0),
        }
        result = score_from_config(session, {})
        self.assertEqual(result["session_type"], SESSION_TYPE_INTERACTIVE)


if __name__ == "__main__":
    unittest.main()
