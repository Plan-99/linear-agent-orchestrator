#!/usr/bin/env bash
# console-attach.sh — safe host-side attach to a per-run in-container tmux session.
#
# Part of the model-neutral tmux agent UI. The orchestrator container hosts each
# backend run in a private tmux session on a container-private socket and emits
# a lifecycle JSONL under the bind-mounted console log root. This script lets an
# operator (or the console supervisor) attach a host terminal to that session by
# reaching *into* the container with the host's own docker CLI. The host tmux
# socket is never shared. Docker access, when LAO_DOCKER_HOST=true, is a
# separate full-host trust mode and not part of this console feature.
#
# Two capabilities, deliberately distinct:
#   * live view (read-only, default)  — always available; "live backend output
#     is visible" (Task 2). Uses `tmux attach -r`. NOTE: read-only is a
#     UI/client-side gate (tmux refuses keystrokes), NOT a hard security
#     boundary — anyone trusted with host `docker exec` can attach read-write
#     regardless. It exists to prevent accidental typing, not to contain a
#     hostile operator.
#   * direct input (read-write, --write) — operator can type to the backend, but
#     ONLY where the backend adapter honestly supports direct input (Task 3).
#     Gated on `supports_direct_input=true` in the run's session_started event.
#     The current omx/codex/claude one-shot modes do NOT support steering, so
#     --write is refused for them by design rather than sending stray tty noise.
#
# Safety: run ids are validated against a strict charset; socket/session paths
# are DERIVED from the validated run id (never trusted verbatim from the JSONL);
# all docker/tmux invocations are argv arrays (no eval, no shell interpolation
# of run/issue data).
set -euo pipefail

PROG="console-attach"
# shellcheck source=scripts/console-lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/console-lib.sh"

SOCKET_DIR_PREFIX="/tmp/lao-console"   # container-private, matches emitter contract
RUN_ID_RE='^[a-f0-9]{32}$'      # frozen console schema: uuid4 hex, exactly 32
CONTAINER_RE='^[A-Za-z0-9_.-]+$'
CONTAINER_DEFAULT="$(default_container_name)"

usage() {
  cat >&2 <<'EOF'
usage: console-attach.sh [options]

  --run <run_id>        Run to attach to (uuid4 hex). If omitted, lists live runs.
  --write               Request read-write (direct input) attach. Refused unless
                        the run's adapter declares supports_direct_input=true.
  --interactive         Read-write tmux client WITHOUT the direct-input gate:
                        enables scrollback (mouse/copy-mode) and selecting inner
                        panes (team-mode splits). Keystrokes typed here reach the
                        PTY but the bundled one-shot adapters never read them;
                        use --write when the backend really supports steering.
  --container <name>    Container to docker-exec into (default: $AGENT_CONSOLE_CONTAINER
                        | $LAO_DAEMON_CONTAINER_NAME | lao-daemon)
  --log-dir <dir>       Host console log root (default: $AGENT_CONSOLE_HOST_LOG_DIR
                        | <repo>/.runtime/logs/console)
  --print-cmd           Print the docker/tmux attach argv and exit (no attach).
  --list                List live runs and exit.
  -h, --help            Show this help.
EOF
}

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { log "$*"; exit 1; }

