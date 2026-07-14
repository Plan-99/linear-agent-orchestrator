#!/usr/bin/env bash
# console-lib.sh — pure, side-effect-free helpers shared by the host-side tmux
# agent UI scripts (daemon.sh, console-supervisor.sh, console-attach.sh) and the
# bash tests. Sourcing this file must not run anything or touch the environment.

# console_mode <agent_console_flag> <tmux> <tmux_pane> -> "on" | "off"
# The console UI auto-enables when the daemon launches inside tmux, unless
# AGENT_CONSOLE is explicitly disabled. Explicit enable forces it on even off-tmux
# (useful for tests / remote attach where the operator drives panes manually).
console_mode() {
  local flag="${1:-}" tmux="${2:-}" pane="${3:-}"
  local lc="${flag,,}"
  case "$lc" in
    false|0|off|no|disabled) printf 'off\n'; return ;;
    true|1|on|yes|enabled)   printf 'on\n';  return ;;
    auto|''|*) : ;;   # auto / unset / unknown -> decide by tmux presence
  esac
  if [[ -n "$tmux" || -n "$pane" ]]; then printf 'on\n'; else printf 'off\n'; fi
}

# read_env_value <key> <dotenv_file> -> value (stdout), or non-zero if absent.
# Safely reads a single KEY=VALUE from a .env file WITHOUT sourcing it (never
# executes the file). Last assignment wins; surrounding quotes are stripped.
read_env_value() {
  local key="$1" file="$2" line val
  [[ -r "$file" ]] || return 1
  line="$(grep -E "^[[:space:]]*${key}=" "$file" 2>/dev/null | tail -n1)" || return 1
  [[ -n "$line" ]] || return 1
  val="${line#*=}"
  val="${val%%[$'\r']}"
  val="${val#\"}"; val="${val%\"}"
  val="${val#\'}"; val="${val%\'}"
  printf '%s\n' "$val"
}

