#!/usr/bin/env bash
# lao-pty-host: host a single backend run inside a per-run PRIVATE tmux session.
#
# Design invariants (see .omx artifacts / collaboration contract):
#   * The tmux server uses a per-run PRIVATE socket under a container-only path
#     (/tmp/lao-console/<run-id>), never the default /tmp/tmux-<uid> socket and
#     never a bind-mounted path. The host tmux socket is never mounted in; the
#     host reaches a live session only by `docker exec ... tmux -S <socket>`.
#   * Issue-controlled text is NEVER interpolated into a tmux `shell-command`.
#     The backend argv is passed as a NUL-delimited file consumed by the fixed
#     lao-pty-entry script; tmux only ever runs that one argument-free script.
#   * This wrapper exits with the EXACT backend exit code so the orchestrator's
#     transport-exit Done gate is unchanged. It also tees a bounded log tail to
#     its own stdout so failure comments stay populated.
#   * Timeout teardown (kill of the escaped tmux server + backend PID) is the
#     orchestrator's job; this wrapper only handles the normal-completion path
#     and always leaves the recorded backend PID / exit code for the runner.
#
# If tmux is unavailable the wrapper degrades to a direct exec so headless /
# non-tmux execution keeps working.
set -uo pipefail

usage() {
  echo "usage: lao-pty-host --run-id ID --run-dir DIR --socket SOCK --session SESS --log LOG --jsonl JSONL [--schema-version N --backend NAME --supports-direct-input BOOL --issue-id ID --issue-identifier IDENT] -- CMD [ARGS...]" >&2
  exit 64
}

run_id="" run_dir="" socket="" session="" log_path="" jsonl_path=""
issue_id="" issue_identifier=""
schema_version="1" backend_name="" supports_direct_input="false"
entry="${LAO_PTY_ENTRY:-/usr/local/bin/lao-pty-entry}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) run_id="$2"; shift 2 ;;
    --run-dir) run_dir="$2"; shift 2 ;;
    --socket) socket="$2"; shift 2 ;;
    --session) session="$2"; shift 2 ;;
    --log) log_path="$2"; shift 2 ;;
    --jsonl) jsonl_path="$2"; shift 2 ;;
    --schema-version) schema_version="$2"; shift 2 ;;
    --backend) backend_name="$2"; shift 2 ;;
    --supports-direct-input) supports_direct_input="$2"; shift 2 ;;
    --issue-id) issue_id="$2"; shift 2 ;;
    --issue-identifier) issue_identifier="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "lao-pty-host: unknown argument: $1" >&2; usage ;;
  esac
done

