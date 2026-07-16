#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AGENT_HOME=.runtime/agent-home

# --refresh-credentials: force re-copy of every existing host credential
# original regardless of mtime. Escape hatch for the GitHub #3 case where the
# stale copy carries the newer mtime (see copy_credential below).
REFRESH_CREDENTIALS=false
for arg in "$@"; do
  case "$arg" in
    --refresh-credentials)
      REFRESH_CREDENTIALS=true
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--refresh-credentials]" >&2
      exit 1
      ;;
  esac
done

mkdir -p .runtime/data .runtime/work .runtime/artifacts .runtime/logs/console \
  "$AGENT_HOME/.codex" "$AGENT_HOME/.claude"
touch .runtime/data/.gitkeep .runtime/work/.gitkeep .runtime/artifacts/.gitkeep .runtime/logs/.gitkeep

# The container runs as the host user (LAO_UID/LAO_GID). Prefer the owner of
# this checkout: inside a dev container `id -u` can be 0 while the bind-mounted
# files still carry the real host owner. Never persist root as the runtime id.
detect_host_id() {
  local kind="$1" value
  case "$kind" in
    uid) value="$(stat -c %u "$ROOT_DIR")" ;;
    gid) value="$(stat -c %g "$ROOT_DIR")" ;;
  esac
  if [[ "$value" == "0" ]]; then
    case "$kind" in
      uid) value="$(id -u)" ;;
      gid) value="$(id -g)" ;;
    esac
  fi
  if [[ "$value" == "0" ]]; then
    value=1000
  fi
  printf '%s' "$value"
}

HOST_UID="$(detect_host_id uid)"
HOST_GID="$(detect_host_id gid)"

# Seed the dedicated agent home with credential copies. Compose mounts only
# this directory at /home/agent — never the live host CLI homes.
#
# A copy is refreshed whenever the host original is strictly newer (mtime), so
# a host-side token rotation self-heals on the next bootstrap/daemon start
# (GitHub #3). Known limitation of the mtime heuristic: if the container CLI
# rewrites the copy *after* the host rotation (e.g. the 16:40 case in #3 — a
# failed refresh with the already-rotated token), the stale copy carries the
# newer mtime and is kept; --refresh-credentials is the escape hatch there.
copy_credential() {
  local source="$1"
  local destination="$2"
  if [[ ! -f "$source" ]]; then
    return 0
  fi
  if [[ ! -e "$destination" ]]; then
    install -m 0600 "$source" "$destination"
    echo "Seeded $(basename "$destination") into $AGENT_HOME/ (dedicated copy)."
  elif [[ "$REFRESH_CREDENTIALS" == "true" ]]; then
    install -m 0600 "$source" "$destination"
    echo "Refreshed $(basename "$destination") from host (--refresh-credentials)."
  elif [[ "$source" -nt "$destination" ]]; then
    install -m 0600 "$source" "$destination"
    echo "Refreshed $(basename "$destination") from newer host original."
  fi
}

# Migrate the legacy non-hidden layout (agent-home/codex, agent-home/claude).
for legacy in codex claude; do
  if [[ -d "$AGENT_HOME/$legacy" ]]; then
    cp -an "$AGENT_HOME/$legacy/." "$AGENT_HOME/.$legacy/" 2>/dev/null || true
  fi
done

codex_source="${CODEX_HOME:-$HOME/.codex}"
copy_credential "$codex_source/auth.json" "$AGENT_HOME/.codex/auth.json"
copy_credential "$codex_source/config.toml" "$AGENT_HOME/.codex/config.toml"
copy_credential "$HOME/.claude/.credentials.json" "$AGENT_HOME/.claude/.credentials.json"
copy_credential "$HOME/.claude.json" "$AGENT_HOME/.claude.json"

# Work/data/artifact directories are non-secret local artifacts. The agent
# home contains credential copies and stays owner-only. Tolerate leftovers
# owned by other users (e.g. files written by the old root container).
if ! chmod -R a+rwX .runtime/data .runtime/work .runtime/artifacts .runtime/logs 2>/dev/null; then
  echo "WARNING: some .runtime files are owned by another user (likely written" >&2
  echo "by the previous root container) and were left untouched." >&2
fi
chmod 700 "$AGENT_HOME" "$AGENT_HOME/.codex" "$AGENT_HOME/.claude"

# When bootstrap runs as root (dev container), hand the runtime tree to the
# host user the container will run as.
if [[ "$(id -u)" == "0" ]]; then
  chown -R "$HOST_UID:$HOST_GID" .runtime || true
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  cat <<'MSG'
Created project-local .env from .env.example.
Edit .env, choose LINEAR_AUTH_MODE, and fill its matching credentials plus
LINEAR_TEAM_ID and a project target before real Linear runs.
MSG
else
  chmod 600 .env
fi

