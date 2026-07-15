#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT_DIR"

# Namespace every checkout's mutable Docker resources using the path identity
# seen by the host Docker daemon, including from inside a dev container.
# shellcheck source=scripts/host-path-lib.sh
source "$ROOT_DIR/scripts/host-path-lib.sh"
checkout_host_path="$(lao_host_checkout_path "$ROOT_DIR")"
instance_id="${LAO_INSTANCE_ID:-$(lao_checkout_instance_id "$ROOT_DIR")}"
if [[ ! "$instance_id" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]]; then
  echo "run.sh: invalid LAO_INSTANCE_ID '$instance_id'." >&2
  exit 64
fi
export LAO_INSTANCE_ID="$instance_id"
export LAO_VERIFIED_IMAGE="linear-agent-orchestrator:verified-${instance_id}"
export LAO_PREVIOUS_IMAGE="linear-agent-orchestrator:previous-${instance_id}"
export COMPOSE_PROJECT_NAME="lao-${instance_id}"
if [[ ! "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "run.sh: invalid LAO Compose project name '$COMPOSE_PROJECT_NAME'." >&2
  exit 64
fi

usage() {
  cat >&2 <<'USAGE'
Usage: ./run.sh [--refresh-credentials] [-- <daemon args...>]

Updates the current LAO branch when it can be fast-forwarded safely, verifies
the latest stable CLI for the configured backend, and starts the daemon in the
foreground. Arguments after -- are passed to the daemon.
USAGE
}

original_args=("$@")
refresh_credentials=false
daemon_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --refresh-credentials)
      refresh_credentials=true
      shift
      ;;
    --)
      shift
      daemon_args=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "run.sh: unknown option before --: $1" >&2
      usage
      exit 64
      ;;
  esac
done

UPDATE_DIR="${LAO_UPDATE_DIR:-$ROOT_DIR/.runtime/update}"
umask 077
mkdir -p -- "$UPDATE_DIR"
chmod 700 "$UPDATE_DIR"
LOCK_FILE="$UPDATE_DIR/run.lock"

if ! command -v flock >/dev/null 2>&1; then
  echo "run.sh: flock is required to prevent concurrent launchers." >&2
  exit 69
fi

lock_ready=false
if [[ "${LAO_RUN_LOCK_HELD:-}" == "1" && -e "/proc/$$/fd/9" ]]; then
  lock_fd_path="$(readlink -f "/proc/$$/fd/9" 2>/dev/null || true)"
  lock_file_path="$(readlink -f "$LOCK_FILE" 2>/dev/null || true)"
  if [[ -n "$lock_file_path" && "$lock_fd_path" == "$lock_file_path" ]]; then
    if flock -n 9; then
      lock_ready=true
    fi
  fi
fi

if [[ "$lock_ready" != "true" ]]; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "run.sh: another LAO launcher is already updating or starting; leaving it untouched." >&2
    exit 0
  fi
fi
export LAO_RUN_LOCK_HELD=1

