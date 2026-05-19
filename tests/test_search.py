"""Unit tests for the FTS5 search query builder."""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub `webview` (pywebview) so app.py imports cleanly without the desktop dep.
sys.modules.setdefault("webview", types.ModuleType("webview"))

from app import build_fts_query  # noqa: E402


class BuildFtsQueryTests(unittest.TestCase):
    def test_single_word_gets_auto_prefix(self):
        self.assertEqual(build_fts_query("renier"), "renier*")

    def test_multi_word_each_word_gets_prefix(self):
        self.assertEqual(build_fts_query("renier opg"), "renier* opg*")

    def test_three_words_all_prefixed(self):
        self.assertEqual(
            build_fts_query("vnx dispatch worker"),
            "vnx* dispatch* worker*",
        )

    def test_leading_and_trailing_whitespace_stripped(self):
        self.assertEqual(build_fts_query("  hello  "), "hello*")

    def test_extra_internal_whitespace_collapsed(self):
        self.assertEqual(build_fts_query("foo   bar"), "foo* bar*")

    def test_quoted_phrase_passes_through_untouched(self):
        self.assertEqual(build_fts_query('"exact phrase"'), '"exact phrase"')

    def test_plus_operator_passes_through(self):
        self.assertEqual(build_fts_query("foo +bar"), "foo +bar")

    def test_minus_operator_passes_through(self):
        self.assertEqual(build_fts_query("foo -bar"), "foo -bar")

    def test_empty_string_returns_empty(self):
        self.assertEqual(build_fts_query(""), "")

    def test_whitespace_only_returns_empty(self):
        self.assertEqual(build_fts_query("   "), "")


class FtsQueryAgainstSqliteTests(unittest.TestCase):
    """End-to-end check that the produced query is valid FTS5 and matches as expected."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='unicode61')"
            )
        except sqlite3.OperationalError:
            self.skipTest("SQLite build lacks FTS5 support")
        self.conn.executemany(
            "INSERT INTO docs(body) VALUES (?)",
            [
                ("renier opgplastic factuur",),
                ("renier zonder match",),
                ("alleen opgplastic",),
                ("ongerelateerd document",),
            ],
        )

    def tearDown(self):
        self.conn.close()

    def _match_count(self, query: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM docs WHERE docs MATCH ?", (query,)
        )
        return cur.fetchone()[0]

    def test_multi_word_prefix_matches_partial_words(self):
        query = build_fts_query("renier opg")
        self.assertEqual(query, "renier* opg*")
        self.assertEqual(self._match_count(query), 1)

    def test_single_word_prefix_matches_multiple_rows(self):
        query = build_fts_query("renier")
        self.assertEqual(self._match_count(query), 2)


if __name__ == "__main__":
    unittest.main()
