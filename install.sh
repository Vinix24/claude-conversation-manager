#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$DIR/.venv"
PLIST_NAME="com.claude-session-dashboard.indexer.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOCAL_BIN_DIR="$HOME/.local/bin"
CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"

echo "=== Claude Code Session Dashboard ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required."
    exit 1
fi

echo "[1/5] Creating virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

echo "[2/5] Installing app dependencies..."
"$VENV_DIR/bin/python3" -m pip install --upgrade pip setuptools wheel >/dev/null
"$VENV_DIR/bin/python3" -m pip install -r "$DIR/requirements.txt" >/dev/null

echo "[3/5] Building the Claude Code index..."
if [ -d "$CLAUDE_PROJECTS_DIR" ]; then
    "$VENV_DIR/bin/python3" "$DIR/indexer.py"
else
    echo "  Skipping initial index: $CLAUDE_PROJECTS_DIR does not exist yet."
    echo "  The app will still start. Once Claude Code creates local sessions, run:"
    echo "    $LOCAL_BIN_DIR/claude-session-index"
fi

mkdir -p "$LOCAL_BIN_DIR"
ln -sf "$DIR/run.sh" "$LOCAL_BIN_DIR/claude-session-dashboard"
cat > "$LOCAL_BIN_DIR/claude-session-index" <<EOF
#!/bin/bash
exec "$VENV_DIR/bin/python3" "$DIR/indexer.py" "\$@"
EOF
chmod +x "$LOCAL_BIN_DIR/claude-session-index"

if [ "$(uname)" = "Darwin" ]; then
    echo "[4/5] Installing auto-indexer (launchd)..."
    mkdir -p "$(dirname "$PLIST_DST")"
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
        <string>$VENV_DIR/bin/python3</string>
        <string>$DIR/indexer.py</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>$HOME/.claude/projects</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-session-dashboard-indexer.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-session-dashboard-indexer.log</string>
</dict>
</plist>
PLIST
    launchctl load "$PLIST_DST"
else
    echo "[4/5] Skipping launchd setup on $(uname)."
fi

echo "[5/5] Done."
echo
echo "Run now:"
echo "  $DIR/run.sh"
echo
echo "If $LOCAL_BIN_DIR is on your PATH, you can also run:"
echo "  claude-session-dashboard"
echo
echo "Config:"
echo "  ~/.config/claude-session-dashboard/config.yaml"
echo
if [ ! -d "$CLAUDE_PROJECTS_DIR" ]; then
    echo "No Claude Code sessions were found yet at:"
    echo "  $CLAUDE_PROJECTS_DIR"
    echo "The dashboard will open with an empty state until that folder exists."
fi
