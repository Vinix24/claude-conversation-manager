# Claude Conversation Manager

A desktop app to browse, search, and resume your [Claude Code](https://claude.ai/code) conversations.

Claude Code stores conversations as JSONL files in `~/.claude/projects/`, but finding old sessions is hard — especially across multiple projects and subdirectories. This tool indexes everything into a searchable SQLite database and provides a visual interface to browse and resume conversations.

## Features

- **Full-text search** across conversation titles, excerpts, and message content (SQLite FTS5)
- **Auto-generated titles** from the first user prompt (handles plan titles, skill invocations, slash commands)
- **Project tree sidebar** with collapsible folders and conversation counts
- **Chat preview** with markdown rendering and syntax highlighting
- **File browser panel** — browse your project files alongside conversations (lightweight VS Code alternative)
- **Resume in terminal** — one click opens iTerm2, Terminal.app, or Warp with `claude -r <session-id>`
- **Export** conversations as Markdown or JSON
- **Resizable columns** — drag to resize all panels
- **Auto-indexer** — launchd daemon re-indexes on file changes (macOS)
- **Scroll to bottom** for long conversations
- **Dark theme** with Claude-inspired colors

## Requirements

- **macOS** (uses native webview and AppleScript for terminal integration)
- **Python 3.10+**
- **Claude Code** installed (`~/.claude/` directory must exist)

## Install

```bash
git clone https://github.com/Vinix24/claude-conversation-manager.git
cd claude-conversation-manager
./install.sh
```

This will:
1. Create a Python virtual environment and install `pywebview`
2. Index all your Claude Code conversations into SQLite
3. Install a launchd daemon that auto-indexes every 30 minutes and on file changes

## Usage

```bash
# Launch the app
./run.sh

# Or add an alias to your .zshrc / .bashrc
alias ccm='~/path/to/claude-conversation-manager/run.sh'

# Re-index manually
.venv/bin/python3 indexer.py

# Force re-index everything
.venv/bin/python3 indexer.py --force
```

## Configuration

On first run, a config file is created at `~/.config/claude-conversation-manager/config.yaml`:

```yaml
# Which project directories to index (filter by name).
# Leave empty to index ALL projects in ~/.claude/projects/
project_filters:

# Subdirectory names to skip when indexing
skip_subdirs: scheduled_jobs, subagents

# Root directory for the file browser panel
file_browser_root:

# Preferred terminal: iterm, terminal, warp, auto
terminal: auto

# Window size
window_width: 1400
window_height: 900
```

## Architecture

```
~/.claude/projects/          JSONL conversation files (source)
        |
   indexer.py                Parses JSONL → SQLite + FTS5
        |
~/.claude/conversation-index.db    Indexed database
        |
   app.py + pywebview        Desktop UI (HTML/CSS/JS)
        |
   terminal_opener.py        AppleScript → iTerm/Terminal/Warp
```

### Database Schema

- **conversations** — session metadata, auto-generated titles, excerpts, token counts
- **messages** — individual messages with role, content, timestamps (RAG-ready with embedding column)
- **conversations_fts** / **messages_fts** — FTS5 virtual tables for full-text search

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Cmd+F` | Focus search bar |
| `Escape` | Clear search |

## How It Works

1. **Indexer** scans all `~/.claude/projects/*/` directories for `.jsonl` files
2. Each file is parsed to extract messages, timestamps, token usage, and working directory
3. A title is auto-generated from the first meaningful user prompt
4. Everything is stored in SQLite with FTS5 indexes for instant search
5. The desktop app (pywebview) displays the data in a 3-panel layout
6. Clicking "Resume" opens a new terminal tab with `claude -r <session-id>`

## License

MIT
