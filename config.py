#!/usr/bin/env python3
"""
Configuration management for Claude Conversation Manager.
Reads from ~/.config/claude-conversation-manager/config.yaml or auto-detects.
"""

import os
import platform
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
DB_PATH = CLAUDE_DIR / "conversation-index.db"
CONFIG_DIR = Path.home() / ".config" / "claude-conversation-manager"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

# ── Defaults ───────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Which project directories to index. Empty = ALL projects in ~/.claude/projects/
    "project_filters": [],
    # Subdirectory names to skip (automated jobs, subagents)
    "skip_subdirs": ["scheduled_jobs", "subagents"],
    # Root directory for the file browser panel. Empty = home directory
    "file_browser_root": "",
    # Preferred terminal: "iterm", "terminal", "warp", "auto"
    "terminal": "auto",
    # UI language: "en" (only English for now)
    "language": "en",
    # Window dimensions
    "window_width": 1400,
    "window_height": 900,
}


def _detect_terminal() -> str:
    """Auto-detect the best available terminal on macOS."""
    if platform.system() != "Darwin":
        return "terminal"
    apps = Path("/Applications")
    if (apps / "iTerm.app").exists():
        return "iterm"
    if (apps / "Warp.app").exists():
        return "warp"
    return "terminal"


def _detect_file_browser_root() -> str:
    """Try to detect a sensible file browser root from indexed projects."""
    # Look for common project roots
    for candidate in [
        Path.home() / "Desktop",
        Path.home() / "Development",
        Path.home() / "Projects",
        Path.home() / "Code",
        Path.home() / "dev",
    ]:
        if candidate.exists():
            return str(candidate)
    return str(Path.home())


def load_config() -> dict:
    """Load configuration from file, falling back to auto-detected defaults."""
    config = dict(DEFAULT_CONFIG)

    if CONFIG_FILE.exists():
        try:
            import yaml
            with open(CONFIG_FILE) as f:
                user_config = yaml.safe_load(f) or {}
            config.update(user_config)
        except ImportError:
            # yaml not installed — try simple key: value parsing
            with open(CONFIG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ":" in line:
                        key, _, value = line.partition(":")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key in config:
                            if isinstance(config[key], list):
                                config[key] = [v.strip() for v in value.split(",") if v.strip()]
                            elif isinstance(config[key], int):
                                try:
                                    config[key] = int(value)
                                except ValueError:
                                    pass
                            else:
                                config[key] = value
        except Exception:
            pass

    # Auto-detect values that weren't explicitly set
    if config["terminal"] == "auto":
        config["terminal"] = _detect_terminal()

    if not config["file_browser_root"]:
        config["file_browser_root"] = _detect_file_browser_root()

    return config


def save_default_config():
    """Write a default config file with comments if none exists."""
    if CONFIG_FILE.exists():
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("""# Claude Conversation Manager Configuration
# https://github.com/Vinix24/claude-conversation-manager

# Which project directories to index (filter by name).
# Leave empty to index ALL projects in ~/.claude/projects/
# Example: BUSINESS, SEOcrawler, my-project
project_filters:

# Subdirectory names to skip when indexing (automated jobs, etc.)
skip_subdirs: scheduled_jobs, subagents

# Root directory for the file browser panel.
# Leave empty to auto-detect from your home directory.
file_browser_root:

# Preferred terminal: iterm, terminal, warp, auto
terminal: auto

# Window size
window_width: 1400
window_height: 900
""")


# ── Singleton ──────────────────────────────────────────────────────
_config = None


def get_config() -> dict:
    """Get the current configuration (cached)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