backend_argv=("$@")
if [[ -z "$run_dir" || -z "$socket" || -z "$session" || -z "$log_path" || -z "$jsonl_path" || ${#backend_argv[@]} -eq 0 ]]; then
  usage
fi
case "${supports_direct_input,,}" in
  1|true|yes|on) supports_direct_input="true" ;;
  *) supports_direct_input="false" ;;
esac

mkdir -p -- "$run_dir" "$(dirname -- "$socket")" "$(dirname -- "$log_path")" "$(dirname -- "$jsonl_path")"
chmod 700 -- "$run_dir" "$(dirname -- "$socket")" 2>/dev/null || true

# Append one lifecycle event as a single, newline-terminated JSON object. Uses
# python3 for guaranteed-valid JSON escaping of issue-derived strings. Every
# event carries schema_version and run_id (contract v1, shared with the host
# supervisor).
emit_event() {
  local event="$1"; shift
  python3 - "$jsonl_path" "$run_id" "$schema_version" "$event" "$@" <<'PY' 2>/dev/null || true
import json, sys, time
jsonl, run_id, schema_version, event = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    schema_version = int(schema_version)
except ValueError:
    schema_version = 1
obj = {"event": event, "schema_version": schema_version, "run_id": run_id}
kv = sys.argv[5:]
for i in range(0, len(kv) - 1, 2):
    k, v = kv[i], kv[i + 1]
    if k in ("backend_pid", "exit_code"):
        try:
            v = int(v)
        except ValueError:
            v = None
    elif k == "supports_direct_input":
        v = v.strip().lower() in ("1", "true", "yes", "on")
    obj[k] = v
obj["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
with open(jsonl, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
PY
}

# Headless / no-tmux fallback: run the backend directly, preserving exit code.
if ! command -v tmux >/dev/null 2>&1; then
  emit_event session_started backend_pid "" socket "" session "" log_path "$log_path" \
    backend "$backend_name" supports_direct_input "$supports_direct_input" \
    issue_id "$issue_id" issue_identifier "$issue_identifier"
  set +e
  if [[ -n "$log_path" ]]; then
    mkdir -p -- "$(dirname -- "$log_path")" 2>/dev/null || true
    if [[ "$supports_direct_input" == "true" ]]; then
      "${backend_argv[@]}" > >(tee -a -- "$log_path") 2>&1
    else
      "${backend_argv[@]}" </dev/null > >(tee -a -- "$log_path") 2>&1
    fi
  else
    if [[ "$supports_direct_input" == "true" ]]; then
      "${backend_argv[@]}"
    else
      "${backend_argv[@]}" </dev/null
    fi
  fi
  code=$?
  set -e 2>/dev/null || true
  # Terminal-event guard (see tmux path): the orchestrator owns the terminal
  # event if it tore the run down on timeout.
  if [[ -f "$run_dir/terminated" ]]; then
    exit 137
  fi
  emit_event session_ended exit_code "$code"
  exit "$code"
fi

# Write the fixed entry's config (orchestrator-generated log path only) and the
# NUL-delimited backend argv. No issue text is ever interpreted by a shell.
printf 'LAO_PTY_LOG=%s\nLAO_PTY_DIRECT_INPUT=%s\n' \
  "$log_path" "$supports_direct_input" >"$run_dir/config"
printf '%s\0' "${backend_argv[@]}" >"$run_dir/argv"
: >"$log_path"
rm -f -- "$run_dir/backend_pid" "$run_dir/exit_code"

# Start-of-run sweep: a crashed prior run on the same socket would orphan a
# server. The run-id is unique per run, so this normally does nothing, but the
# guard keeps a reused path clean.
if [[ -S "$socket" ]]; then
  tmux -S "$socket" kill-server 2>/dev/null || true
fi
rm -f -- "$socket"

# Start the detached private session. `-e` passes only the run-dir; the entry
# script is fixed and argument-free.
if ! tmux -S "$socket" new-session -d -s "$session" -x 200 -y 50 \
     -e "LAO_PTY_RUN_DIR=$run_dir" "$entry"; then
  # Startup failed before any session_started; emit the single allowed terminal
  # event (session_killed reason=start_failed, per console_schema v1). The run
  # still fails via this nonzero transport exit, which the strict Done gate
  # treats as failure.
  echo "lao-pty-host: failed to start tmux session" >&2
  emit_event session_killed reason start_failed
  exit 70
fi

# Best-effort hardening of the inner session for a safe host attach.
tmux -S "$socket" set-option -t "$session" status off 2>/dev/null || true
tmux -S "$socket" set-option -t "$session" remain-on-exit off 2>/dev/null || true
tmux -S "$socket" set-option -g allow-passthrough off 2>/dev/null || true

# Wait briefly for the entry script to record the real backend PID.
backend_pid=""
for _ in $(seq 1 50); do
  if [[ -s "$run_dir/backend_pid" ]]; then
    backend_pid="$(cat "$run_dir/backend_pid" 2>/dev/null || true)"
    break
  fi
  if ! tmux -S "$socket" has-session -t "$session" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

emit_event session_started backend_pid "$backend_pid" socket "$socket" session "$session" \
  log_path "$log_path" backend "$backend_name" supports_direct_input "$supports_direct_input" \
  issue_id "$issue_id" issue_identifier "$issue_identifier"

# Block until the session ends (backend exited and entry returned).
while tmux -S "$socket" has-session -t "$session" 2>/dev/null; do
  sleep 0.2
done

# Terminal-event guard: if the orchestrator's timeout teardown dropped a
# `terminated` marker, it already owns the single terminal event
# (session_killed). Do not also emit session_ended — exactly one terminal event
# per session. Just reap and exit nonzero (transport failure).
if [[ -f "$run_dir/terminated" ]]; then
  tmux -S "$socket" kill-server 2>/dev/null || true
  rm -f -- "$socket"
  exit 137
fi

# Fail closed: a missing exit code means the session died without a clean
# handoff, which must not read as success.
code=1
if [[ -s "$run_dir/exit_code" ]]; then
  code="$(cat "$run_dir/exit_code" 2>/dev/null || echo 1)"
  [[ "$code" =~ ^[0-9]+$ ]] || code=1
fi

emit_event session_ended exit_code "$code"

# Clean up the private server and socket for the next run.
tmux -S "$socket" kill-server 2>/dev/null || true
rm -f -- "$socket"

# Surface the backend output on this wrapper's own stdout so process.communicate
# in the runner captures it for failure comments even if it never reads the log.
if [[ -s "$log_path" ]]; then
  echo "--- lao-pty-host: backend output (tail) ---"
  tail -c 100000 -- "$log_path" 2>/dev/null || true
fi

exit "$code"
