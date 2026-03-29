#!/bin/bash
# Launch Claude Conversation Manager
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/.venv/bin/python3" "$DIR/app.py" "$@"
