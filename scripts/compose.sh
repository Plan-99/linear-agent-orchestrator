#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT_DIR"

# shellcheck source=scripts/host-path-lib.sh
source "$ROOT_DIR/scripts/host-path-lib.sh"

# Read one literal KEY=value entry without evaluating shell syntax. Callers
# provide the allowlisted key; arbitrary state-file keys are never exported.
read_update_value() {
  local wanted="$1" file="$2" line value found=""
  [[ -f "$file" ]] || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" == "$wanted="* ]] || continue
    value="${line#*=}"
    if [[ ${#value} -ge 2 && "$value" == \"*\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ ${#value} -ge 2 && "$value" == \'*\' ]]; then
      value="${value:1:${#value}-2}"
    fi
    found="$value"
  done < "$file"

  [[ -n "$found" ]] || return 1
  printf '%s' "$found"
}

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

# Exact CLI versions from the last verified build are the normal build inputs.
# Process environment values (used for candidate builds) always win. LAO_IMAGE
# is deliberately not loaded here: callers must opt into candidate/verified
# tags so a standalone `test.sh --build` cannot overwrite a last-known-good
# image. The state file is never sourced and only these known keys are read.
verified_env="$ROOT_DIR/.runtime/update/verified.env"
for key in CODEX_VERSION OMX_VERSION CLAUDE_CODE_VERSION; do
  if [[ -z "${!key+x}" ]] && value="$(read_update_value "$key" "$verified_env")"; then
    export "$key=$value"
  fi
done

# A candidate launcher may pin the revision explicitly. Otherwise label normal
# builds with the current checkout; only non-Git/failure cases fall back to the
# last verified revision and finally Dockerfile/Compose's `unknown` default.
if [[ -z "${LAO_SOURCE_REVISION+x}" ]]; then
  if revision="$(git -C "$ROOT_DIR" rev-parse --verify HEAD 2>/dev/null)" && [[ -n "$revision" ]]; then
    export LAO_SOURCE_REVISION="$revision"
  elif revision="$(read_update_value LAO_SOURCE_REVISION "$verified_env")"; then
    export LAO_SOURCE_REVISION="$revision"
  fi
fi

# Docker bind mounts and all Compose resources must use the same host checkout
# identity as run.sh/updater, even when invoked directly in a dev container.
orchestrator_dir="$(lao_host_checkout_path "$ROOT_DIR")"
instance_id="${LAO_INSTANCE_ID:-$(lao_checkout_instance_id "$ROOT_DIR")}"
if [[ ! "$instance_id" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]]; then
  echo "compose.sh: invalid LAO_INSTANCE_ID '$instance_id'." >&2
  exit 64
fi
export LAO_INSTANCE_ID="$instance_id"
export COMPOSE_PROJECT_NAME="lao-${instance_id}"

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
