#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AGENT_HOME=.runtime/agent-home

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
copy_credential() {
  local source="$1"
  local destination="$2"
  if [[ -f "$source" && ! -e "$destination" ]]; then
    install -m 0600 "$source" "$destination"
    echo "Seeded $(basename "$destination") into $AGENT_HOME/ (dedicated copy)."
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
Edit .env and fill LINEAR_API_KEY / LINEAR_TEAM_ID before real Linear runs.
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
if ! grep -Eq '^LINEAR_PROJECT_(NAME|ID)=.+' .env; then
  ensure_env_default LINEAR_PROJECT_NAME TEST
fi
# Only issues assigned to LINEAR_ASSIGNEE_ID are polled; empty means the API
# viewer. Keep a documented placeholder so operators know the knob exists.
if ! grep -Eq '^#? ?LINEAR_ASSIGNEE_ID=' .env; then
  printf '# Only issues assigned to this user are polled; empty = API viewer.\n# LINEAR_ASSIGNEE_ID=\n' >> .env
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
# Optional per-run interactive console (tmux/PTY host). `auto` lets
# scripts/daemon.sh enable it when launched inside tmux; direct Compose/headless
# runs resolve unknown `auto` to false, so the execution path stays
# byte-identical unless the daemon exports AGENT_CONSOLE=true. The tmux socket
# always stays container-private.
ensure_env_default AGENT_CONSOLE auto
ensure_env_default AGENT_CONSOLE_DIRECT_INPUT false
ensure_env_default AGENT_CONSOLE_LOG_DIR /workspace/logs/console
ensure_env_default AGENT_CONSOLE_SOCKET_DIR /tmp/lao-console

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
