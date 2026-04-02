#!/bin/bash
# Launch Claude Code Session Dashboard
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$DIR/.venv/bin/python3" ]; then
    echo "No local environment found. Run ./install.sh first."
    exit 1
fi

if [ -x "$DIR/.venv/bin/claude-session-dashboard" ]; then
    exec "$DIR/.venv/bin/claude-session-dashboard" "$@"
fi

exec "$DIR/.venv/bin/python3" "$DIR/app.py" "$@"
