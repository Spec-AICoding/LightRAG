#!/usr/bin/env bash
#
# Start / stop / restart / status for the magicbox WebUI dev server
# (Vite, fixed port 5174 --strictPort so it never silently drifts).
#
# Usage:
#   magicbox_webui.sh start     # start in background (default)
#   magicbox_webui.sh stop
#   magicbox_webui.sh restart
#   magicbox_webui.sh status
#   magicbox_webui.sh --help
#
# PID file: /tmp/magicbox_webui.pid
# Log file: <repo>/deployment/script/logs/magicbox_webui.log
#
# Notes:
# - Runs `bun run dev` with the nvm Node v24.12.0 on PATH: the system
#   Node 22.2.0 has a broken ICU dylib and cannot launch Vite.
# - `bun run dev` spawns Vite as a child process, so `stop` also kills
#   whatever owns port 5174 to avoid orphans.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WEBUI_DIR="$REPO_ROOT/magicbox/webui"

PORT=5174
PID_FILE="/tmp/magicbox_webui.pid"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/magicbox_webui.log"

BUN="$HOME/.bun/bin/bun"
NVM_NODE_DIR="$HOME/.nvm/versions/node/v24.12.0/bin"

log() { echo "[magicbox-webui] $*"; }
die() { echo "[magicbox-webui] ERROR: $*" >&2; exit 1; }

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

prepare_node_path() {
  if [ -d "$NVM_NODE_DIR" ]; then
    export PATH="$NVM_NODE_DIR:$PATH"
  elif ! node -v >/dev/null 2>&1; then
    die "no usable Node found; install nvm Node v24.12.0 (system Node 22.2.0 has a broken ICU dylib)"
  fi
}

start() {
  if is_running; then
    log "already running (pid $(cat "$PID_FILE"), port $PORT)"
    return 0
  fi
  if port_in_use; then
    die "port $PORT is held by pid(s) $(lsof -ti "tcp:$PORT" | tr '\n' ' '); run 'stop' first"
  fi
  if [ ! -x "$BUN" ]; then
    die "bun not found at $BUN"
  fi

  prepare_node_path
  mkdir -p "$LOG_DIR"

  (
    cd "$WEBUI_DIR"
    nohup "$BUN" run dev --strictPort >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
  )

  # Wait for the dev server to serve index.html (first start may include
  # dependency pre-bundling, so allow up to ~30s). Use `localhost`, not
  # 127.0.0.1: Vite binds the IPv6 loopback (::1) on macOS.
  for _ in $(seq 1 60); do
    if curl -fsS "http://localhost:$PORT/" -o /dev/null >/dev/null 2>&1; then
      log "up and serving (pid $(cat "$PID_FILE"), port $PORT)"
      return 0
    fi
    sleep 0.5
  done

  log "ERROR: dev server did not become reachable within 30s; check $LOG_FILE"
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

  # Port cleanup: bun exits but the Vite child may linger
  local port_pids
  port_pids="$(lsof -ti "tcp:$PORT" 2>/dev/null || true)"
  if [ -n "$port_pids" ]; then
    # shellcheck disable=SC2086
    kill $port_pids 2>/dev/null || true
    log "sent SIGTERM to port owner(s): $port_pids"
    found=1
  fi

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
    if curl -fsS "http://localhost:$PORT/" -o /dev/null >/dev/null 2>&1; then
      log "RUNNING (pid $pid, port $PORT) - serving"
    else
      log "RUNNING (pid $pid, port $PORT) - process alive but not responding"
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
