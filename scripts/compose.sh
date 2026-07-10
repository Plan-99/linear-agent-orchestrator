#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Read LAO_* values from .env when they are not already in the environment, so
# shell-exported defaults below never override explicit .env configuration
# (compose gives shell env precedence over the .env file).
if [[ -f .env ]]; then
  for key in LAO_PROJECT_DIR LAO_UID LAO_GID; do
    if [[ -z "${!key:-}" ]]; then
      value="$(grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- || true)"
      value="${value%\"}" && value="${value#\"}"
      value="${value%\'}" && value="${value#\'}"
      if [[ -n "$value" ]]; then
        export "$key=$value"
      fi
    fi
  done
fi

# In the Codex/OMX dev container, Docker bind mounts are resolved by the host
# daemon. If /workspace is mounted from the host, translate this project path
# so Compose binds the real host folder. Prefer /proc/self/mountinfo because
# HOST_WORKSPACE can be stale when the container was renamed or remounted. On a
# normal host checkout, orchestrator paths stay project-relative.
orchestrator_dir="$ROOT_DIR"
if [[ "$ROOT_DIR" == /workspace/* ]]; then
  host_workspace="$(awk '$5 == "/workspace" {print $4}' /proc/self/mountinfo | tail -n 1 | sed 's/\\040/ /g')"
  if [[ -n "$host_workspace" && "$host_workspace" != "/" ]]; then
    orchestrator_dir="${host_workspace}${ROOT_DIR#/workspace}"
  elif [[ -n "${HOST_WORKSPACE:-}" ]]; then
    orchestrator_dir="${HOST_WORKSPACE}${ROOT_DIR#/workspace}"
  fi
fi

# Exactly one trusted repository is mounted at /workspace/project; it defaults
# to this checkout. Runtime state (data/work/artifacts/agent-home) always
# lives under this checkout's .runtime, even when LAO_PROJECT_DIR targets
# another repository — LAO_RUNTIME_DIR is internal plumbing for dev-container
# path translation, not a user setting. Credentials come only from the
# dedicated agent-home copies seeded by bootstrap.sh — never the live host
# CLI homes.
export LAO_PROJECT_DIR="${LAO_PROJECT_DIR:-$orchestrator_dir}"
export LAO_RUNTIME_DIR="$orchestrator_dir/.runtime"

exec docker compose "$@"
