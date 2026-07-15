#!/usr/bin/env bash

# Resolve the path identity seen by the host Docker daemon. In a dev container,
# /workspace may be a bind mount whose host source is otherwise not visible in
# the container filesystem. Keep this logic shared by run.sh, the updater, and
# the Compose wrapper so all three derive the same resource namespace.
lao_host_checkout_path() {
  local root host_workspace=""
  if ! root="$(cd -- "$1" 2>/dev/null && pwd -P)"; then
    return 1
  fi

  if [[ "$root" == "/workspace" || "$root" == /workspace/* ]]; then
    if [[ -r /proc/self/mountinfo ]]; then
      host_workspace="$(
        awk '$5 == "/workspace" {print $4}' /proc/self/mountinfo |
          tail -n 1 |
          sed 's/\\040/ /g'
      )"
    fi
    if [[ -n "$host_workspace" && "$host_workspace" != "/" ]]; then
      host_workspace="${host_workspace%/}"
      root="${host_workspace}${root#/workspace}"
    elif [[ -n "${HOST_WORKSPACE:-}" ]]; then
      host_workspace="${HOST_WORKSPACE%/}"
      root="${host_workspace}${root#/workspace}"
    fi
  fi

  printf '%s\n' "$root"
}

lao_checkout_instance_id() {
  local checkout_path checksum byte_count
  checkout_path="$(lao_host_checkout_path "$1")" || return 1
  read -r checksum byte_count _ < <(
    printf '%s' "$checkout_path" | LC_ALL=C cksum
  )
  [[ -n "$checksum" && -n "$byte_count" ]] || return 1
  printf '%s-%s\n' "$checksum" "$byte_count"
}
