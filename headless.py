#!/usr/bin/env python3
"""Heuristic detection of headless `claude -p` sessions.

A "headless" session is one started by a subprocess dispatch (VNX worker,
automation script, CI job) rather than an interactive Claude Code session
the user opened themselves. Headless sessions clutter the dashboard, so
the indexer scores each session against a handful of cheap signals and
the UI hides anything that crosses a configurable threshold.

The signals are intentionally weak on their own — any one of them can
also describe a normal interactive session that happened to be short or
that ran in a worktree. Requiring two independent signals before flipping
the verdict keeps false positives down on real conversations.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_DISPATCH_PATTERNS: tuple[str, ...] = (
    "# Dispatch:",
    "## Critical rules",
    "dispatch_id",
    "Task ID:",
    "VNX_DISPATCH",
)

DEFAULT_CWD_PATTERNS: tuple[str, ...] = (
    "-wt-",
    "/worktree/",
    "/.claude/terminals/",
)

DEFAULT_MAX_TURNS = 4
DEFAULT_MAX_DURATION_SECONDS = 60
DEFAULT_SCORE_THRESHOLD = 2

SESSION_TYPE_HEADLESS = "headless"
SESSION_TYPE_INTERACTIVE = "interactive"
SESSION_TYPE_UNKNOWN = "unknown"


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Claude Code timestamps end in `Z`; Python <3.11 won't parse that.
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_seconds(first: str | None, last: str | None) -> float | None:
    start = _parse_iso_timestamp(first)
    end = _parse_iso_timestamp(last)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def score_session_headlessness(
    *,
    message_count: int,
    first_prompt: str | None,
    cwd: str | None,
    first_timestamp: str | None,
    last_timestamp: str | None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    dispatch_patterns: tuple[str, ...] = DEFAULT_DISPATCH_PATTERNS,
    cwd_patterns: tuple[str, ...] = DEFAULT_CWD_PATTERNS,
    score_threshold: int = DEFAULT_SCORE_THRESHOLD,
) -> dict:
    """Score a session against headless-detection signals.

    Returns a dict with:
      - `score` (int): number of triggered signals (0..4)
      - `signals` (list[str]): names of the signals that fired
      - `session_type` (str): 'headless' if score >= threshold else 'interactive'
    """
    signals: list[str] = []

    if isinstance(message_count, int) and message_count < max_turns:
        signals.append("low_turn_count")

    if first_prompt and any(pattern in first_prompt for pattern in dispatch_patterns):
        signals.append("dispatch_first_message")

    if cwd and any(pattern in cwd for pattern in cwd_patterns):
        signals.append("worktree_cwd")

    duration = _duration_seconds(first_timestamp, last_timestamp)
    if duration is not None and duration < max_duration_seconds:
        signals.append("short_duration")

    score = len(signals)
    session_type = (
        SESSION_TYPE_HEADLESS if score >= score_threshold else SESSION_TYPE_INTERACTIVE
    )
    return {
        "score": score,
        "signals": signals,
        "session_type": session_type,
    }


def score_from_config(session: dict, config: dict) -> dict:
    """Convenience wrapper that pulls thresholds + patterns from a config dict."""
    return score_session_headlessness(
        message_count=session.get("message_count", 0),
        first_prompt=session.get("first_prompt"),
        cwd=session.get("cwd"),
        first_timestamp=session.get("first_message"),
        last_timestamp=session.get("last_message"),
        max_turns=int(config.get("headless_max_turns", DEFAULT_MAX_TURNS)),
        max_duration_seconds=int(
            config.get("headless_max_duration_seconds", DEFAULT_MAX_DURATION_SECONDS)
        ),
        dispatch_patterns=tuple(
            config.get("headless_dispatch_patterns") or DEFAULT_DISPATCH_PATTERNS
        ),
        cwd_patterns=tuple(
            config.get("headless_cwd_patterns") or DEFAULT_CWD_PATTERNS
        ),
        score_threshold=int(
            config.get("headless_score_threshold", DEFAULT_SCORE_THRESHOLD)
        ),
    )
