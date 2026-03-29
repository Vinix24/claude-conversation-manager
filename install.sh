#!/bin/bash
# Install Claude Conversation Manager
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.claude-conversation-manager.indexer.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "=== Claude Conversation Manager ==="
echo

# 1. Setup venv
if [ ! -d "$DIR/.venv" ]; then
    echo "[1/4] Creating Python virtual environment..."
    python3 -m venv "$DIR/.venv"
    "$DIR/.venv/bin/pip" install -q pywebview
else
    echo "[1/4] Virtual environment exists"
fi

# 2. Run initial index
echo "[2/4] Indexing conversations..."
"$DIR/.venv/bin/python3" "$DIR/indexer.py"

# 3. Install launchd plist (macOS only)
if [ "$(uname)" = "Darwin" ]; then
    echo
    echo "[3/4] Installing auto-indexer (launchd)..."
    if [ -f "$PLIST_DST" ]; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi
    cat > "$PLIST_DST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/.venv/bin/python3</string>
        <string>$DIR/indexer.py</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>$HOME/.claude/projects</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>StandardOutPath</key>
    <string>/tmp/claude-conversation-indexer.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-conversation-indexer.log</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
PLIST
    launchctl load "$PLIST_DST"
    echo "  Auto-indexer installed (runs every 30 min + on file changes)"
else
    echo "[3/4] Skipping launchd (not macOS)"
fi

# 4. Done
echo
echo "[4/4] Done!"
echo
echo "  Start:  $DIR/run.sh"
echo "  Config: ~/.config/claude-conversation-manager/config.yaml"
echo
echo "  Add to your shell profile:"
echo "    alias ccm='$DIR/run.sh'"
