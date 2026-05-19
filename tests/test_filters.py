"""Unit tests for the days-active filter."""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub `webview` so app.py imports cleanly without the desktop dep.
sys.modules.setdefault("webview", types.ModuleType("webview"))

from app import (  # noqa: E402
    DAYS_ACTIVE_VALUES,
    build_days_active_clause,
    normalize_days_active,
)


class NormalizeDaysActiveTests(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(normalize_days_active(None))

    def test_string_all_returns_none(self):
        self.assertIsNone(normalize_days_active("all"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(normalize_days_active(""))

    def test_whitespace_string_returns_none(self):
        self.assertIsNone(normalize_days_active("   "))

    def test_whitelisted_int_passes(self):
        for value in DAYS_ACTIVE_VALUES:
            self.assertEqual(normalize_days_active(value), value)

    def test_whitelisted_string_int_passes(self):
        self.assertEqual(normalize_days_active("30"), 30)
        self.assertEqual(normalize_days_active(" 7 "), 7)

    def test_non_whitelisted_int_rejected(self):
        self.assertIsNone(normalize_days_active(5))
        self.assertIsNone(normalize_days_active(365))

    def test_non_whitelisted_string_rejected(self):
        self.assertIsNone(normalize_days_active("5"))
        self.assertIsNone(normalize_days_active("365"))

    def test_garbage_string_rejected(self):
        self.assertIsNone(normalize_days_active("DROP TABLE conversations"))
        self.assertIsNone(normalize_days_active("'; --"))


class BuildDaysActiveClauseTests(unittest.TestCase):
    def test_no_window_returns_empty(self):
        fragment, params = build_days_active_clause("c", None)
        self.assertEqual(fragment, "")
        self.assertEqual(params, [])

    def test_window_30_returns_clause_with_param(self):
        fragment, params = build_days_active_clause("c", 30)
        self.assertIn("datetime('now', ?)", fragment)
        self.assertIn("c.last_message", fragment)
        self.assertEqual(params, ["-30 days"])

    def test_invalid_window_silently_drops(self):
        # Non-whitelisted values fall back to no filter rather than raising,
        # so a stale UI value never produces a broken query.
        fragment, params = build_days_active_clause("c", 365)
        self.assertEqual(fragment, "")
        self.assertEqual(params, [])

    def test_alias_is_respected(self):
        fragment, _ = build_days_active_clause("conv", 7)
        self.assertIn("conv.last_message", fragment)


class DaysActiveAgainstSqliteTests(unittest.TestCase):
    """End-to-end check that the produced clause filters real rows by date."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE conversations (
                session_id TEXT PRIMARY KEY,
                first_message TEXT,
                last_message TEXT
            )
            """
        )
        # Use modifiers well clear of the window boundaries so clock-drift
        # between INSERT and the test query can't flip the comparison.
        self.conn.executemany(
            "INSERT INTO conversations VALUES ("
            "  ?, datetime('now', ?), datetime('now', ?)"
            ")",
            [
                ("recent",   "-10 minutes", "-10 minutes"),
                ("two_days", "-2 days",     "-2 days"),
                ("ten_days", "-10 days",    "-10 days"),
                ("old",      "-100 days",   "-100 days"),
            ],
        )

    def tearDown(self):
        self.conn.close()

    def _count(self, days_active) -> int:
        fragment, params = build_days_active_clause("c", days_active)
        query = "SELECT COUNT(*) FROM conversations c WHERE 1=1" + fragment
        return self.conn.execute(query, params).fetchone()[0]

    def test_1_day_window_keeps_only_today(self):
        self.assertEqual(self._count(1), 1)

    def test_7_day_window_includes_recent_and_two_days(self):
        self.assertEqual(self._count(7), 2)

    def test_30_day_window_includes_first_three(self):
        self.assertEqual(self._count(30), 3)

    def test_90_day_window_still_excludes_100_day_old(self):
        self.assertEqual(self._count(90), 3)

    def test_none_returns_all_rows(self):
        self.assertEqual(self._count(None), 4)


if __name__ == "__main__":
    unittest.main()
