#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=scripts/console-lib.sh
source "$ROOT_DIR/scripts/console-lib.sh"

./scripts/bootstrap.sh >/dev/null

CONTAINER_RE='^[A-Za-z0-9_.-]+$'
_container_exists() { docker inspect "$1" >/dev/null 2>&1; }

# --- Optional model-neutral tmux agent UI (host side) -----------------------
# Auto-enables when ./scripts/daemon.sh runs inside host tmux ($TMUX/$TMUX_PANE),
# unless AGENT_CONSOLE is explicitly false. Resolution: shell $AGENT_CONSOLE wins;
# otherwise the project .env value is read safely (never sourced), defaulting to
# `auto`. The resolved on/off decision is forwarded to the container via
# `-e AGENT_CONSOLE=<bool>` so host and container always agree: the emitter only
# hosts sessions when a host supervisor is actually watching. When on we also
# (1) pin a collision-safe deterministic container name so the host console
# scripts can `docker exec` into the daemon, and (2) start
# scripts/console-supervisor.sh beside this pane. The container never sees the
# host tmux/docker socket; the host reaches in with its own docker CLI.
if [[ -n "${AGENT_CONSOLE+x}" ]]; then
  agent_console="$AGENT_CONSOLE"
else
  agent_console="$(read_env_value AGENT_CONSOLE "$ROOT_DIR/.env" || true)"
  [[ -n "$agent_console" ]] || agent_console="auto"
fi
console_decision="$(console_mode "$agent_console" "${TMUX:-}" "${TMUX_PANE:-}")"

run_args=(run --rm)
if [[ "$console_decision" == "on" ]]; then
  base_container="$(default_container_name)"
  if [[ ! "$base_container" =~ $CONTAINER_RE ]]; then
    # Fail closed: disable core too so the container never emits orphan sessions.
    echo "daemon.sh: invalid container name '$base_container'; disabling console." >&2
    run_args+=(-e AGENT_CONSOLE=false)
  else
    host_log_dir="$(resolve_host_console_log_dir "$ROOT_DIR")"
    mkdir -p -- "$host_log_dir"

    # Collision-safe deterministic name: never kill/rename a running container
    # (could be the user's live daemon); pick the first free name instead.
    if command -v docker >/dev/null 2>&1; then
      container="$(next_free_container_name "$base_container" _container_exists)"
      [[ "$container" != "$base_container" ]] && \
        echo "daemon.sh: '$base_container' is taken; using '$container'." >&2
    else
      container="$base_container"
    fi
    run_args+=(-e AGENT_CONSOLE=true --name "$container")

    # Start the host supervisor beside pane 1. It self-terminates when this
    # launcher process (PID $$, preserved across the exec below) exits, so it
    # never orphans. Built as an argv array (no ${VAR:+...} word-splitting).
    if [[ -x "$ROOT_DIR/scripts/console-supervisor.sh" ]]; then
      sup_log="$host_log_dir/supervisor.log"
      sup_args=(--container "$container" --log-dir "$host_log_dir" --parent-pid "$$")
      [[ -n "${TMUX_PANE:-}" ]] && sup_args+=(--daemon-pane "$TMUX_PANE")
      "$ROOT_DIR/scripts/console-supervisor.sh" "${sup_args[@]}" \
        >>"$sup_log" 2>&1 < /dev/null &
      echo "daemon.sh: console supervisor started (container=$container, host_log_dir=$host_log_dir, out=$sup_log)" >&2
    fi
  fi
else
  # Headless / explicitly disabled: force the container off too so an inherited
  # AGENT_CONSOLE=true in .env can't spawn sessions with no supervisor watching.
  run_args+=(-e AGENT_CONSOLE=false)
fi

# exec preserves PID $$ (so the supervisor's --parent-pid watch stays valid) and
# hands terminal/signal control to the daemon exactly as before.
exec ./scripts/compose.sh "${run_args[@]}" orchestrator daemon "$@"