repo_root() {
  # scripts/ lives directly under the repo root.
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

console_log_dir() {
  if [[ -n "${1:-}" ]]; then printf '%s\n' "$1"; return; fi
  resolve_host_console_log_dir "$(repo_root)"
}

validate_run_id() {
  [[ "$1" =~ $RUN_ID_RE ]] || die "invalid run id (expected 32-char lowercase hex uuid4): $1"
}

# JSONL parsing helpers (jsonl_field / jsonl_terminal_state) are provided by
# console-lib.sh — structured, never substring-spoofable by issue text.

list_runs() {
  local dir="$1"
  [[ -d "$dir" ]] || { log "no console log dir: $dir"; return 0; }
  local d run f state started
  printf '%-34s %-12s %-8s %s\n' "RUN_ID" "STATE" "DIRECT" "ISSUE" >&2
  for d in "$dir"/*/; do
    [[ -d "$d" ]] || continue
    run="$(basename "$d")"
    [[ "$run" =~ $RUN_ID_RE ]] || continue
    f="$d/lifecycle.jsonl"
    [[ -r "$f" ]] || continue
    started="$(jsonl_field "$f" session_started run_id || true)"
    [[ -n "$started" ]] || continue
    state="$(jsonl_terminal_state "$f")"; [[ -n "$state" ]] || state="live"
    local direct issue
    direct="$(jsonl_field "$f" session_started supports_direct_input || echo false)"
    issue="$(jsonl_field "$f" session_started issue_identifier || echo '-')"
    printf '%-34s %-12s %-8s %s\n' "$run" "$state" "$direct" "$issue" >&2
  done
}

main() {
  local run_id="" want_write=0 want_interactive=0 container="$CONTAINER_DEFAULT" log_dir_opt="" print_cmd=0 do_list=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --run) run_id="${2:-}"; shift 2 ;;
      --write) want_write=1; shift ;;
      --interactive) want_interactive=1; shift ;;
      --container) container="${2:-}"; shift 2 ;;
      --log-dir) log_dir_opt="${2:-}"; shift 2 ;;
      --print-cmd) print_cmd=1; shift ;;
      --list) do_list=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage; die "unknown argument: $1" ;;
    esac
  done

  local dir; dir="$(console_log_dir "$log_dir_opt")"
  [[ "$container" =~ $CONTAINER_RE ]] || die "invalid container name: $container"

  if [[ "$do_list" == 1 || -z "$run_id" ]]; then
    list_runs "$dir"
    [[ "$do_list" == 1 ]] && exit 0
    [[ -z "$run_id" ]] && { log "specify --run <run_id> to attach"; exit 2; }
  fi

  validate_run_id "$run_id"
  local run_dir="$dir/$run_id"
  local jsonl="$run_dir/lifecycle.jsonl"
  [[ -r "$jsonl" ]] || die "no lifecycle log for run $run_id at $jsonl"

  # Fast-exit race: if the session already ended/killed, do not attach to a corpse.
  local state; state="$(jsonl_terminal_state "$jsonl")"
  if [[ -n "$state" ]]; then
    log "run $run_id already terminal ($state); not attaching. See $run_dir/output.log"
    exit 0
  fi

  # Derive socket + session from the validated run id (defense in depth); the
  # emitter uses the identical deterministic scheme.
  local socket="$SOCKET_DIR_PREFIX/$run_id/tmux.sock"
  local session="lao-$run_id"

  # Cross-check against the event, warn on drift (does not trust event verbatim).
  local ev_session ev_socket
  ev_session="$(jsonl_field "$jsonl" session_started session || true)"
  ev_socket="$(jsonl_field "$jsonl" session_started socket || true)"
  [[ -n "$ev_session" && "$ev_session" != "$session" ]] && log "warn: event session '$ev_session' != derived '$session' (using derived)"
  [[ -n "$ev_socket"  && "$ev_socket"  != "$socket"  ]] && log "warn: event socket '$ev_socket' != derived '$socket' (using derived)"

  # Build the tmux attach argv. Read-only unless --write (gated on the adapter's
  # direct-input capability) or --interactive (ungated read-write client for
  # scrollback/mouse/inner-pane navigation; typing is inert for one-shot
  # adapters that never read the PTY).
  local -a attach=(tmux -S "$socket" attach -t "$session")
  if [[ "$want_write" == 1 ]]; then
    local direct; direct="$(jsonl_field "$jsonl" session_started supports_direct_input || echo false)"
    if [[ "$direct" != "true" ]]; then
      local backend; backend="$(jsonl_field "$jsonl" session_started backend || echo '?')"
      die "refusing --write: backend '$backend' does not declare supports_direct_input=true for run $run_id (Task 3 safety gate). Retry read-only (omit --write) or use --interactive for scroll/mouse without steering."
    fi
    log "direct-input attach ENABLED for run $run_id (adapter supports it); keystrokes reach the backend PTY."
  elif [[ "$want_interactive" == 1 ]]; then
    log "interactive attach for run $run_id: scroll/mouse/pane-select enabled; this backend does not read typed input unless it declares supports_direct_input=true."
  else
    attach=(tmux -S "$socket" attach -r -t "$session")
  fi

  local -a cmd=(docker exec -it "$container" "${attach[@]}")
  if [[ "$print_cmd" == 1 ]]; then
    # Print a safely-quoted representation for logs / headless operators.
    printf '%q ' "${cmd[@]}"; printf '\n'
    exit 0
  fi

  command -v docker >/dev/null 2>&1 || die "docker not found on PATH (needed to exec into $container)"
  local mode=read-only
  [[ "$want_interactive" == 1 ]] && mode=interactive
  [[ "$want_write" == 1 ]] && mode=read-write
  log "attaching to run $run_id (session $session) in container $container [$mode]"
  exec "${cmd[@]}"
}

# Only run when executed directly; sourcing (tests) exposes functions without
# side effects.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
