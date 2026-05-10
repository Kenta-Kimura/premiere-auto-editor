#!/bin/zsh
set -e

cd "$(dirname "$0")"
LOG_FILE="/private/tmp/premiere-auto-editor-server.log"
PID_FILE="/private/tmp/premiere-auto-editor-server.pid"
PORT="${PORT:-8765}"

if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if [[ "$PORT" == "8765" ]]; then
    PORT=8766
  else
    echo "Port $PORT is already in use."
    exit 1
  fi
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running: http://127.0.0.1:8765"
  exit 0
fi

/usr/bin/nohup env PORT="$PORT" /opt/homebrew/bin/python3.12 -u app.py > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 1
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Failed to start. Log: $LOG_FILE"
  exit 1
fi
echo "Started: http://127.0.0.1:$PORT"
echo "Log: $LOG_FILE"
