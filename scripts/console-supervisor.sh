#!/usr/bin/env bash
# console-supervisor.sh — host-side sibling-pane supervisor for the tmux agent UI.
#
# Runs on the HOST beside the daemon pane (host tmux pane 1). It watches the
# bind-mounted host console log root (<runtime>/logs/console/<run_id>/lifecycle.jsonl)
# emitted by the container. For each live run it opens exactly one sibling host
# tmux pane that attaches to the run's in-container tmux session ("live backend
# output is visible", Task 2). When the run ends, the inner session dies and the
# attach client — hence the pane — exits on its own.
#
# Attach mode per run (Task 3): read-write when the run's session_started event
# declares supports_direct_input=true (operator can type to the backend);
# read-only otherwise (safe default). NOTE: read-only is a UI/client-side gate
# (tmux `attach -r`), not a hard security boundary — a trusted user with host
# docker access can always exec in read-write. console-attach.sh re-verifies the
# capability gate before ever passing --write.
#
# It never mounts or touches the host tmux socket into the container and only
# reaches in via `docker exec` (inside console-attach.sh / has-session probe).
# Trust flows host -> container only.
#
# Headless / non-tmux hosts (CI, SSH without $TMUX): degrades to logging the
# exact manual `console-attach.sh` command once per run start. The container
# behaves identically in both modes; only this host-side consumer differs.
#
# Stale/orphan safety: .runtime console logs persist across restarts, so a crash
# can leave a session_started with no terminal event. Before splitting we verify
# the in-container session actually exists (`tmux has-session`); an absent
# session is recorded as an orphan and skipped, never repeatedly re-split.
# Opened runs are tracked in memory so an operator-closed pane is not respawned.
#
# Lifecycle: with --parent-pid <pid> the supervisor self-terminates when that
# launcher process exits, so it never orphans after the daemon stops.
set -euo pipefail

PROG="console-supervisor"
# shellcheck source=scripts/console-lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/console-lib.sh"

RUN_ID_RE='^[a-f0-9]{32}$'      # frozen console schema: uuid4 hex, exactly 32
CONTAINER_RE='^[A-Za-z0-9_.-]+$'
SOCKET_DIR_PREFIX="/tmp/lao-console"

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { log "$*"; exit 1; }

repo_root() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }

ATTACH_SCRIPT=""   # resolved in main
STOP=0
trap 'STOP=1' INT TERM

