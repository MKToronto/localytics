#!/bin/bash
# Launch local_server.py in a detached `screen` session via `uv run`.
# Runs cleanly from a terminal, LaunchAgent, or double-click.
# Conda users: swap the `uv run ...` line near the bottom for your own
# `conda activate <env> && python ...` invocation.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SCREEN_BIN="${SCREEN_BIN:-/opt/homebrew/bin/screen}"
SCREENNAME="localytics_server"
LOGFILE="$REPO_DIR/server.log"
ERROR_LOG="$REPO_DIR/server_error.log"
SERVER_PORT=51515

# LaunchAgents start with a minimal PATH — make uv reachable.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export TERM="xterm-256color"

exec > "$REPO_DIR/debug.log" 2>&1
echo "Starting Localytics server at $(date)..."

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv not found on PATH. Install from https://docs.astral.sh/uv/" >&2
    exit 1
fi
if ! command -v screen >/dev/null 2>&1 && [ ! -x "$SCREEN_BIN" ]; then
    echo "Error: screen not installed" >&2
    exit 1
fi

# Stop any existing screen session with the same name.
if "$SCREEN_BIN" -list 2>/dev/null | grep -q "$SCREENNAME"; then
    echo "$(date) - Stopping existing screen session..."
    "$SCREEN_BIN" -S "$SCREENNAME" -X quit
    sleep 1
fi

# Free the port if something's already bound.
if lsof -i :$SERVER_PORT -t >/dev/null 2>&1; then
    echo "$(date) - Port $SERVER_PORT in use; killing existing process..."
    kill -9 $(lsof -i :$SERVER_PORT -t)
    sleep 2
fi

echo "$(date) - Launching via uv run..."
"$SCREEN_BIN" -dmS "$SCREENNAME" bash -c \
    "cd '$REPO_DIR' && uv run '$SCRIPT_DIR/local_server.py' >> '$LOGFILE' 2>> '$ERROR_LOG'"

echo "Screen session '$SCREENNAME' started. Attach with: screen -r $SCREENNAME"