running_daemon_id() {
  local id command_json project working_dir working_host data_source same_checkout
  command -v docker >/dev/null 2>&1 || return 1

  # Fast path for daemons created by the current namespaced launcher.
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    command_json="$(docker inspect --format '{{json .Config.Cmd}}' "$id" 2>/dev/null || true)"
    if [[ "$command_json" == *'"daemon"'* ]]; then
      printf '%s\n' "$id"
      return 0
    fi
  done < <(docker ps --quiet \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter label=com.docker.compose.service=orchestrator 2>/dev/null || true)

  # Migration path: older/direct launchers used an unscoped Compose project.
  # Prefer the authoritative state bind mount. A /workspace working_dir belongs
  # to the container that created the label and cannot be translated using this
  # container's mount table, so it is never accepted without the data mount.
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$id" 2>/dev/null || true)"
    working_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$id" 2>/dev/null || true)"
    data_source="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/workspace/data"}}{{.Source}}{{end}}{{end}}' "$id" 2>/dev/null || true)"
    same_checkout=false
    if [[ "$project" == "$COMPOSE_PROJECT_NAME" ]]; then
      same_checkout=true
    elif [[ -n "$data_source" ]]; then
      if [[ "$data_source" == "$checkout_host_path/.runtime/data" ]]; then
        same_checkout=true
      fi
    elif [[ -n "$working_dir" &&
            "$working_dir" != "/workspace" &&
            "$working_dir" != /workspace/* ]]; then
      working_host="$(lao_host_checkout_path "$working_dir" 2>/dev/null || true)"
      if [[ "$working_dir" == "$checkout_host_path" ||
            "$working_host" == "$checkout_host_path" ]]; then
        same_checkout=true
      fi
    fi
    if [[ "$same_checkout" != "true" ]]; then
      continue
    fi
    command_json="$(docker inspect --format '{{json .Config.Cmd}}' "$id" 2>/dev/null || true)"
    if [[ "$command_json" == *'"daemon"'* ]]; then
      printf '%s\n' "$id"
      return 0
    fi
  done < <(docker ps --quiet --filter label=com.docker.compose.service=orchestrator 2>/dev/null || true)
  return 1
}

if daemon_id="$(running_daemon_id)"; then
  daemon_name="$(docker inspect --format '{{.Name}}' "$daemon_id" 2>/dev/null || true)"
  daemon_name="${daemon_name#/}"
  echo "run.sh: LAO daemon is already running${daemon_name:+ ($daemon_name)}; no update or restart performed."
  exit 0
fi

fallback_only=false
git_fallback() {
  fallback_only=true
  echo "run.sh: $1; using the last verified image." >&2
}

update_git_checkout() {
  local branch upstream remote before after

  if ! command -v git >/dev/null 2>&1 || ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_fallback "this directory is not an accessible Git checkout"
    return
  fi
  if [[ -n "$(git status --porcelain --untracked-files=normal 2>/dev/null)" ]]; then
    git_fallback "the checkout has local changes"
    return
  fi
  branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ -z "$branch" ]]; then
    git_fallback "HEAD is detached"
    return
  fi
  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
  remote="$(git config --get "branch.${branch}.remote" 2>/dev/null || true)"
  if [[ -z "$upstream" || -z "$remote" || "$remote" == "." ]]; then
    git_fallback "the current branch has no fetchable upstream"
    return
  fi

  before="$(git rev-parse HEAD)"
  if ! GIT_TERMINAL_PROMPT=0 git fetch --prune "$remote"; then
    git_fallback "fetch from '$remote' failed"
    return
  fi

  if git merge-base --is-ancestor "$before" "$upstream"; then
    if ! git merge --ff-only "$upstream"; then
      git_fallback "the upstream could not be fast-forwarded"
      return
    fi
  elif git merge-base --is-ancestor "$upstream" "$before"; then
    : # Local branch is equal to or ahead of its upstream.
  else
    git_fallback "the current branch and its upstream have diverged"
    return
  fi

  after="$(git rev-parse HEAD)"
  if [[ "$after" != "$before" && "${LAO_RUN_REEXECED:-}" != "1" ]]; then
    echo "run.sh: LAO updated to ${after:0:12}; restarting with the updated launcher."
    export LAO_RUN_REEXECED=1
    exec "$ROOT_DIR/run.sh" "${original_args[@]}"
  fi
}

if [[ "${LAO_RUN_REEXECED:-}" != "1" ]]; then
  update_git_checkout
fi

bootstrap_args=()
if [[ "$refresh_credentials" == "true" ]]; then
  bootstrap_args+=(--refresh-credentials)
fi
if ! "$ROOT_DIR/scripts/bootstrap.sh" "${bootstrap_args[@]}"; then
  echo "run.sh: bootstrap failed; daemon was not started." >&2
  exit 20
fi

# console-lib is side-effect-free and supplies the repository's safe dotenv
# reader. Never source .env: values in it must remain data, not shell code.
# shellcheck source=scripts/console-lib.sh
source "$ROOT_DIR/scripts/console-lib.sh"
backend="$(read_env_value AGENT_BACKEND "$ROOT_DIR/.env" 2>/dev/null || true)"
backend="${backend:-omx}"
case "$backend" in
  codex|omx|claude) ;;
  *)
    echo "run.sh: invalid AGENT_BACKEND '$backend' (expected codex, omx, or claude)." >&2
    exit 20
    ;;
esac
# Keep candidate selection and the eventual Compose daemon on the same backend
# even when the invoking shell exported a different AGENT_BACKEND value.
export AGENT_BACKEND="$backend"

git_sha="$(git rev-parse HEAD 2>/dev/null || printf 'unknown')"
updater_args=(--backend "$backend" --git-sha "$git_sha")
if [[ "$fallback_only" == "true" ]]; then
  updater_args+=(--fallback-only)
fi

set +e
"$ROOT_DIR/scripts/update-agent-tools.sh" "${updater_args[@]}"
update_status=$?
set -e
case "$update_status" in
  0)
    ;;
  10)
    echo "run.sh: update was unavailable; starting the last verified image." >&2
    ;;
  20)
    echo "run.sh: configuration or authentication validation failed; daemon was not started." >&2
    exit 20
    ;;
  21)
    echo "run.sh: no verified fallback image is available; daemon was not started." >&2
    exit 21
    ;;
  *)
    echo "run.sh: updater failed unexpectedly (exit $update_status); checking the last verified image once." >&2
    set +e
    "$ROOT_DIR/scripts/update-agent-tools.sh" \
      --backend "$backend" --git-sha "$git_sha" --fallback-only
    fallback_status=$?
    set -e
    case "$fallback_status" in
      0|10)
        echo "run.sh: starting the last verified image after the updater failure." >&2
        ;;
      20)
        echo "run.sh: configuration or authentication validation failed; daemon was not started." >&2
        exit 20
        ;;
      21)
        echo "run.sh: no verified fallback image is available; daemon was not started." >&2
        exit 21
        ;;
      *)
        echo "run.sh: fallback validation also failed unexpectedly (exit $fallback_status); daemon was not started." >&2
        exit "$update_status"
        ;;
    esac
    ;;
esac

# The verified tag is the only image run by the daemon. Passing it explicitly
# prevents a standalone Compose invocation from accidentally replacing it.
export LAO_IMAGE="$LAO_VERIFIED_IMAGE"
echo "run.sh: starting the verified LAO daemon (backend=$backend, image=$LAO_IMAGE)."
exec "$ROOT_DIR/scripts/daemon.sh" "${daemon_args[@]}"
