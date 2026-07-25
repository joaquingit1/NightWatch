#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
dimos_root="$project_root/dimos"
env_file="$project_root/robot.env"
venue="${NIGHTWATCH_VENUE:-legacy}"
while (( $# )); do
  case "$1" in
    --venue)
      [[ -n "${2:-}" ]] || {
        print -u2 "--venue requires a short name, for example: booth-a"
        exit 2
      }
      venue="$2"
      shift 2
      ;;
    *)
      print -u2 "Usage: $0 [--venue NAME]"
      exit 2
      ;;
  esac
done
if [[ "$venue" == *[^A-Za-z0-9_-]* ]]; then
  print -u2 "Venue names may contain only letters, numbers, _ and -."
  exit 2
fi

if [[ ! -f "$env_file" ]]; then
  print -u2 "Missing $env_file"
  exit 1
fi

if pgrep -f '[d]imos run nightwatch.scout' >/dev/null; then
  print -u2 "nightwatch.scout is already running; stop it cleanly before starting another."
  exit 1
fi

# The booth's development-only fake map streamer is sometimes left orphaned
# after an interrupted UI test. It must never silently replace the hardware
# map on :8010. Stop only this checkout's exact fake-map command; refuse to
# kill an unrelated process that happens to own the same port.
map_port_pid=$(lsof -tiTCP:8010 -sTCP:LISTEN 2>/dev/null | head -1 || true)
if [[ -n "$map_port_pid" ]]; then
  map_port_command=$(ps -p "$map_port_pid" -o command= 2>/dev/null || true)
  if [[ "$map_port_command" == *"$project_root/scripts/fake_map_stream.py"* \
        || "$map_port_command" == *"$project_root/scripts/serve_saved_map.py"* ]]; then
    print -u2 "Stopping stale fake map streamer: $map_port_pid"
    kill -TERM "$map_port_pid" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 "$map_port_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -0 "$map_port_pid" 2>/dev/null && kill -KILL "$map_port_pid" 2>/dev/null || true
  else
    print -u2 "Port 8010 is owned by another process: $map_port_command"
    print -u2 "Close it before starting the real Nightwatch map streamer."
    exit 1
  fi
fi

