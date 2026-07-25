#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
key_path="${NIGHTWATCH_FORM_SYNC_KEY:-$project_root/.secrets/tencent_form_sync_ed25519}"
known_hosts_path="${NIGHTWATCH_FORM_SYNC_KNOWN_HOSTS:-$project_root/.secrets/tencent_known_hosts}"
remote_host="${NIGHTWATCH_FORM_SYNC_HOST:-82.157.96.225}"
remote_user="${NIGHTWATCH_FORM_SYNC_USER:-ubuntu}"
remote_port="${NIGHTWATCH_FORM_SYNC_REMOTE_PORT:-18020}"
local_port="${NIGHTWATCH_FORM_SYNC_LOCAL_PORT:-8000}"
retry_seconds="${NIGHTWATCH_FORM_SYNC_RETRY_SECONDS:-2}"

if [[ ! -r "$key_path" ]]; then
  print -u2 "Missing form-sync SSH key: $key_path"
  print -u2 "Run the Tencent form-sync setup before starting the tunnel."
  exit 1
fi
if [[ ! -r "$known_hosts_path" ]]; then
  print -u2 "Missing Tencent SSH host fingerprint: $known_hosts_path"
  exit 1
fi

while true; do
  if ssh \
    -N \
    -T \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$known_hosts_path" \
    -i "$key_path" \
    -R "127.0.0.1:${remote_port}:127.0.0.1:${local_port}" \
    "${remote_user}@${remote_host}"; then
    exit_code=0
  else
    exit_code=$?
  fi
  print -u2 "Public form sync tunnel disconnected (exit $exit_code); retrying."
  sleep "$retry_seconds"
done
