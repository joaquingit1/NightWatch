#!/bin/zsh
set -euo pipefail

project_root=${0:A:h}
with_robot=0
camera_override=""
while (( $# )); do
  case "$1" in
    --with-robot)
      with_robot=1
      shift
      ;;
    --camera)
      [[ -n "${2:-}" ]] || {
        print -u2 "--camera requires robot, webcam, insta360, stub, or a device."
        exit 2
      }
      camera_override="$2"
      shift 2
      ;;
    *)
      print -u2 "Usage: $0 [--with-robot] [--camera SOURCE]"
      exit 2
      ;;
  esac
done

fatigue_python="$project_root/fatigue_fastapi_service/.venv/bin/python"
server_python="$project_root/server/.venv/bin/python"
scorer_backend="${SCORER_BACKEND:-live}"
if [[ -n "$camera_override" ]]; then
  camera_source="$camera_override"
elif [[ -n "${CAMERA_SOURCE:-}" ]]; then
  camera_source="$CAMERA_SOURCE"
elif (( with_robot )); then
  camera_source="robot"
else
  camera_source="webcam"
fi
robot_bridge_enabled="${ROBOT_BRIDGE_ENABLED:-$with_robot}"

if [[ "$scorer_backend" == "live" && ! -x "$fatigue_python" ]]; then
  print -u2 "Missing fatigue_fastapi_service/.venv."
  print -u2 "Create it with Python 3.12 and install fatigue_fastapi_service/requirements.txt."
  exit 1
fi
if [[ ! -x "$server_python" ]]; then
  print -u2 "Missing server/.venv. Create it and install server/requirements.txt first."
  exit 1
fi
if [[ ! -d "$project_root/client/node_modules" ]]; then
  print -u2 "Missing client/node_modules. Run: cd client && pnpm install"
  exit 1
fi

typeset -a child_pids

terminate_tree() {
  local pid="$1"
  local signal="$2"
  local child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    terminate_tree "$child" "$signal"
  done
  kill "-$signal" "$pid" 2>/dev/null || true
}

cleanup() {
  for pid in "${child_pids[@]}"; do
    terminate_tree "$pid" TERM
  done

  local deadline=$((SECONDS + 5))
  while (( SECONDS < deadline )); do
    local any_alive=0
    for pid in "${child_pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        any_alive=1
        break
      fi
    done
    (( any_alive )) || return
    sleep 0.1
  done

  for pid in "${child_pids[@]}"; do
    terminate_tree "$pid" KILL
  done
}
trap 'exit_code=$?; trap - EXIT INT TERM; cleanup; exit $exit_code' EXIT INT TERM

for port in 8000 3000; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    print -u2 "Port $port is already in use. Close the previous booth stack first."
    exit 1
  fi
done
if [[ "$scorer_backend" == "live" ]] && \
  lsof -nP -iTCP:8001 -sTCP:LISTEN >/dev/null 2>&1; then
  print -u2 "Port 8001 is already in use. Close the previous model service first."
  exit 1
fi

cd "$project_root"
if [[ "$scorer_backend" == "live" ]]; then
  "$fatigue_python" -m uvicorn fatigue_fastapi_service.app.main:app \
    --host 127.0.0.1 --port 8001 --workers 1 --no-access-log &
  child_pids+=($!)
fi

env \
  CAMERA_SOURCE="$camera_source" \
  SCORER_BACKEND="$scorer_backend" \
  ROBOT_BRIDGE_ENABLED="$robot_bridge_enabled" \
  "$server_python" -m uvicorn app.main:app \
  --app-dir "$project_root/server" \
  --host 127.0.0.1 --port 8000 --no-access-log &
child_pids+=($!)

(
  cd "$project_root/client"
  pnpm dev
) &
child_pids+=($!)

if (( with_robot )); then
  "$project_root/nightwatch/run_scout.sh" &
  child_pids+=($!)
fi

wait_http() {
  local name="$1"
  local url="$2"
  local deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      print "  ✓ $name ready"
      return 0
    fi
    for pid in "${child_pids[@]}"; do
      if ! kill -0 "$pid" 2>/dev/null; then
        print -u2 "$name could not start because process $pid exited."
        return 1
      fi
    done
    sleep 1
  done
  print -u2 "Timed out waiting for $name at $url"
  return 1
}

print "Night Watch integrated stack:"
print "  Camera: $camera_source"
print "  Scorer: $scorer_backend"
print "  Robot bridge: $robot_bridge_enabled"
if [[ "$scorer_backend" == "live" ]]; then
  wait_http "Fatigue model" "http://127.0.0.1:8001/health"
fi
wait_http "Policy API" "http://127.0.0.1:8000/api/score"
wait_http "Booth UI" "http://127.0.0.1:3000"

print "  Booth:  http://localhost:3000"
print "  API:    http://localhost:8000"
if [[ "$scorer_backend" == "live" ]]; then
  print "  Model:  http://localhost:8001/health"
else
  print "  Model:  stub scorer (no model process)"
fi
if (( with_robot )); then
  print "  Robot:  http://localhost:5555/operator"
else
  print "  Robot:  start separately with ./nightwatch/run_scout.sh"
fi
print "Press Ctrl-C to stop."

while true; do
  for pid in "${child_pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      print -u2 "Integrated process $pid exited; stopping the remaining stack."
      exit 1
    fi
  done
  sleep 1
done
