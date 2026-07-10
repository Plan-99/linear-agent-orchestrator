#!/usr/bin/env bash
# lao-pty-entry: fixed, argument-free tmux session entry point.
#
# The orchestrator's runner never interpolates issue-controlled text into a tmux
# `shell-command` string. Instead the pty host writes the backend argv as a
# NUL-delimited file and a small KEY=VALUE config of orchestrator-generated
# paths, then starts this fixed script inside the tmux session with only
# LAO_PTY_RUN_DIR exported (via `tmux new-session -e`). This script reconstructs
# the argv verbatim from bytes, so no quoting/escaping decision is ever made
# from untrusted data.
set -uo pipefail

run_dir="${LAO_PTY_RUN_DIR:-}"
if [[ -z "$run_dir" || ! -d "$run_dir" ]]; then
  echo "lao-pty-entry: LAO_PTY_RUN_DIR missing or not a directory" >&2
  printf '127' >"${run_dir:-/dev/null}/exit_code" 2>/dev/null || true
  exit 127
fi

# Orchestrator-generated paths only (log destination); never issue content.
log_path=""
direct_input="false"
# shellcheck disable=SC1090
if [[ -f "$run_dir/config" ]]; then
  # config contains only orchestrator-generated log/capability values.
  while IFS='=' read -r key value; do
    case "$key" in
      LAO_PTY_LOG) log_path="$value" ;;
      LAO_PTY_DIRECT_INPUT) [[ "$value" == "true" ]] && direct_input="true" ;;
    esac
  done <"$run_dir/config"
fi

if [[ ! -r "$run_dir/argv" ]]; then
  echo "lao-pty-entry: argv file is not readable" >&2
  printf '127' >"$run_dir/exit_code"
  exit 127
fi

# Reconstruct argv verbatim from NUL-delimited bytes — no word splitting, no
# glob, no shell interpretation of any element.
mapfile -d '' -t argv <"$run_dir/argv"
if [[ ${#argv[@]} -eq 0 ]]; then
  echo "lao-pty-entry: empty argv" >&2
  printf '127' >"$run_dir/exit_code"
  exit 127
fi

# Run the backend as a child so this script can record the real backend PID and
# its exit status. Output is teed to the persistent log (host-readable, for
# failure comments) while still reaching the pane pty for live observation.
# stdin is connected to the PTY only for an explicit direct-input capability;
# otherwise it is /dev/null, matching the read-only UI contract and preventing
# accidental stdin readers from hanging until the runner timeout.
stdin_path="/dev/null"
[[ "$direct_input" == "true" ]] && stdin_path="/dev/tty"
if [[ -n "$log_path" ]]; then
  mkdir -p -- "$(dirname -- "$log_path")" 2>/dev/null || true
  "${argv[@]}" <"$stdin_path" > >(tee -a -- "$log_path") 2>&1 &
else
  "${argv[@]}" <"$stdin_path" &
fi
backend_pid=$!
printf '%s' "$backend_pid" >"$run_dir/backend_pid"
# Record /proc start time (field 22) so the orchestrator's timeout teardown can
# confirm the recorded PID has not been recycled before it kills the group.
python3 - "$backend_pid" >"$run_dir/backend_starttime" 2>/dev/null <<'PY' || true
import sys
pid = sys.argv[1]
data = open("/proc/%s/stat" % pid).read()
sys.stdout.write(data[data.rindex(")") + 1:].split()[19])
PY

wait "$backend_pid"
code=$?

# Persist the exit code atomically so the pty host reads a complete value even
# if it polls concurrently.
printf '%s' "$code" >"$run_dir/exit_code.tmp"
mv -f "$run_dir/exit_code.tmp" "$run_dir/exit_code"
exit "$code"
