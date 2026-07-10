#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: lao-agent-runner <prompt-path> <result-path>" >&2
  exit 64
fi

prompt_path="$1"
result_path="$2"

if [[ ! -r "$prompt_path" ]]; then
  echo "lao-agent-runner: prompt is not readable: $prompt_path" >&2
  exit 66
fi

# The result path is orchestrator-owned (never issue content). Require an
# absolute path so `cd "$project_dir"` below cannot re-anchor it.
if [[ "$result_path" != /* ]]; then
  echo "lao-agent-runner: result path must be absolute: $result_path" >&2
  exit 64
fi

mkdir -p -- "$(dirname -- "$result_path")"

# Backends read this to locate the structured-result destination. The wrapper
# never writes the result itself: transport exit 0 is not semantic success.
export LAO_RESULT_PATH="$result_path"

backend="${AGENT_BACKEND:-omx}"
project_dir="${AGENT_PROJECT_DIR:-/workspace/project}"
model="${AGENT_MODEL:-}"

if [[ ! -d "$project_dir" ]]; then
  echo "lao-agent-runner: project directory does not exist: $project_dir" >&2
  exit 72
fi

cd "$project_dir"

# Docker/Compose is the security boundary; agent CLIs run with permissions
# bypassed inside the container by default. Set AGENT_BYPASS_PERMISSIONS=false
# to fall back to the backend-native sandbox/permission modes.
bypass="${AGENT_BYPASS_PERMISSIONS:-true}"

case "$backend" in
  omx|codex)
    if [[ "$backend" == "omx" ]]; then
      command=(omx exec)
    else
      command=(codex exec)
    fi
    command+=(--cd "$project_dir" --skip-git-repo-check)
    if [[ -n "$model" ]]; then
      command+=(--model "$model")
    fi
    if [[ "$bypass" == "true" ]]; then
      command+=(--dangerously-bypass-approvals-and-sandbox)
    else
      command+=(--sandbox "${AGENT_SANDBOX:-workspace-write}")
    fi
    command+=(-)
    exec "${command[@]}" < "$prompt_path"
    ;;
  claude)
    command=(claude --print)
    if [[ "$bypass" == "true" ]]; then
      command+=(--dangerously-skip-permissions)
    else
      command+=(--permission-mode "${AGENT_PERMISSION_MODE:-acceptEdits}")
    fi
    if [[ -n "$model" ]]; then
      command+=(--model "$model")
    fi
    exec "${command[@]}" < "$prompt_path"
    ;;
  *)
    echo "lao-agent-runner: unsupported AGENT_BACKEND '$backend' (expected omx, codex, or claude)" >&2
    exit 64
    ;;
esac
