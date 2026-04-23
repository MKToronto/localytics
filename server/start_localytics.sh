#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

exec > "$REPO_DIR/debug.log" 2>&1
echo "Starting Localytics server in screen session at $(date)..."

# Override via env vars if your conda/screen paths differ.
CONDAROOT="${CONDAROOT:-$HOME/miniforge3}"
CONDA_ENV="${CONDA_ENV:-localytics}"
SCREEN_BIN="${SCREEN_BIN:-/opt/homebrew/bin/screen}"
SCREENNAME="localytics_server"
LOGFILE="$REPO_DIR/server.log"
ERROR_LOG="$REPO_DIR/server_error.log"

# Ensure environment variables are set correctly
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export TERM="xterm-256color"

# Check if `screen` is available
if ! command -v screen &> /dev/null && [ ! -x "$SCREEN_BIN" ]; then
    echo "Error: screen is not installed or not in PATH" >> "$ERROR_LOG"
    exit 1
fi


# Kill any existing screen session with the same name
if "$SCREEN_BIN" -list | grep -q "$SCREENNAME"; then
    echo "$(date) - Stopping existing screen session..." >> "$LOGFILE"
    "$SCREEN_BIN" -S "$SCREENNAME" -X quit
    sleep 1
fi
# Define the port your Localytics server runs on
SERVER_PORT=51515

# Check if the port is in use and kill the process if necessary
if lsof -i :$SERVER_PORT -t >/dev/null 2>&1; then
    echo "$(date) - Port $SERVER_PORT is already in use. Stopping existing process..." >> "$LOGFILE"
    kill -9 $(lsof -i :$SERVER_PORT -t)
    sleep 2
fi

echo "$(date) - Starting new screen session..." >> "$LOGFILE"
# Start a new detached screen session
"$SCREEN_BIN" -dmS $SCREENNAME
sleep 2  # Wait for screen to initialize
echo "$(date) - Screen session started." >> "$LOGFILE"
"$SCREEN_BIN" -S $SCREENNAME -X stuff "exec -l bash\n"
sleep 1
echo "$(date) - Bash shell started." >> "$LOGFILE"
"$SCREEN_BIN" -S $SCREENNAME -X stuff "source $CONDAROOT/etc/profile.d/conda.sh\n"
sleep 1
echo "$(date) - Conda initialized." >> "$LOGFILE"
"$SCREEN_BIN" -S $SCREENNAME -X stuff "source $CONDAROOT/etc/profile.d/mamba.sh\n"
sleep 1
echo "$(date) - Mamba initialized." >> "$LOGFILE"
# Activate conda environment
"$SCREEN_BIN" -S $SCREENNAME -X stuff "source $CONDAROOT/bin/activate $CONDA_ENV\n"
sleep 1
echo "$(date) - Conda environment activated." >> "$LOGFILE"
# Log Python path
"$SCREEN_BIN" -S $SCREENNAME -X stuff "echo 'Using Python: ' \$(which python) >> $LOGFILE\n"
sleep 1
echo "$(date) - Python path logged." >> "$LOGFILE"
# Start the Localytics server
"$SCREEN_BIN" -S $SCREENNAME -X stuff "python $SCRIPT_DIR/local_server.py & >> $LOGFILE 2>> $ERROR_LOG\n"

echo "Screen session '$SCREENNAME' started. Use 'screen -r $SCREENNAME' to check." >> "$LOGFILE"
