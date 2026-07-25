#!/bin/zsh
set -euo pipefail

# Expose only the local, session-aware form API to the public form host.  The
# remote Nginx process binds this reverse forward on loopback, so no robot API
# port is opened directly to the Internet.
project_root=${0:A:h:h}
key_path="${NIGHTWATCH_FORM_SYNC_KEY:-$project_root/.secrets/tencent_form_sync_ed25519}"
known_hosts_path="${NIGHTWATCH_FORM_SYNC_KNOWN_HOSTS:-$project_root/.secrets/tencent_known_hosts}"
remote_host="${NIGHTWATCH_FORM_SYNC_HOST:-82.157.96.225}"
remote_user="${NIGHTWATCH_FORM_SYNC_USER:-ubuntu}"
remote_port="${NIGHTWATCH_FORM_SYNC_REMOTE_PORT:-18020}"
local_port="${NIGHTWATCH_FORM_SYNC_LOCAL_PORT:-8000}"
# The public Nginx also proxies the form PAGE (/form, /_next/) to loopback
# 3020. On the current deployment that port is held by a Next server running
# ON the cloud host, so the page is already served there and we forward only
# the session-aware API. Set NIGHTWATCH_FORM_SYNC_FORWARD_PAGE=1 to also serve
# the page from this machine (requires the remote page server to be stopped).
forward_page="${NIGHTWATCH_FORM_SYNC_FORWARD_PAGE:-0}"
remote_page_port="${NIGHTWATCH_FORM_PAGE_REMOTE_PORT:-3020}"
local_page_port="${NIGHTWATCH_FORM_PAGE_LOCAL_PORT:-3000}"
retry_seconds="${NIGHTWATCH_FORM_SYNC_RETRY_SECONDS:-2}"

if [[ ! -r "$key_path" ]]; then
  print -u2 "Missing form-sync SSH key: $key_path"
  exit 1
fi
if [[ ! -r "$known_hosts_path" ]]; then
  print -u2 "Missing pinned SSH host fingerprint: $known_hosts_path"
  exit 1
fi

typeset -a page_forward
page_forward=()
if [[ "$forward_page" == "1" ]]; then
  page_forward=(-R "127.0.0.1:${remote_page_port}:127.0.0.1:${local_page_port}")
fi

while true; do
  exit_code=0
  ssh \
    -N \
    -T \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=2 \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$known_hosts_path" \
    -i "$key_path" \
    -R "127.0.0.1:${remote_port}:127.0.0.1:${local_port}" \
    "${page_forward[@]}" \
    "${remote_user}@${remote_host}" || exit_code=$?
  print -u2 "Public form tunnel disconnected (exit $exit_code); retrying in ${retry_seconds}s."
  sleep "$retry_seconds"
done
