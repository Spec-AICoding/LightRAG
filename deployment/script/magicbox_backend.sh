#!/usr/bin/env bash
#
# Start / stop / restart / status for the magicbox backend service
# (read-only FastAPI + Neo4j graph retrieval, default port 9622).
#
# Usage:
#   magicbox_backend.sh start     # start in background (default)
#   magicbox_backend.sh stop
#   magicbox_backend.sh restart
#   magicbox_backend.sh status
#   magicbox_backend.sh --help
#
# PID file: /tmp/magicbox_backend.pid
# Log file: <repo>/deployment/script/logs/magicbox_backend.log
#
# The port can be overridden with MAGICBOX_PORT (keep it consistent with
# the frontend proxy / consumers that call this service).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/magicbox/backend"

PORT="${MAGICBOX_PORT:-9622}"
PID_FILE="/tmp/magicbox_backend.pid"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/magicbox_backend.log"

log() { echo "[magicbox-backend] $*"; }
die() { echo "[magicbox-backend] ERROR: $*" >&2; exit 1; }

is_running() {
  local pid=""
  if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  fi
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

port_in_use() {
  lsof -ti "tcp:$PORT" >/dev/null 2>&1
}

start() {
  if is_running; then
    log "already running (pid $(cat "$PID_FILE"), port $PORT)"
    return 0
  fi
  if port_in_use; then
    die "port $PORT is held by pid(s) $(lsof -ti "tcp:$PORT" | tr '\n' ' '); run 'stop' first"
  fi

  mkdir -p "$LOG_DIR"

  if [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
    run_cmd=("$BACKEND_DIR/.venv/bin/python" -m uvicorn)
  elif [ -x "$HOME/.local/bin/uv" ]; then
    run_cmd=("$HOME/.local/bin/uv" run uvicorn)
  elif command -v uv >/dev/null 2>&1; then
    run_cmd=(uv run uvicorn)
  else
    die "neither $BACKEND_DIR/.venv/bin/python nor uv is available"
  fi

  (
    cd "$BACKEND_DIR"
    nohup "${run_cmd[@]}" app.main:app --port "$PORT" >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
  )

  # Wait for readiness (uvicorn + fail-fast DB check in lifespan)
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      log "up and healthy (pid $(cat "$PID_FILE"), port $PORT)"
      return 0
    fi
    sleep 0.5
  done

  log "ERROR: did not become healthy within 15s; check $LOG_FILE"
  return 1
}

stop() {
  local found=0

  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      log "sent SIGTERM to pid $pid"
      found=1
    fi
    rm -f "$PID_FILE"
  fi

  # Port cleanup: catches orphaned processes that own the port without a pidfile
  local port_pids
  port_pids="$(lsof -ti "tcp:$PORT" 2>/dev/null || true)"
  if [ -n "$port_pids" ]; then
    # shellcheck disable=SC2086
    kill $port_pids 2>/dev/null || true
    log "sent SIGTERM to port owner(s): $port_pids"
    found=1
  fi

  # Wait up to ~5s for the port to free up
  for _ in $(seq 1 10); do
    if ! port_in_use; then
      break
    fi
    sleep 0.5
  done

  local remaining
  remaining="$(lsof -ti "tcp:$PORT" 2>/dev/null || true)"
  if [ -n "$remaining" ]; then
    # shellcheck disable=SC2086
    kill -9 $remaining 2>/dev/null || true
    log "force-killed remaining pid(s): $remaining"
  fi

  if [ "$found" -eq 1 ]; then
    log "stopped"
  else
    log "was not running"
  fi
}

restart() {
  stop
  start
}

status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      log "RUNNING (pid $pid, port $PORT) - healthy"
    else
      log "RUNNING (pid $pid, port $PORT) - process alive but /health is failing"
    fi
  elif port_in_use; then
    log "RUNNING on port $PORT but not managed by this script (pid(s) $(lsof -ti "tcp:$PORT" | tr '\n' ' '))"
  else
    log "NOT RUNNING"
    return 1
  fi
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) restart ;;
  status)  status ;;
  --help|-h|help)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
