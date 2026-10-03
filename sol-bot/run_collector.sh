#!/bin/bash
# Runs collect_history.py continuously in the background, restarting it
# automatically if it crashes (network blip, unhandled exception, etc.)
# rather than silently going dark for the rest of the day.
#
# Usage:
#   ./run_collector.sh start   # start in background, detached from terminal
#   ./run_collector.sh stop    # stop it
#   ./run_collector.sh status  # check if it's running

set -euo pipefail
cd "$(dirname "$0")"

PID_FILE="collector.pid"
WRAPPER_LOG="collector_wrapper.log"

start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Already running (PID $(cat "$PID_FILE"))."
        exit 0
    fi

    echo "Starting collector in background..."
    nohup bash -c '
        while true; do
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) starting collect_history.py" >> collector_wrapper.log
            python3 collect_history.py >> collector_wrapper.log 2>&1
            EXIT_CODE=$?
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) collect_history.py exited with code $EXIT_CODE, restarting in 10s" >> collector_wrapper.log
            sleep 10
        done
    ' > /dev/null 2>&1 &

    echo $! > "$PID_FILE"
    echo "Started (PID $(cat "$PID_FILE")). Logs: $WRAPPER_LOG and collector.log"
}

stop() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        PID=$(cat "$PID_FILE")
        # Kill the whole process group so the inner while-loop and
        # python process both die, not just the outer bash wrapper.
        pkill -P "$PID" 2>/dev/null || true
        kill "$PID" 2>/dev/null || true
        pkill -f "collect_history.py" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "Stopped."
    else
        echo "Not running."
        rm -f "$PID_FILE"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Running (PID $(cat "$PID_FILE"))."
        echo "Recent log:"
        tail -n 5 "$WRAPPER_LOG" 2>/dev/null || echo "(no log yet)"
    else
        echo "Not running."
    fi
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