usage() {
  cat >&2 <<'EOF'
usage: console-supervisor.sh [options]

  --log-dir <dir>     Host console log root (default: resolve_host_console_log_dir:
                      $AGENT_CONSOLE_HOST_LOG_DIR | <LAO_RUNTIME_DIR>/logs/console |
                      <repo>/.runtime/logs/console)
  --container <name>  Container the panes attach into (default: $AGENT_CONSOLE_CONTAINER
                      | $LAO_DAEMON_CONTAINER_NAME | lao-daemon)
  --daemon-pane <id>  tmux pane id to split beside (default: $TMUX_PANE)
  --parent-pid <pid>  Exit when this launcher pid is gone (no-orphan guarantee).
  --open-target <t>   Where each run opens: "window" (default; daemon keeps
                      window 0, each agent gets its own window so team-mode
                      backends can split panes inside it) or "split" (sibling
                      pane in the daemon's window).
                      Default: $AGENT_CONSOLE_OPEN_TARGET | .env | window.
  --focus <bool>      Switch the client to the newly opened run window/pane
                      (true/false). Default: $AGENT_CONSOLE_FOCUS | .env | true.
  --layout <name>     Split mode only: window layout applied after opening a
                      run pane so the daemon (first pane) stays the main pane.
                      One of: main-vertical | main-horizontal | tiled |
                      even-vertical | even-horizontal | none.
                      Default: $AGENT_CONSOLE_LAYOUT | .env | main-vertical.
  --interval <sec>    Poll interval, integer >= 1 (default: 2)
  --once              Run a single reconcile pass and exit (for tests/cron).
  --headless          Force headless mode (log attach commands, never split panes).
  --dry-run           Print intended actions; never run tmux/docker.
  -h, --help          Show this help.
EOF
}

run_direct_input() { # file -> "true"|"false"
  local v; v="$(jsonl_field "$1" session_started supports_direct_input || echo false)"
  [[ "$v" == "true" ]] && echo true || echo false
}

# --- tmux / docker helpers (argv arrays; run id is schema-validated) ---

existing_pane_for() { # run_id -> pane id if a pane titled lao:<run_id> exists
  local run="$1" pid title
  while IFS=' ' read -r pid title; do
    [[ "$title" == "lao:$run" ]] && { printf '%s\n' "$pid"; return 0; }
  done < <(tmux list-panes -a -F '#{pane_id} #{pane_title}' 2>/dev/null || true)
  return 1
}

session_alive() { # run_id -> 0 if the in-container private tmux session exists
  [[ "$DRY_RUN" == 1 ]] && return 0
  command -v docker >/dev/null 2>&1 || return 0   # cannot verify; let attach report
  local run="$1"
  docker exec "$CONTAINER" tmux -S "$SOCKET_DIR_PREFIX/$run/tmux.sock" \
    has-session -t "lao-$run" >/dev/null 2>&1
}

open_pane() { # run_id
  local run="$1"
  local direct; direct="$(run_direct_input "$LOG_DIR/$run/lifecycle.jsonl")"
  local -a attach_cmd=("$ATTACH_SCRIPT" --run "$run" --container "$CONTAINER" --log-dir "$LOG_DIR")
  # Direct-input adapters get a gated read-write attach; everything else gets an
  # interactive client so scrollback, mouse, and inner team-pane selection work.
  local mode="interactive"
  if [[ "$direct" == "true" ]]; then attach_cmd+=(--write); mode="read-write (direct input)"
  else attach_cmd+=(--interactive); fi
  local shcmd; shcmd="$(printf '%q ' "${attach_cmd[@]}")"
  local focus_suffix=""
  [[ "$FOCUS" == "true" ]] && focus_suffix=" ; focus"
  if [[ "$DRY_RUN" == 1 ]]; then
    if [[ "$OPEN_TARGET" == "window" ]]; then
      printf 'DRY: [%s] new-window -a -t %s sh -c %q ; select-pane -T lao:%s%s\n' \
        "$mode" "${DAEMON_PANE:-<pane>}" "$shcmd" "$run" "$focus_suffix"
    else
      printf 'DRY: [%s] split-window -t %s sh -c %q ; select-pane -T lao:%s ; select-layout %s%s\n' \
        "$mode" "${DAEMON_PANE:-<pane>}" "$shcmd" "$run" "$LAYOUT" "$focus_suffix"
    fi
    return 0
  fi
  local -a open_cmd
  if [[ "$OPEN_TARGET" == "window" ]]; then
    # One window per run: the daemon keeps window 0 and each agent gets its own
    # window, so a team-mode backend can split panes inside it freely.
    # new-window's -t takes a WINDOW target ("can't specify pane here"), so the
    # daemon pane id must be resolved to its session:window first.
    open_cmd=(tmux new-window -d -PF '#{pane_id}')
    if [[ -n "$DAEMON_PANE" ]]; then
      local daemon_window
      daemon_window="$(tmux display-message -p -t "$DAEMON_PANE" '#{session_name}:#{window_index}' 2>/dev/null || true)"
      [[ -n "$daemon_window" ]] && open_cmd+=(-a -t "$daemon_window")
    fi
  else
    open_cmd=(tmux split-window -d -PF '#{pane_id}')
    [[ -n "$DAEMON_PANE" ]] && open_cmd+=(-t "$DAEMON_PANE")
  fi
  open_cmd+=("$shcmd")
  local pane_id
  if ! pane_id="$("${open_cmd[@]}" 2>&1)"; then
    log "failed to open $OPEN_TARGET for run $run: $pane_id"
    return 1
  fi
  tmux select-pane -t "$pane_id" -T "lao:$run" 2>/dev/null || true
  if [[ "$OPEN_TARGET" == "window" ]]; then
    tmux rename-window -t "$pane_id" "lao:$run" 2>/dev/null || true
    # Jump the operator to the freshest run (AGENT_CONSOLE_FOCUS=false to stay put).
    if [[ "$FOCUS" == "true" ]]; then
      tmux select-window -t "$pane_id" 2>/dev/null || true
    fi
    log "opened window for run $run [$mode] (pane $pane_id, focus=$FOCUS)"
    return 0
  fi
  # Split mode: re-apply the window layout so the daemon (first pane) remains
  # the main pane and issue panes stack beside it as more runs open.
  if [[ "$LAYOUT" != "none" ]]; then
    tmux select-layout -t "$pane_id" "$LAYOUT" 2>/dev/null || true
  fi
  if [[ "$FOCUS" == "true" ]]; then
    tmux select-pane -t "$pane_id" 2>/dev/null || true
  fi
  log "opened pane $pane_id for run $run [$mode] (layout=$LAYOUT, focus=$FOCUS)"
}

headless_notice() { # run_id
  local run="$1"
  case " $HEADLESS_SEEN " in *" $run "*) return 0 ;; esac
  HEADLESS_SEEN="$HEADLESS_SEEN $run"
  local direct; direct="$(run_direct_input "$LOG_DIR/$run/lifecycle.jsonl")"
  local -a extra=(--interactive); local mode="interactive"
  if [[ "$direct" == "true" ]]; then extra=(--write); mode="read-write (direct input)"; fi
  log "run $run live (headless) [$mode]. Attach with:"
  printf '  %s --run %s --container %s --log-dir %q' \
    "$ATTACH_SCRIPT" "$run" "$CONTAINER" "$LOG_DIR" >&2
  ((${#extra[@]})) && printf ' %s' "${extra[@]}" >&2
  printf '\n' >&2
}

check_container() { # 0 if container present or unknowable
  [[ "$DRY_RUN" == 1 ]] && return 0
  command -v docker >/dev/null 2>&1 || return 0
  docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1
}

seen_in() { case " $2 " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

reconcile_once() {
  [[ -d "$LOG_DIR" ]] || { log "console log dir absent yet: $LOG_DIR"; return 0; }
  local container_ok=1
  if [[ "$MODE" == pane ]] && ! check_container; then
    container_ok=0
    log "container '$CONTAINER' not running; deferring pane opens"
  fi
  local d run f
  for d in "$LOG_DIR"/*/; do
    [[ -d "$d" ]] || continue
    run="$(basename "$d")"
    [[ "$run" =~ $RUN_ID_RE ]] || continue     # ignore non-schema dirs entirely
    f="$d/lifecycle.jsonl"
    [[ -r "$f" ]] || continue
    jsonl_has_event "$f" session_started || continue
    [[ -n "$(jsonl_terminal_state "$f")" ]] && continue   # session over; pane self-closes
    if [[ "$MODE" == headless ]]; then headless_notice "$run"; continue; fi
    [[ "$container_ok" == 1 ]] || continue
    # In-memory dedupe: never respawn an operator-closed pane; never re-log an orphan.
    seen_in "$run" "$OPENED_SEEN" && continue
    seen_in "$run" "$ORPHAN_SEEN" && continue
    # Stale-start guard: a persisted session_started with no live in-container
    # session (e.g. after a crash) is an orphan — record and skip, don't re-split.
    if ! session_alive "$run"; then
      ORPHAN_SEEN="$ORPHAN_SEEN $run"
      log "orphan run $run (session_started with no live in-container session); skipping"
      continue
    fi
    # Adopt an already-open pane after a supervisor restart (dedupe by title).
    if [[ "$DRY_RUN" != 1 ]] && existing_pane_for "$run" >/dev/null; then
      OPENED_SEEN="$OPENED_SEEN $run"; continue
    fi
    if open_pane "$run"; then OPENED_SEEN="$OPENED_SEEN $run"; fi
  done
}

parent_gone() { [[ -n "$PARENT_PID" ]] && ! kill -0 "$PARENT_PID" 2>/dev/null; }

main() {
  local log_dir_opt="" interval=2 once=0 force_headless=0
  DRY_RUN=0
  CONTAINER="$(default_container_name)"
  DAEMON_PANE="${TMUX_PANE:-}"
  PARENT_PID=""
  OPENED_SEEN=""
  ORPHAN_SEEN=""
  LAYOUT=""
  OPEN_TARGET=""
  FOCUS=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --log-dir) log_dir_opt="${2:-}"; shift 2 ;;
      --container) CONTAINER="${2:-}"; shift 2 ;;
      --daemon-pane) DAEMON_PANE="${2:-}"; shift 2 ;;
      --parent-pid) PARENT_PID="${2:-}"; shift 2 ;;
      --open-target) OPEN_TARGET="${2:-}"; shift 2 ;;
      --focus) FOCUS="${2:-}"; shift 2 ;;
      --layout) LAYOUT="${2:-}"; shift 2 ;;
      --interval) interval="${2:-}"; shift 2 ;;
      --once) once=1; shift ;;
      --headless) force_headless=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage; die "unknown argument: $1" ;;
    esac
  done

  [[ "$CONTAINER" =~ $CONTAINER_RE ]] || die "invalid container name: $CONTAINER"
  [[ "$interval" =~ ^[1-9][0-9]*$ ]] || die "invalid --interval (integer >= 1): $interval"
  [[ -z "$PARENT_PID" || "$PARENT_PID" =~ ^[0-9]+$ ]] || die "invalid parent pid: $PARENT_PID"
  # Open target: flag > process env > project .env > window (daemon keeps
  # window 0; each run gets its own window for team-mode pane splits).
  if [[ -z "$OPEN_TARGET" ]]; then
    OPEN_TARGET="${AGENT_CONSOLE_OPEN_TARGET:-}"
    [[ -n "$OPEN_TARGET" ]] || OPEN_TARGET="$(read_env_value AGENT_CONSOLE_OPEN_TARGET "$(repo_root)/.env" 2>/dev/null || true)"
    [[ -n "$OPEN_TARGET" ]] || OPEN_TARGET="window"
  fi
  [[ "$OPEN_TARGET" =~ ^(window|split)$ ]] || die "invalid --open-target: $OPEN_TARGET (expected window|split)"
  # Focus: flag > process env > project .env > true (jump to the newest run).
  if [[ -z "$FOCUS" ]]; then
    FOCUS="${AGENT_CONSOLE_FOCUS:-}"
    [[ -n "$FOCUS" ]] || FOCUS="$(read_env_value AGENT_CONSOLE_FOCUS "$(repo_root)/.env" 2>/dev/null || true)"
    [[ -n "$FOCUS" ]] || FOCUS="true"
  fi
  case "${FOCUS,,}" in
    1|true|yes|on) FOCUS="true" ;;
    0|false|no|off) FOCUS="false" ;;
    *) die "invalid --focus: $FOCUS (expected true|false)" ;;
  esac
  # Layout (split mode): flag > process env > project .env > main-vertical
  # (daemon stays the large main pane; issue panes stack beside it).
  if [[ -z "$LAYOUT" ]]; then
    LAYOUT="${AGENT_CONSOLE_LAYOUT:-}"
    [[ -n "$LAYOUT" ]] || LAYOUT="$(read_env_value AGENT_CONSOLE_LAYOUT "$(repo_root)/.env" 2>/dev/null || true)"
    [[ -n "$LAYOUT" ]] || LAYOUT="main-vertical"
  fi
  [[ "$LAYOUT" =~ ^(main-vertical|main-horizontal|tiled|even-vertical|even-horizontal|none)$ ]] \
    || die "invalid --layout: $LAYOUT (expected main-vertical|main-horizontal|tiled|even-vertical|even-horizontal|none)"
  LOG_DIR="$(if [[ -n "$log_dir_opt" ]]; then printf '%s\n' "$log_dir_opt"; else resolve_host_console_log_dir "$(repo_root)"; fi)"
  ATTACH_SCRIPT="$(repo_root)/scripts/console-attach.sh"
  [[ -x "$ATTACH_SCRIPT" ]] || log "warn: $ATTACH_SCRIPT not executable"
  HEADLESS_SEEN=""

  if [[ "$force_headless" == 1 ]]; then
    MODE=headless
  elif [[ "$DRY_RUN" == 1 ]]; then
    MODE=pane
  elif command -v tmux >/dev/null 2>&1 && { [[ -n "${TMUX:-}" ]] || [[ -n "$DAEMON_PANE" ]]; } && tmux list-panes >/dev/null 2>&1; then
    MODE=pane
  else
    MODE=headless
  fi
  log "mode=$MODE log_dir=$LOG_DIR container=$CONTAINER daemon_pane=${DAEMON_PANE:-<none>} parent_pid=${PARENT_PID:-<none>} open_target=$OPEN_TARGET focus=$FOCUS layout=$LAYOUT interval=${interval}s dry_run=$DRY_RUN"

  if [[ "$once" == 1 ]]; then reconcile_once; return 0; fi
  while [[ "$STOP" == 0 ]]; do
    if parent_gone; then log "parent pid $PARENT_PID gone; exiting"; break; fi
    reconcile_once
    local slept=0
    while [[ "$slept" -lt "$interval" && "$STOP" == 0 ]]; do
      sleep 1; slept=$((slept + 1))
      if parent_gone; then STOP=1; fi
    done
  done
  log "stopped"
}

# Only run when executed directly; sourcing (tests) exposes functions without
# side effects.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
