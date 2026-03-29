#!/usr/bin/env python3
"""
Terminal opener for macOS — opens a terminal with claude -r command.
Supports iTerm2, Terminal.app, and Warp.
"""

import platform
import subprocess

from config import get_config


def _escape_applescript(s: str) -> str:
    """Escape a string for use inside AppleScript double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def open_in_iterm(session_id: str, cwd: str):
    """Open a new iTerm2 tab with claude -r."""
    cmd = _escape_applescript(f'cd "{cwd}" && claude -r {session_id}')
    applescript = f'''
tell application "iTerm2"
    activate
    if (count of windows) = 0 then
        create window with default profile
    end if
    tell current window
        create tab with default profile
        tell current session
            write text "{cmd}"
        end tell
    end tell
end tell
'''
    result = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"iTerm2 failed: {result.stderr}")


def open_in_terminal_app(session_id: str, cwd: str):
    """Open macOS Terminal.app with claude -r."""
    cmd = _escape_applescript(f'cd "{cwd}" && claude -r {session_id}')
    applescript = f'''
tell application "Terminal"
    activate
    do script "{cmd}"
end tell
'''
    result = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Terminal.app failed: {result.stderr}")


def open_in_warp(session_id: str, cwd: str):
    """Open Warp terminal with claude -r."""
    cmd = _escape_applescript(f'cd "{cwd}" && claude -r {session_id}')
    applescript = f'''
tell application "Warp"
    activate
end tell
delay 0.5
tell application "System Events"
    tell process "Warp"
        keystroke "t" using command down
        delay 0.3
    end tell
end tell
'''
    # Warp doesn't have great AppleScript support — fallback to open command
    result = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    # Then use pbcopy fallback
    subprocess.run(["pbcopy"], input=f"cd {cwd} && claude -r {session_id}".encode())


def open_session(session_id: str, cwd: str, terminal: str = None):
    """Open a session in the configured terminal."""
    if terminal is None:
        terminal = get_config().get("terminal", "auto")

    openers = {
        "iterm": open_in_iterm,
        "terminal": open_in_terminal_app,
        "warp": open_in_warp,
    }

    # Fallback chain
    opener = openers.get(terminal)
    if opener is None:
        if platform.system() == "Darwin":
            opener = open_in_terminal_app
        else:
            raise RuntimeError(f"Unsupported terminal: {terminal}. macOS only for now.")

    opener(session_id, cwd)
