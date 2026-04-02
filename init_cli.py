#!/usr/bin/env python3
"""
Initialize the local Claude Code Session Dashboard environment.
Creates config and builds the initial index when possible.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import APP_NAME, CLAUDE_DIR, load_config, save_default_config, get_projects_dir
from indexer import run_index


def _project_dir_has_jsonl(project_dir: Path) -> bool:
    try:
        next(project_dir.glob("*.jsonl"))
        return True
    except StopIteration:
        return False


def _projects_root_has_sessions(projects_dir: Path) -> bool:
    if not projects_dir.exists():
        return False
    for child in projects_dir.iterdir():
        if child.is_dir() and _project_dir_has_jsonl(child):
            return True
    return False


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Initialize Claude Code Session Dashboard.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild every session even if files are unchanged.",
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Only write the default config; skip indexing.",
    )
    args = parser.parse_args(argv)

    print(f"{APP_NAME} init")
    print()

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    save_default_config()
    config = load_config()
    projects_dir = get_projects_dir(config)

    print(f"Config: {projects_dir}")
    if args.skip_index:
        print("Skipping index build (--skip-index).")
        return 0

    if not _projects_root_has_sessions(projects_dir):
        print(f"No session files found under: {projects_dir}")
        print("Once Claude Code has created local sessions, run:")
        print("  claude-session-index --force")
        return 0

    print("Building index...")
    run_index(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