# resolve_host_console_log_dir <repo_root> [dotenv_file] -> host filesystem path
# IMPORTANT: this is the HOST path the bind mount lands on, NOT the in-container
# AGENT_CONSOLE_LOG_DIR (=/workspace/logs/console). Resolution order:
#   1. process AGENT_CONSOLE_HOST_LOG_DIR (explicit host override)
#   2. dotenv AGENT_CONSOLE_HOST_LOG_DIR
#   3. <repo_root>/.runtime/logs/console
# Compose reads project .env automatically, while these host scripts do not.
# Reading the explicit host override safely here keeps the watched host path
# identical to the bind mount when operators configure it only in .env.
# LAO_RUNTIME_DIR is deliberately ignored: scripts/compose.sh owns it as
# internal path-translation plumbing, so it is not a user-facing path override.
resolve_host_console_log_dir() {
  local repo_root="${1:-}" env_file="${2:-${1:-}/.env}"
  local host_override="${AGENT_CONSOLE_HOST_LOG_DIR:-}"
  if [[ -z "$host_override" ]]; then
    host_override="$(read_env_value AGENT_CONSOLE_HOST_LOG_DIR "$env_file" 2>/dev/null || true)"
  fi
  if [[ -n "$host_override" ]]; then
    case "$host_override" in /*) : ;; *) host_override="$repo_root/$host_override" ;; esac
    printf '%s\n' "$host_override"; return
  fi
  printf '%s\n' "$repo_root/.runtime/logs/console"
}

# default_container_name -> configured daemon container name
default_container_name() {
  printf '%s\n' "${AGENT_CONSOLE_CONTAINER:-${LAO_DAEMON_CONTAINER_NAME:-lao-daemon}}"
}

# jsonl_field <file> <event> <field> -> first matching value (structured).
# Parses each line as JSON and selects objects whose .event == <event>, then
# returns the requested field of the FIRST such object. Identity/capability
# metadata is immutable after the orchestrator's initial session_started; later
# appended records cannot elevate direct-input. Never executes JSONL content or
# substring-matches (issue text cannot spoof the event type).
jsonl_field() {
  local file="$1" event="$2" field="$3"
  [[ -r "$file" ]] || return 1
  if [[ -z "${CONSOLE_FORCE_NO_JQ:-}" ]] && command -v jq >/dev/null 2>&1; then
    # Raw line-by-line: `fromjson?` skips a torn/partial final line instead of
    # failing the whole file (slurp mode `-s` would). `has()` (not `//`)
    # preserves legitimate false/0 field values. sed prints the first match but
    # still drains jq's output, avoiding SIGPIPE under this file's pipefail mode.
    jq -Rr --arg ev "$event" --arg f "$field" '
      fromjson? | select(type=="object" and .event==$ev)
      | if has($f) then .[$f] else empty end | tostring
    ' "$file" 2>/dev/null | sed -n '1p'
  else
    python3 - "$file" "$event" "$field" <<'PY' 2>/dev/null
import json,sys
path,ev,field=sys.argv[1],sys.argv[2],sys.argv[3]
val=None
found=False
with open(path,encoding="utf-8",errors="replace") as fh:
    for line in fh:
        line=line.strip()
        if not line: continue
        try: obj=json.loads(line)
        except Exception: continue
        if not found and isinstance(obj,dict) and obj.get("event")==ev and field in obj:
            val=obj[field]
            found=True
if not found: sys.exit(1)
print(val if not isinstance(val,bool) else ("true" if val else "false"))
PY
  fi
}

# jsonl_has_event <file> <event> -> 0 iff a record with that .event exists.
jsonl_has_event() {
  local v; v="$(jsonl_field "$1" "$2" event 2>/dev/null || true)"
  [[ -n "$v" ]]
}

# jsonl_terminal_state <file> -> "ended:<code>" | "killed:<reason>" | ""
# Determined by the LAST session_ended/session_killed record, parsed
# structurally (not by grep/awk substring, which issue text could spoof).
jsonl_terminal_state() {
  local file="$1"
  [[ -r "$file" ]] || { printf '\n'; return; }
  if [[ -z "${CONSOLE_FORCE_NO_JQ:-}" ]] && command -v jq >/dev/null 2>&1; then
    # Raw line-by-line so a torn final line cannot discard earlier valid events.
    jq -Rr '
      fromjson? | select(type=="object" and (.event=="session_ended" or .event=="session_killed"))
      | if .event=="session_ended" then "ended:\(if has("exit_code") then .exit_code else "?" end)"
        else "killed:\(if has("reason") then .reason else "?" end)" end
    ' "$file" 2>/dev/null | tail -n1
  else
    python3 - "$file" <<'PY' 2>/dev/null
import json,sys
last=None
with open(sys.argv[1],encoding="utf-8",errors="replace") as fh:
    for line in fh:
        line=line.strip()
        if not line: continue
        try: o=json.loads(line)
        except Exception: continue
        if isinstance(o,dict) and o.get("event") in ("session_ended","session_killed"):
            last=o
if not last: print("")
elif last.get("event")=="session_ended": print("ended:%s"%last.get("exit_code","?"))
else: print("killed:%s"%last.get("reason","?"))
PY
  fi
}

# next_free_container_name <base> <exists_fn>
# exists_fn NAME returns 0 iff a container named NAME already exists (any state).
# Returns the first free name among base, base-2, base-3, ...  Never removes or
# kills anything — collision-safe by picking a new name (leader decision).
next_free_container_name() {
  local base="$1" exists_fn="$2" n=1 candidate="$1"
  while "$exists_fn" "$candidate"; do
    n=$((n + 1)); candidate="${base}-${n}"
    if [[ $n -gt 64 ]]; then printf '%s\n' "$candidate"; return; fi
  done
  printf '%s\n' "$candidate"
}