ensure_env_default() {
  local key="$1"
  local value="$2"
  if ! grep -Eq "^${key}=.+" .env; then
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

ensure_env_default APP_ENV local
ensure_env_default LOG_LEVEL INFO
ensure_env_default LOG_FORMAT json
ensure_env_default STATE_DB_PATH /workspace/data/state.sqlite3
ensure_env_default LAO_UID "$HOST_UID"
ensure_env_default LAO_GID "$HOST_GID"
ensure_env_default LINEAR_AUTH_MODE api_key
if ! grep -Eq '^LINEAR_PROJECT_(NAME|ID)=.+' .env; then
  ensure_env_default LINEAR_PROJECT_NAME TEST
fi
# Migrate the API-key-only wording written by pre-OAuth bootstrap versions.
if grep -Fqx '# Only issues assigned to this user are polled; empty = API viewer.' .env; then
  sed -i 's|^# Only issues assigned to this user are polled; empty = API viewer\.$|# Assignee filter: OAuth mode requires a human user ID; API-key mode may use its viewer.|' .env
fi
# Keep a documented placeholder so operators know OAuth mode requires an
# explicit human assignee while API-key mode may use the credential viewer.
if ! grep -Eq '^#? ?LINEAR_ASSIGNEE_ID=' .env; then
  printf '# Assignee filter: OAuth mode requires a human user ID; API-key mode may use its viewer.\n# LINEAR_ASSIGNEE_ID=\n' >> .env
fi
ensure_env_default LINEAR_QUEUE_STATE_NAME Todo
ensure_env_default LINEAR_IN_PROGRESS_STATE_NAME "'In Progress'"
ensure_env_default LINEAR_DONE_STATE_NAME Done

# Migrate stale defaults written by earlier bootstrap versions.
if grep -Fqx "RUNNER_COMMAND_TEMPLATE='omx run --model gpt-5.5 --prompt-file {prompt_path}'" .env; then
  sed -i "s|^RUNNER_COMMAND_TEMPLATE=.*$|RUNNER_COMMAND_TEMPLATE='/usr/local/bin/lao-agent-runner {prompt_path} {result_path}'|" .env
fi
if grep -Fqx "RUNNER_COMMAND_TEMPLATE='/usr/local/bin/lao-agent-runner {prompt_path}'" .env; then
  sed -i "s|^RUNNER_COMMAND_TEMPLATE=.*$|RUNNER_COMMAND_TEMPLATE='/usr/local/bin/lao-agent-runner {prompt_path} {result_path}'|" .env
fi
ensure_env_default RUNNER_COMMAND_TEMPLATE "'/usr/local/bin/lao-agent-runner {prompt_path} {result_path}'"
ensure_env_default RUNNER_WORK_DIR /workspace/work
ensure_env_default RUNNER_ARTIFACTS_DIR /workspace/artifacts
if grep -Fqx 'RUNNER_ALLOWED_ENV=PATH,HOME' .env; then
  sed -i 's|^RUNNER_ALLOWED_ENV=PATH,HOME$|RUNNER_ALLOWED_ENV=PATH,HOME,CODEX_HOME,AGENT_BACKEND,AGENT_MODEL,AGENT_PROJECT_DIR,AGENT_SANDBOX,AGENT_PERMISSION_MODE,AGENT_BYPASS_PERMISSIONS|' .env
fi
ensure_env_default RUNNER_ALLOWED_ENV PATH,HOME,CODEX_HOME,AGENT_BACKEND,AGENT_MODEL,AGENT_PROJECT_DIR,AGENT_SANDBOX,AGENT_PERMISSION_MODE,AGENT_BYPASS_PERMISSIONS
ensure_env_default AGENT_BACKEND omx
ensure_env_default AGENT_MODEL gpt-5.5
ensure_env_default AGENT_PROJECT_DIR /workspace/project
ensure_env_default AGENT_SANDBOX workspace-write
ensure_env_default AGENT_PERMISSION_MODE acceptEdits
# Docker is the boundary: bypass inside the container is the supported default.
if grep -Fqx 'AGENT_BYPASS_PERMISSIONS=false' .env; then
  sed -i 's|^AGENT_BYPASS_PERMISSIONS=false$|AGENT_BYPASS_PERMISSIONS=true|' .env
fi
ensure_env_default AGENT_BYPASS_PERMISSIONS true
# Per-run interactive console (tmux/PTY host). Foreground launchers enter a
# checkout-specific host tmux session automatically; redirected/service runs do
# not allocate a terminal and can still be attached manually. The tmux socket
# always stays container-private.
ensure_env_default AGENT_CONSOLE true
ensure_env_default AGENT_CONSOLE_AUTO_TMUX true
ensure_env_default AGENT_CONSOLE_DIRECT_INPUT false
ensure_env_default AGENT_CONSOLE_LOG_DIR /workspace/logs/console
ensure_env_default AGENT_CONSOLE_SOCKET_DIR /tmp/lao-console
ensure_env_default AGENT_INTERACTIVE true
ensure_env_default AGENT_INTERACTIVE_AUTOCLOSE false
ensure_env_default MAX_PARALLEL_RUNS 1

cat <<'MSG'
Project-local runtime is ready:
  .env
  .runtime/data
  .runtime/work
  .runtime/artifacts
  .runtime/agent-home   (dedicated credential copies; not the live CLI homes)

Next:
  scripts/compose.sh run --rm orchestrator startup-check --allow-missing-secrets
  # After filling .env with real Linear credentials:
  scripts/compose.sh run --rm orchestrator startup-check
  scripts/compose.sh run --rm orchestrator daemon
MSG
