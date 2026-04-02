#!/usr/bin/env python3
"""
Configuration management for Claude Code Session Dashboard.
"""

import platform
from pathlib import Path
from shutil import which

# ── Paths ──────────────────────────────────────────────────────────
APP_NAME = "Claude Code Session Dashboard"
APP_SLUG = "claude-session-dashboard"
LEGACY_APP_SLUG = "claude-conversation-manager"
CLAUDE_DIR = Path.home() / ".claude"
DB_PATH = CLAUDE_DIR / "conversation-index.db"
CONFIG_DIR = Path.home() / ".config" / APP_SLUG
CONFIG_FILE = CONFIG_DIR / "config.yaml"
LEGACY_CONFIG_FILE = Path.home() / ".config" / LEGACY_APP_SLUG / "config.yaml"

# ── Defaults ───────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Root directory containing Claude Code session folders.
    "claude_projects_dir": str(CLAUDE_DIR / "projects"),
    # Which project directories to index. Empty = all session folders.
    "project_filters": [],
    # Subdirectory names to skip.
    "skip_subdirs": ["scheduled_jobs", "subagents"],
    # Root directory for the optional file browser panel. Empty = auto-detect.
    "file_browser_root": "",
    # Preferred terminal: auto, terminal, iterm, warp, vscode, system, windows
    "terminal": "auto",
    # Window dimensions
    "window_width": 1560,
    "window_height": 980,
}


def _detect_terminal() -> str:
    """Auto-detect the best available terminal."""
    system = platform.system()
    if system == "Darwin":
        apps = Path("/Applications")
        if (apps / "iTerm.app").exists():
            return "iterm"
        if (apps / "Warp.app").exists():
            return "warp"
        return "terminal"
    if system == "Linux":
        for candidate in ("x-terminal-emulator", "gnome-terminal", "konsole", "wezterm", "xterm"):
            if which(candidate):
                return "system"
        return "system"
    if system == "Windows":
        if which("wt.exe") or which("powershell") or which("cmd.exe"):
            return "windows"
    return "terminal"


def get_projects_dir(config: dict | None = None) -> Path:
    """Return the configured Claude projects directory."""
    resolved = (config or get_config()).get("claude_projects_dir") or str(CLAUDE_DIR / "projects")
    return Path(resolved).expanduser()


def _read_yamlish_file(path: Path, config: dict) -> dict:
    """Load YAML if available, otherwise parse simple key/value pairs."""
    try:
        import yaml
        with open(path) as f:
            user_config = yaml.safe_load(f) or {}
        config.update(user_config)
        return config
    except ImportError:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key not in config:
                    continue
                if isinstance(config[key], list):
                    config[key] = [v.strip() for v in value.split(",") if v.strip()]
                elif isinstance(config[key], int):
                    try:
                        config[key] = int(value)
                    except ValueError:
                        pass
                else:
                    config[key] = value
        return config
    except Exception:
        return config


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
        config = _read_yamlish_file(CONFIG_FILE, config)
    elif LEGACY_CONFIG_FILE.exists():
        config = _read_yamlish_file(LEGACY_CONFIG_FILE, config)

    # Auto-detect values that weren't explicitly set
    if config["terminal"] == "auto":
        config["terminal"] = _detect_terminal()

    config["claude_projects_dir"] = str(Path(config["claude_projects_dir"]).expanduser())
    if not config["file_browser_root"]:
        config["file_browser_root"] = _detect_file_browser_root()

    return config


def save_default_config():
    """Write a default config file with comments if none exists."""
    if CONFIG_FILE.exists() or LEGACY_CONFIG_FILE.exists():
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(f"""# {APP_NAME} configuration

# Claude Code sessions root. The default matches the standard Claude Code path.
claude_projects_dir: ~/.claude/projects

# Optional folder name filters. Leave empty to index every Claude Code project.
# Example: marketing-site, api, mono-repo
project_filters:

# Subdirectory names to skip while scanning the Claude projects directory.
skip_subdirs: scheduled_jobs, subagents

# Root directory for the file browser panel. Leave empty to auto-detect.
file_browser_root:

# Preferred terminal: auto, terminal, iterm, warp, vscode, system, windows
terminal: auto

# Window size
window_width: 1560
window_height: 980
""")


# ── Singleton ──────────────────────────────────────────────────────
_config = None


def get_config() -> dict:
    """Get the current configuration (cached)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