# A killed coordinator can leave its multiprocessing forkserver reparented to
# launchd while one or more module workers keep the shared Zenoh/LCM topics
# alive.  A later scout then receives commands and map packets from both
# generations (observed live as unsolicited stop_movement and PointCloud2
# decode errors).  With no live `dimos run nightwatch.scout` above, any
# root-owned orphan whose embedded sys.path names this checkout is stale.
stale_forkserver_text=$(
  ps -axo pid=,ppid=,command= | awk -v root="$project_root" '
    $2 == 1 &&
    index($0, "multiprocessing.forkserver") &&
    index($0, root "/nightwatch") &&
    index($0, root "/dimos") { print $1 }
  '
)
if [[ -n "$stale_forkserver_text" ]]; then
  stale_forkservers=(${(f)stale_forkserver_text})
  stale_workers=()
  for forkserver_pid in "${stale_forkservers[@]}"; do
    stale_worker_text=$(pgrep -P "$forkserver_pid" 2>/dev/null || true)
    [[ -n "$stale_worker_text" ]] && stale_workers+=(${(f)stale_worker_text})
  done
  stale_processes=("${stale_workers[@]}" "${stale_forkservers[@]}")
  print -u2 "Stopping stale Nightwatch worker generation: ${stale_processes[*]}"
  kill -TERM "${stale_processes[@]}" 2>/dev/null || true
  for _ in {1..20}; do
    remaining=()
    for stale_pid in "${stale_processes[@]}"; do
      kill -0 "$stale_pid" 2>/dev/null && remaining+=("$stale_pid")
    done
    (( ${#remaining[@]} == 0 )) && break
    sleep 0.1
  done
  (( ${#remaining[@]} )) && kill -KILL "${remaining[@]}" 2>/dev/null || true
fi

source "$env_file"
export PYTHONPATH="$project_root/nightwatch${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$dimos_root/.venv/bin:$PATH"
export NIGHTWATCH_VENUE_NAME="$venue"
# A fresh launch always begins moving in map-first Autonomous mode unless the
# operator explicitly selects another mode in the environment or workbench.
export NIGHTWATCH_OPERATING_MODE="${NIGHTWATCH_OPERATING_MODE:-autonomous}"
if [[ "$venue" == "legacy" ]]; then
  venue_map_root="$dimos_root/assets/output/maps"
  venue_memory_root="$dimos_root/assets/output/memory"
  export_dir="$venue_map_root/export"
  export NIGHTWATCH_ZONE_STATE_PATH="${NIGHTWATCH_ZONE_STATE_PATH:-$venue_map_root/nightwatch_zones.json}"
  export NIGHTWATCH_KEEP_OUT_PATH="${NIGHTWATCH_KEEP_OUT_PATH:-$venue_map_root/nightwatch_keepout.json}"
  export NIGHTWATCH_ODOM_EPOCH_PATH="${NIGHTWATCH_ODOM_EPOCH_PATH:-$venue_map_root/nightwatch_odom_epoch.json}"
  export NIGHTWATCH_RELOCALIZATION_STATE_PATH="${NIGHTWATCH_RELOCALIZATION_STATE_PATH:-$venue_map_root/nightwatch_relocalization.json}"
  export NIGHTWATCH_HOME_STATE_PATH="${NIGHTWATCH_HOME_STATE_PATH:-$venue_map_root/nightwatch_home.json}"
  export NIGHTWATCH_BREADCRUMB_STATE_PATH="${NIGHTWATCH_BREADCRUMB_STATE_PATH:-$venue_map_root/nightwatch_breadcrumbs.json}"
  export NIGHTWATCH_MAP_STATE_PATH="${NIGHTWATCH_MAP_STATE_PATH:-$venue_map_root/nightwatch_state.json}"
  export NIGHTWATCH_MAP_DB_PATH="${NIGHTWATCH_MAP_DB_PATH:-$venue_map_root/nightwatch_map.db}"
  export NIGHTWATCH_OPERATOR_SETTINGS_PATH="${NIGHTWATCH_OPERATOR_SETTINGS_PATH:-$venue_map_root/nightwatch_operator_settings.json}"
else
  venue_map_root="$dimos_root/assets/output/maps/venues/$venue"
  venue_memory_root="$dimos_root/assets/output/memory/venues/$venue"
  export_dir="$venue_map_root/export"
  mkdir -p "$venue_map_root" "$venue_memory_root" "$export_dir"
  export NIGHTWATCH_ZONE_STATE_PATH="$venue_map_root/zones.json"
  export NIGHTWATCH_KEEP_OUT_PATH="$venue_map_root/keepout.json"
  export NIGHTWATCH_ODOM_EPOCH_PATH="$venue_map_root/odom_epoch.json"
  export NIGHTWATCH_RELOCALIZATION_STATE_PATH="$venue_map_root/relocalization.json"
  export NIGHTWATCH_HOME_STATE_PATH="$venue_map_root/home.json"
  export NIGHTWATCH_BREADCRUMB_STATE_PATH="$venue_map_root/breadcrumbs.json"
  export NIGHTWATCH_MAP_STATE_PATH="$venue_map_root/state.json"
  export NIGHTWATCH_MAP_DB_PATH="$venue_map_root/nightwatch_map.db"
  export NIGHTWATCH_OPERATOR_SETTINGS_PATH="$venue_map_root/operator_settings.json"
  export NIGHTWATCH_WORLD_DB_PATH="$venue_memory_root/world.sqlite3"
  export NIGHTWATCH_EXPERIENCE_DB_PATH="$venue_memory_root/experience.db"
  export NIGHTWATCH_SPATIAL_DB_PATH="$venue_memory_root/spatial/chromadb_data"
  export NIGHTWATCH_VISUAL_MEMORY_PATH="$venue_memory_root/spatial/visual_memory.pkl"
  export NIGHTWATCH_SPATIAL_OUTPUT_DIR="$venue_memory_root/spatial/images"
fi
# The booth map and camera are the production display. Running a second Rerun
# renderer duplicates camera decode plus large-cloud conversion on the same
# Mac and was the main source of minutes-long demo feed latency. It remains
# opt-in for diagnostics: launch with DIMOS_VIEWER=rerun when explicitly needed.
export DIMOS_VIEWER="${DIMOS_VIEWER:-none}"

# The saved premap loads by default so every session resumes the floor it
# already knows. NIGHTWATCH_PREMAP=off keeps navigation on the live map only;
# an absolute path selects another map; auto is the same as the default.
default_premap="$export_dir/nightwatch_map.pc2.lcm"
if [[ "${NIGHTWATCH_PREMAP:-}" == "off" ]]; then
  unset NIGHTWATCH_PREMAP
  print "Premap disabled by request; navigating on the live map only."
elif [[ "${NIGHTWATCH_PREMAP:-}" == "auto" || -z "${NIGHTWATCH_PREMAP:-}" ]]; then
  if [[ -f "$default_premap" ]]; then
    export NIGHTWATCH_PREMAP="$default_premap"
    print "Using saved premap: $NIGHTWATCH_PREMAP"
  elif [[ "${NIGHTWATCH_PREMAP:-}" == "auto" ]]; then
    print -u2 "NIGHTWATCH_PREMAP=auto but no premap exists at $default_premap"
    exit 1
  else
    unset NIGHTWATCH_PREMAP
    print "No saved premap yet; this session maps from scratch and exports on exit."
  fi
else
  print "Using premap: $NIGHTWATCH_PREMAP"
fi

# DimensionalOS documents <10 ms / 0% loss for the Go2 LAN, but some firmware
# and access-point combinations silently drop ICMP while WebRTC is available.
# A TCP rejection is also positive proof that the host answered; use it as the
# fallback so a live robot is not incorrectly rejected during demo startup.
robot_ip="${DIMOS_ROBOT_IP:-192.168.12.1}"
if ping_output=$(ping -c 3 -t 3 "$robot_ip" 2>&1); then
  print "$ping_output" | tail -2
else
  tcp_probe=$(nc -zvw2 "$robot_ip" 8080 2>&1) || true
  if [[ "$tcp_probe" == *"Connection refused"* || "$tcp_probe" == *"succeeded"* ]]; then
    print -u2 "Go2 does not answer ICMP, but it responded to the TCP reachability probe."
  else
    print -u2 "$ping_output"
    print -u2 "$tcp_probe"
    print -u2 "The Go2 is unreachable. Join its LAN before starting the stack."
    exit 1
  fi
fi
print "Venue profile: $venue"
print "Venue data: $venue_map_root"
print "Startup mode: $NIGHTWATCH_OPERATING_MODE"

cd "$dimos_root"
# Ctrl-C stops the stack; the trap keeps this script alive so the session's
# map still gets exported afterwards.
trap 'print ""' INT
.venv/bin/dimos run nightwatch.scout || true
trap - INT

# Persist this session's geometric map. A failed export must never break the
# operator's exit path; the previous canonical premap simply stays in place.
print "Exporting this session's map to the saved premap..."
"$project_root/nightwatch/export_map.sh" --auto-promote \
  "$NIGHTWATCH_MAP_DB_PATH" "$export_dir" \
  || print -u2 "Map export failed; the previous premap is kept."
