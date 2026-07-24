#!/bin/zsh
set -euo pipefail

# Export a recorded mapping database to a reusable .pc2.lcm premap.
#
# Manual usage (unchanged):
#   export_map.sh [dataset.db] [output_dir]
#
# Automated usage (called by run_scout.sh after every session):
#   export_map.sh --auto-promote [dataset.db] [output_dir]
#
# --auto-promote exports the session db into a staging directory, keeps a
# timestamped copy, and promotes it to the canonical premap only when it
# looks at least as complete as the current one. Short or aborted sessions
# never destroy a good premap.

project_root=${0:A:h:h}
dimos_root="${DIMOS_ROOT:-${project_root:h}/dimos}"

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
dimos_python="${dimos_bin:h}/python"

auto_promote=0
if [[ "${1:-}" == "--auto-promote" ]]; then
  auto_promote=1
  shift
fi

dataset=${1:-"$dimos_root/assets/output/maps/nightwatch_map.db"}
output_dir=${2:-"$dimos_root/assets/output/maps/export"}

# Below this size the recording is a boot fragment (crashed or seconds-long
# session). Real mapping sessions record 15 MB+ at 1 Hz lidar.
min_db_bytes=$((10 * 1024 * 1024))
# A fresh export replaces the canonical premap only when its XY bounding
# footprint is at least this fraction of the canonical map. File size was a
# bad proxy: the clean, successful 28 x 30 m floor map is 1.4 MB, while an old
# over-sampled 13 x 46 m corridor loop is 4 MB.
promote_ratio_pct=80
keep_exports=5

if [[ ! -f "$dataset" ]]; then
  print -u2 "No mapping database at $dataset"
  exit 1
fi

base=${dataset:t:r}

if (( ! auto_promote )); then
  mkdir -p "$output_dir"
  cd "$output_dir"
  exec "$dimos_bin" map global "$dataset" \
    --voxel 0.05 \
    --device CPU:0 \
    --pgo \
    --denoise \
    --replace-window \
    --export \
    --no-gui \
    --out "$output_dir/$base.rrd"
fi

db_bytes=$(stat -f %z "$dataset")
if (( db_bytes < min_db_bytes )); then
  print "Session db is only $((db_bytes / 1024)) KB; skipping export (needs $((min_db_bytes / 1024 / 1024)) MB)."
  exit 0
fi

# The exporter writes <dataset stem>.pc2.lcm into the working directory. That
# stem is usually nightwatch_map, the canonical name, so stage the export in
# a scratch directory to keep the canonical premap untouched until promotion.
staging="$output_dir/.staging"
rm -rf "$staging"
mkdir -p "$staging"
cd "$staging"
"$dimos_bin" map global "$dataset" \
  --voxel 0.05 \
  --device CPU:0 \
  --pgo \
  --denoise \
  --replace-window \
  --export \
  --no-gui \
  --out "$staging/$base.rrd"

exported="$staging/$base.pc2.lcm"
if [[ ! -f "$exported" ]]; then
  print -u2 "Export finished but $exported was not produced."
  exit 1
fi

canonical="$output_dir/nightwatch_map.pc2.lcm"
stamp=$(date +%Y%m%d%H%M%S)
stamped="$output_dir/nightwatch_map.$stamp.pc2.lcm"
cp "$exported" "$stamped"

new_bytes=$(stat -f %z "$stamped")
if [[ -f "$canonical" ]]; then
  map_xy_area_milli() {
    "$dimos_python" - "$1" <<'PY'
import sys
import numpy as np
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2

cloud = PointCloud2.lcm_decode(open(sys.argv[1], "rb").read()).pointcloud
points = np.asarray(cloud.points)
width = float(points[:, 0].max() - points[:, 0].min())
height = float(points[:, 1].max() - points[:, 1].min())
print(max(1, round(width * height * 1000.0)))
PY
  }
  new_area_milli=$(map_xy_area_milli "$stamped") || {
    print -u2 "Could not measure new map coverage; keeping the canonical premap."
    rm -rf "$staging"
    exit 0
  }
  old_area_milli=$(map_xy_area_milli "$canonical") || {
    print -u2 "Could not measure canonical map coverage; keeping it."
    rm -rf "$staging"
    exit 0
  }
  if (( new_area_milli * 100 < old_area_milli * promote_ratio_pct )); then
    print "New export covers $((new_area_milli / 1000)) m2 versus canonical $((old_area_milli / 1000)) m2; keeping the broader canonical map."
    print "Timestamped copy saved: $stamped"
    rm -rf "$staging"
    exit 0
  fi
fi
cp "$stamped" "$canonical"
print "Promoted premap: $canonical ($((new_bytes / 1024)) KB)"
rm -rf "$staging"

# Prune old timestamped exports, newest first.
typeset -a old_exports
old_exports=($output_dir/nightwatch_map.[0-9]*.pc2.lcm(N.om))
if (( ${#old_exports} > keep_exports )); then
  for stale in "${old_exports[@]:$keep_exports}"; do
    rm -f "$stale"
    print "Pruned old export: ${stale:t}"
  done
fi
