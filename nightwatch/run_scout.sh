#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
dimos_root="${DIMOS_ROOT:-${project_root:h}/dimos}"
env_file="$project_root/robot.env"

if [[ -f "$env_file" ]]; then
  source "$env_file"
else
  print -u2 "No robot.env found; using variables already present in this shell."
fi

dimos_bin="${DIMOS_BIN:-}"
if [[ -z "$dimos_bin" && "${CONDA_DEFAULT_ENV:-}" == "dimos" && -n "${CONDA_PREFIX:-}" ]]; then
  dimos_bin="$CONDA_PREFIX/bin/dimos"
fi
if [[ -z "$dimos_bin" ]] && command -v conda >/dev/null 2>&1; then
  conda_base=$(conda info --base 2>/dev/null || true)
  [[ -n "$conda_base" ]] && dimos_bin="$conda_base/envs/dimos/bin/dimos"
fi
if [[ -z "$dimos_bin" || ! -x "$dimos_bin" ]]; then
  print -u2 "The conda environment 'dimos' is unavailable. Activate it or set DIMOS_BIN."
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
  if [[ "$map_port_command" == *"$project_root/scripts/fake_map_stream.py"* ]]; then
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
  ps -axo pid=,ppid=,command= | awk -v root="$project_root" -v dimos="$dimos_root" '
    $2 == 1 &&
    index($0, "multiprocessing.forkserver") &&
    index($0, root "/nightwatch") &&
    index($0, dimos) { print $1 }
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

export PYTHONPATH="$project_root/nightwatch${PYTHONPATH:+:$PYTHONPATH}"
export PATH="${dimos_bin:h}:$PATH"

# The saved premap loads by default so every session resumes the floor it
# already knows. NIGHTWATCH_PREMAP=off keeps navigation on the live map only;
# an absolute path selects another map; auto is the same as the default.
default_premap="$dimos_root/assets/output/maps/export/nightwatch_map.pc2.lcm"
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

# DimensionalOS documents <10 ms / 0% loss for the Go2 LAN. A failed ping is a
# hard error; a slow ping is a warning because radio congestion can be brief.
ping_output=$(ping -c 3 -t 3 "${DIMOS_ROBOT_IP:-192.168.12.1}" 2>&1) || {
  print -u2 "$ping_output"
  print -u2 "The Go2 is unreachable. Join its LAN before starting the stack."
  exit 1
}
print "$ping_output" | tail -2

cd "$dimos_root"
# Ctrl-C stops the stack; the trap keeps this script alive so the session's
# map still gets exported afterwards.
trap 'print ""' INT
"$dimos_bin" run nightwatch.scout || true
trap - INT

# Persist this session's geometric map. A failed export must never break the
# operator's exit path; the previous canonical premap simply stays in place.
print "Exporting this session's map to the saved premap..."
"$project_root/nightwatch/export_map.sh" --auto-promote \
  || print -u2 "Map export failed; the previous premap is kept."
