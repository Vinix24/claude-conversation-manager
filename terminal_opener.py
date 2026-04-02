#!/usr/bin/env python3
"""
Terminal opener for Claude Code session resume commands.
Supports macOS terminals and common Linux terminal launchers.
"""

import platform
import shlex
from pathlib import Path
from shutil import which
import subprocess

from config import get_config


def _escape_applescript(s: str) -> str:
    """Escape a string for use inside AppleScript double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _resume_command(session_id: str, cwd: str) -> str:
    command = f"claude -r {shlex.quote(session_id)}"
    if cwd:
        return f"cd {shlex.quote(cwd)} && {command}"
    return command


def open_in_iterm(session_id: str, cwd: str):
    """Open a new iTerm2 tab with claude -r."""
    cmd = _escape_applescript(_resume_command(session_id, cwd))
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
    cmd = _escape_applescript(_resume_command(session_id, cwd))
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
    subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    # Then use pbcopy fallback
    subprocess.run(["pbcopy"], input=_resume_command(session_id, cwd).encode(), check=False)


def open_in_vscode(session_id: str, cwd: str):
    """Open VS Code and create a fresh integrated terminal with claude -r.

    This is a best-effort path that currently targets macOS.
    """
    if platform.system() != "Darwin":
        raise RuntimeError("VS Code integrated terminal resume is currently supported on macOS only.")

    target_dir = cwd or str(Path.home())
    if which("code"):
        result = subprocess.run(
            ["code", "-r", target_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"VS Code CLI failed: {result.stderr.strip()}")
    elif (Path("/Applications") / "Visual Studio Code.app").exists():
        subprocess.run(["open", "-a", "Visual Studio Code", target_dir], check=True)
    else:
        raise RuntimeError("VS Code was not found. Install VS Code or add the `code` CLI to your PATH.")

    palette_command = _escape_applescript("Terminal: Create New Terminal")
    terminal_command = _escape_applescript(_resume_command(session_id, cwd))
    applescript = f'''
tell application "Visual Studio Code"
    activate
end tell
delay 0.7
tell application "System Events"
    tell process "Code"
        keystroke "p" using {{command down, shift down}}
        delay 0.2
        keystroke "{palette_command}"
        delay 0.2
        key code 36
        delay 0.35
        keystroke "{terminal_command}"
        key code 36
    end tell
end tell
'''
    result = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"VS Code terminal automation failed: {result.stderr.strip()}")


def open_in_linux_terminal(session_id: str, cwd: str):
    """Open a Claude resume command in a common Linux terminal."""
    resume_command = _resume_command(session_id, cwd)
    command = f"{resume_command}; exec bash"
    candidates = [
        ["x-terminal-emulator", "-e", "bash", "-lc", command],
        ["gnome-terminal", "--", "bash", "-lc", command],
        ["konsole", "-e", "bash", "-lc", command],
        ["wezterm", "start", "--cwd", cwd, "bash", "-lc", command],
        ["xterm", "-e", "bash", "-lc", command],
    ]
    for candidate in candidates:
        try:
            subprocess.Popen(candidate)
            return
        except FileNotFoundError:
            continue
    raise RuntimeError("No supported Linux terminal launcher found. Install x-terminal-emulator, gnome-terminal, konsole, wezterm, or xterm.")


def open_in_windows_terminal(session_id: str, cwd: str):
    """Open a Claude resume command in Windows Terminal or PowerShell."""
    resume_command = _resume_command(session_id, cwd)
    target_dir = cwd or str(Path.home())
    command = f'cd "{target_dir}" && {resume_command}'

    if which("wt.exe"):
        subprocess.Popen(["wt.exe", "powershell", "-NoExit", "-Command", command])
        return

    if which("powershell"):
        subprocess.Popen(["powershell", "-NoExit", "-Command", command])
        return

    if which("cmd.exe"):
        subprocess.Popen(["cmd.exe", "/k", command])
        return

    raise RuntimeError("No supported Windows terminal launcher found. Install Windows Terminal or PowerShell.")


def open_session(session_id: str, cwd: str, terminal: str = None):
    """Open a session in the configured terminal."""
    if terminal is None:
        terminal = get_config().get("terminal", "auto")

    openers = {
        "iterm": open_in_iterm,
        "terminal": open_in_terminal_app,
        "warp": open_in_warp,
        "vscode": open_in_vscode,
        "system": open_in_linux_terminal,
        "windows": open_in_windows_terminal,
    }

    # Fallback chain
    opener = openers.get(terminal)
    if opener is None:
        if platform.system() == "Darwin":
            opener = open_in_terminal_app
        elif platform.system() == "Linux":
            opener = open_in_linux_terminal
        elif platform.system() == "Windows":
            opener = open_in_windows_terminal
        else:
            raise RuntimeError(f"Unsupported terminal: {terminal}. Resume-in-terminal currently supports macOS and Linux.")

    opener(session_id, cwd)
