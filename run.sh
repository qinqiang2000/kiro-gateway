#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "$0")" && pwd)
LOG_DIR="$PROJECT_DIR/log"
PID_FILE="$LOG_DIR/gateway.pid"
LOG_FILE="$LOG_DIR/gateway.log"

# Prefer .venv if available
if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
else
    PYTHON="python"
fi

show_help() {
    cat <<EOF
Usage: ./run.sh <command> [options]

Commands:
  start    [--port PORT] [--host HOST]   Start gateway in background
  stop                                   Stop gateway
  restart  [--port PORT] [--host HOST]   Restart gateway
  status                                 Show running status
  log                                    Tail the log file (Ctrl+C to exit)
  help, -h, --help                       Show this help

Examples:
  ./run.sh start
  ./run.sh start --port 9000
  ./run.sh log
EOF
}

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

rotate_log() {
    if [ -f "$LOG_FILE" ]; then
        OLD_TIMESTAMP=$(date -r "$LOG_FILE" +%Y%m%d-%H%M%S)
        mv "$LOG_FILE" "$LOG_DIR/gateway-$OLD_TIMESTAMP.log"
        echo "Rotated old log to: gateway-$OLD_TIMESTAMP.log"
    fi
}

cmd_start() {
    if is_running; then
        echo "Kiro Gateway is already running (PID: $(cat "$PID_FILE"))"
        exit 1
    fi
    mkdir -p "$LOG_DIR"
    rotate_log
    nohup "$PYTHON" "$PROJECT_DIR/main.py" "$@" > "$LOG_FILE" 2>&1 < /dev/null &
    local pid=$!
    disown
    echo "$pid" > "$PID_FILE"
    echo "Kiro Gateway started (PID: $pid)"
    echo "Log: $LOG_FILE"
    echo ""
    echo "Viewing logs (Press Ctrl+C to stop viewing, service will continue running)..."
    echo ""
    trap 'echo ""; echo "Stopped viewing logs. Service continues running."; exit 0' INT
    tail -f "$LOG_FILE"
}

cmd_stop() {
    if ! is_running; then
        echo "Kiro Gateway is not running"
        [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
        exit 0
    fi
    local PID
    PID=$(cat "$PID_FILE")
    echo "Sending SIGTERM to process $PID..."
    kill -TERM "$PID"
    local wait_time=0
    while kill -0 "$PID" 2>/dev/null && [ $wait_time -lt 10 ]; do
        sleep 1
        wait_time=$((wait_time + 1))
        echo -n "."
    done
    echo ""
    if kill -0 "$PID" 2>/dev/null; then
        echo "Process did not stop gracefully. Sending SIGKILL..."
        kill -KILL "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "Kiro Gateway stopped"
}

cmd_status() {
    if is_running; then
        echo "Kiro Gateway is running (PID: $(cat "$PID_FILE"))"
    else
        echo "Kiro Gateway is not running"
        [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
    fi
}

COMMAND="${1:-help}"
shift || true

case "$COMMAND" in
    start)   cmd_start "$@" ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start "$@" ;;
    status)  cmd_status ;;
    log)     tail -f "$LOG_FILE" ;;
    help|-h|--help|*)  show_help ;;
esac
