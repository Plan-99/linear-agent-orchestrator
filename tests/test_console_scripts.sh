#!/usr/bin/env bash
# Bash tests for the host-side tmux agent UI scripts (Tasks 2 & 3).
#
# Self-contained: builds a fixture console log tree and exercises
# console-lib.sh, console-attach.sh, and console-supervisor.sh without needing a
# live tmux server or docker. Run: bash tests/test_console_scripts.sh
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT_DIR/scripts"
LIB="$SCRIPTS/console-lib.sh"
ATTACH="$SCRIPTS/console-attach.sh"
SUP="$SCRIPTS/console-supervisor.sh"

pass=0; fail=0
ok()   { printf 'ok   - %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf 'FAIL - %s\n' "$1"; fail=$((fail + 1)); }
check(){ # desc, expected-substr, actual
  if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1 (want substr: $2 | got: $3)"; fi
}

# --- fixture -----------------------------------------------------------------
# Run ids are 32-char lowercase hex (frozen console schema ^[a-f0-9]{32}$).
R1="0123456789abcdef0123456789abcdef"   # omx, no direct input, live
R2="abcdef0123456789abcdef0123456789"   # custom, direct input, live (+ torn line)
R3="fedcba9876543210fedcba9876543210"   # ended (terminal)
R4="deadbeefdeadbeefdeadbeefdeadbeef"   # killed (terminal)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
CD="$TMP/console"; mkdir -p "$CD/$R1" "$CD/$R2" "$CD/$R3" "$CD/$R4"
cat > "$CD/$R1/lifecycle.jsonl" <<EOF
{"backend":"omx","backend_pid":1234,"event":"session_started","issue_id":"i-1","issue_identifier":"DEV-1","log_path":"x","run_id":"$R1","session":"lao-$R1","socket":"/tmp/lao-console/$R1/tmux.sock","supports_direct_input":false,"ts":"2026-07-10T12:50:00+00:00"}
EOF
cat > "$CD/$R2/lifecycle.jsonl" <<EOF
{"backend":"custom","backend_pid":2222,"event":"session_started","issue_id":"i-2","issue_identifier":"DEV-2","log_path":"x","run_id":"$R2","session":"lao-$R2","socket":"/tmp/lao-console/$R2/tmux.sock","supports_direct_input":true,"ts":"2026-07-10T12:51:00+00:00"}
{"event":"session_started","run_id":"abcdef012
EOF
cat > "$CD/$R3/lifecycle.jsonl" <<EOF
{"backend":"claude","event":"session_started","issue_identifier":"DEV-3","run_id":"$R3","session":"lao-$R3","socket":"/tmp/lao-console/$R3/tmux.sock","supports_direct_input":false,"ts":"2026-07-10T12:52:00+00:00"}
{"event":"session_ended","exit_code":0,"run_id":"$R3","ts":"2026-07-10T12:53:00+00:00"}
EOF
cat > "$CD/$R4/lifecycle.jsonl" <<EOF
{"backend":"omx","event":"session_started","issue_identifier":"DEV-4","run_id":"$R4","session":"lao-$R4","socket":"/tmp/lao-console/$R4/tmux.sock","supports_direct_input":false,"ts":"2026-07-10T12:54:00+00:00"}
{"event":"session_killed","reason":"timeout","run_id":"$R4","ts":"2026-07-10T12:55:00+00:00"}
EOF
# R5: issue text tries to spoof a terminal event via substring — structured
# parsing must still treat it as LIVE (no real session_ended/killed record).
R5="00000000000000000000000000000005"; mkdir -p "$CD/$R5"
cat > "$CD/$R5/lifecycle.jsonl" <<EOF
{"backend":"omx","event":"session_started","issue_identifier":"pwn \"event\": \"session_ended\" exit_code 0","run_id":"$R5","session":"lao-$R5","socket":"/tmp/lao-console/$R5/tmux.sock","supports_direct_input":false,"ts":"2026-07-10T12:56:00+00:00"}
EOF

# --- console-lib.sh ----------------------------------------------------------
# shellcheck source=scripts/console-lib.sh
source "$LIB"
check "console_mode unset+tmux=on"  "on"  "$(console_mode '' 'x' '')"
check "console_mode unset+pane=on"  "on"  "$(console_mode '' '' '%1')"
check "console_mode unset+none=off" "off" "$(console_mode '' '' '')"
check "console_mode false=off"      "off" "$(console_mode 'false' 'x' '%1')"
check "console_mode true=on"        "on"  "$(console_mode 'true' '' '')"
check "console_mode auto+tmux=on"   "on"  "$(console_mode 'auto' 'x' '')"
check "console_mode auto+none=off"  "off" "$(console_mode 'auto' '' '')"
_ev="$TMP/dotenv"; printf 'FOO=1\nAGENT_CONSOLE="auto"\n' > "$_ev"
check "read_env_value quoted" "auto" "$(read_env_value AGENT_CONSOLE "$_ev")"
printf 'AGENT_CONSOLE=false\n' > "$_ev"
check "read_env_value plain" "false" "$(read_env_value AGENT_CONSOLE "$_ev")"
( unset AGENT_CONSOLE_HOST_LOG_DIR LAO_RUNTIME_DIR; source "$LIB"
  check "hostlog default" "/repo/.runtime/logs/console" "$(resolve_host_console_log_dir /repo)" )
( export LAO_RUNTIME_DIR=/data/rt; source "$LIB"
  check "hostlog runtime-abs" "/data/rt/logs/console" "$(resolve_host_console_log_dir /repo)" )
( export LAO_RUNTIME_DIR=rel; source "$LIB"
  check "hostlog runtime-rel" "/repo/rel/logs/console" "$(resolve_host_console_log_dir /repo)" )
_exists() { case "$1" in lao-daemon|lao-daemon-2) return 0;; *) return 1;; esac; }
check "next_free_container_name" "lao-daemon-3" "$(next_free_container_name lao-daemon _exists)"

# Torn final line must not discard earlier valid events. R2 ends with a partial
# line; run under both the jq path (if present) and the forced Python path.
have_jq=0; command -v jq >/dev/null 2>&1 && have_jq=1
for fm in jq py; do
  [[ "$fm" == jq && "$have_jq" == 0 ]] && continue
  force=""; [[ "$fm" == py ]] && force=1
  di="$(CONSOLE_FORCE_NO_JQ="$force" jsonl_field "$CD/$R2/lifecycle.jsonl" session_started supports_direct_input)"
  check "torn-line: direct=true survives ($fm)" "true" "$di"
  bk="$(CONSOLE_FORCE_NO_JQ="$force" jsonl_field "$CD/$R2/lifecycle.jsonl" session_started backend)"
  check "torn-line: backend survives ($fm)" "custom" "$bk"
  ts="$(CONSOLE_FORCE_NO_JQ="$force" jsonl_terminal_state "$CD/$R3/lifecycle.jsonl")"
  check "terminal_state ended ($fm)" "ended:0" "$ts"
  ts="$(CONSOLE_FORCE_NO_JQ="$force" jsonl_terminal_state "$CD/$R4/lifecycle.jsonl")"
  check "terminal_state killed ($fm)" "killed:timeout" "$ts"
  ts="$(CONSOLE_FORCE_NO_JQ="$force" jsonl_terminal_state "$CD/$R1/lifecycle.jsonl")"
  [[ -z "$ts" ]] && ok "terminal_state live empty ($fm)" || bad "live should be empty ($fm): $ts"
done

# --- console-attach.sh -------------------------------------------------------
out="$("$ATTACH" --list --log-dir "$CD" 2>&1)"
check "attach --list shows R1" "$R1" "$out"
check "attach --list shows R2 direct" "true" "$out"

out="$("$ATTACH" --run "$R1" --log-dir "$CD" --print-cmd 2>&1)"
check "attach R1 read-only (-r)" "attach -r -t lao-$R1" "$out"

out="$("$ATTACH" --run "$R1" --log-dir "$CD" --write --print-cmd 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "attach --write refused for non-direct backend (exit $rc)" || bad "attach --write should refuse non-direct"
check "attach --write refuse message" "safety gate" "$out"

out="$("$ATTACH" --run "$R2" --log-dir "$CD" --write --print-cmd 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && ok "attach --write allowed for direct backend" || bad "attach --write should allow direct (exit $rc)"
check "attach R2 read-write (no -r)" "attach -t lao-$R2" "$out"

out="$("$ATTACH" --run "$R3" --log-dir "$CD" --print-cmd 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && ok "attach terminal(ended) run exits 0" || bad "attach terminal run should exit 0"
check "attach terminal not attaching" "already terminal" "$out"

out="$("$ATTACH" --run "$R4" --log-dir "$CD" --print-cmd 2>&1)"
check "attach killed run reports terminal" "already terminal" "$out"

# Rejection tests for run-id schema (leader: short/uppercase/metacharacter).
for bad_id in \
  'abc123' \
  '0123456789ABCDEF0123456789abcdef' \
  '0123456789abcdef0123456789abcde' \
  '0123456789abcdef0123456789abcdeff' \
  '0123456789abcdef0123456789abcde;' \
  'BAD;rm -rf /' \
  '../../etc/passwd00000000000000000'
do
  out="$("$ATTACH" --run "$bad_id" --log-dir "$CD" --print-cmd 2>&1)"; rc=$?
  [[ $rc -ne 0 ]] && ok "attach rejects invalid run id: '$bad_id'" || bad "attach should reject run id: '$bad_id'"
done

# --- console-supervisor.sh ---------------------------------------------------
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' 2>&1)"
check "supervisor R1 read-only pane" "[read-only] split-window -t %1" "$out"
check "supervisor R2 read-write pane" "[read-write (direct input)] split-window -t %1" "$out"
[[ "$out" != *"lao:$R3"* ]] && ok "supervisor skips ended run" || bad "supervisor must skip ended run"
[[ "$out" != *"lao:$R4"* ]] && ok "supervisor skips killed run" || bad "supervisor must skip killed run"

# A directory whose name is not a valid run id must be ignored entirely.
mkdir -p "$CD/NOTHEX"; printf '{"event":"session_started","run_id":"NOTHEX"}\n' > "$CD/NOTHEX/lifecycle.jsonl"
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' 2>&1)"
[[ "$out" != *"NOTHEX"* ]] && ok "supervisor ignores non-hex run dir" || bad "supervisor must ignore non-hex run dir"
rm -rf "$CD/NOTHEX"

out="$("$SUP" --once --headless --log-dir "$CD" 2>&1)"
check "supervisor headless R1 read-only" "run $R1 live (headless) [read-only]" "$out"
check "supervisor headless R2 read-write" "run $R2 live (headless) [read-write (direct input)]" "$out"

# Anti-spoof: R5's issue text embeds a fake session_ended; must stay LIVE.
out="$("$ATTACH" --run "$R5" --log-dir "$CD" --print-cmd 2>&1)"
check "spoofed terminal text stays live (attach)" "attach -r -t lao-$R5" "$out"
out="$("$SUP" --once --dry-run --log-dir "$CD" 2>&1)"
check "spoofed terminal text stays live (supervisor)" "lao:$R5" "$out"

out="$("$SUP" --once --dry-run --log-dir "$CD" --interval 0 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "supervisor rejects --interval 0" || bad "supervisor should reject --interval 0"

# --- supervisor internals: orphan skip + open dedupe (mocked docker/tmux) -----
res="$(
  set +e
  source "$SUP" 2>/dev/null           # source guard: main() does not run
  MODE=pane; DRY_RUN=0; LOG_DIR="$CD"; CONTAINER=lao-daemon
  ATTACH_SCRIPT="$ATTACH"; DAEMON_PANE="%1"; HEADLESS_SEEN=""
  OPENED_SEEN=""; ORPHAN_SEEN=""; opened=""
  check_container(){ return 0; }
  existing_pane_for(){ return 1; }      # no pre-existing panes
  open_pane(){ opened="$opened $1"; return 0; }   # record instead of tmux
  session_alive(){ return 0; }          # all sessions live
  reconcile_once >/dev/null 2>&1; echo "PASS1:$opened"
  opened=""; reconcile_once >/dev/null 2>&1; echo "PASS2:$opened"   # dedupe via OPENED_SEEN
  OPENED_SEEN=""; ORPHAN_SEEN=""; opened=""
  session_alive(){ return 1; }          # sessions gone -> orphans
  reconcile_once >/dev/null 2>&1; echo "ORPH:$opened|seen:$ORPHAN_SEEN"
)"
p1="$(sed -n 's/^PASS1://p' <<<"$res")"
p2="$(sed -n 's/^PASS2://p' <<<"$res")"
orph="$(sed -n 's/^ORPH://p' <<<"$res")"
[[ "$p1" == *"$R1"* && "$p1" == *"$R2"* ]] && ok "mock: opens live runs on first pass" || bad "mock pass1 opened='$p1'"
[[ "$p1" != *"$R3"* && "$p1" != *"$R4"* ]] && ok "mock: skips terminal runs" || bad "mock pass1 included terminal='$p1'"
[[ -z "${p2// }" ]] && ok "mock: pane-close dedupe (no reopen on pass 2)" || bad "mock pass2 reopened='$p2'"
[[ "$orph" == *"opened:"* || "$orph" == "|seen:"* ]] || true
orph_opened="${orph%%|*}"
[[ -z "${orph_opened// }" ]] && ok "mock: orphan runs not opened" || bad "mock orphan opened='$orph_opened'"
[[ "$orph" == *"$R1"* && "$orph" == *"$R2"* ]] && ok "mock: orphans recorded in ORPHAN_SEEN" || bad "mock orphan seen='$orph'"

out="$("$SUP" --once --dry-run --log-dir "$TMP/nope" 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && ok "supervisor missing dir graceful" || bad "supervisor missing dir should exit 0"

out="$("$SUP" --once --dry-run --log-dir "$CD" --container 'bad name;x' 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "supervisor rejects bad container" || bad "supervisor should reject bad container"

out="$("$SUP" --once --dry-run --log-dir "$CD" --parent-pid 'notanumber' 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "supervisor rejects bad parent-pid" || bad "supervisor should reject bad parent-pid"

# --- daemon.sh integration (env resolution + -e forwarding + supervisor) -----
# Build a throwaway repo with stubbed bootstrap/compose/supervisor so we can run
# daemon.sh end-to-end without docker/tmux and capture the compose invocation.
DREPO="$TMP/repo"; mkdir -p "$DREPO/scripts"
cp "$SCRIPTS/daemon.sh" "$SCRIPTS/console-lib.sh" "$DREPO/scripts/"
printf '#!/usr/bin/env bash\ntrue\n' > "$DREPO/scripts/bootstrap.sh"
cat > "$DREPO/scripts/compose.sh" <<'X'
#!/usr/bin/env bash
echo "COMPOSE: $*"
X
cat > "$DREPO/scripts/console-supervisor.sh" <<'X'
#!/usr/bin/env bash
echo "SUPERVISOR: $*"
X
chmod +x "$DREPO"/scripts/*.sh

run_daemon() { # env-assignments... ; sets $DOUT
  # Run in a subshell with a clean AGENT_CONSOLE unless caller exports it.
  DOUT="$(cd "$DREPO" && env -u TMUX -u TMUX_PANE -u AGENT_CONSOLE "$@" bash scripts/daemon.sh 2>/dev/null)"
  sleep 1  # let the backgrounded supervisor stub flush to supervisor.log
}

# Case A: unset shell, .env=auto, no tmux -> off -> -e AGENT_CONSOLE=false, no --name
printf 'AGENT_CONSOLE=auto\n' > "$DREPO/.env"
run_daemon
check "daemon A: forwards console=false" "-e AGENT_CONSOLE=false" "$DOUT"
[[ "$DOUT" != *"--name"*  ]] && ok "daemon A: no --name when off" || bad "daemon A: should not add --name"

# Case B: shell AGENT_CONSOLE=true, no tmux -> on -> -e true + --name + supervisor
run_daemon AGENT_CONSOLE=true
check "daemon B: forwards console=true" "-e AGENT_CONSOLE=true" "$DOUT"
check "daemon B: pins container name" "--name lao-daemon" "$DOUT"
sup_out="$(cat "$DREPO/.runtime/logs/console/supervisor.log" 2>/dev/null || true)"
check "daemon B: supervisor got parent-pid" "--parent-pid" "$sup_out"
check "daemon B: supervisor got container" "--container lao-daemon" "$sup_out"

# Case C: .env=false but inside tmux -> explicit false must win -> off
printf 'AGENT_CONSOLE=false\n' > "$DREPO/.env"
run_daemon TMUX=/tmp/fake TMUX_PANE=%1
check "daemon C: explicit false disables in tmux" "-e AGENT_CONSOLE=false" "$DOUT"

# Case D: .env=auto + inside tmux -> on
printf 'AGENT_CONSOLE=auto\n' > "$DREPO/.env"
rm -f "$DREPO/.runtime/logs/console/supervisor.log"
run_daemon TMUX_PANE=%3
check "daemon D: auto+tmux enables" "-e AGENT_CONSOLE=true" "$DOUT"
sup_out="$(cat "$DREPO/.runtime/logs/console/supervisor.log" 2>/dev/null || true)"
check "daemon D: supervisor got daemon-pane" "--daemon-pane %3" "$sup_out"

# --- summary -----------------------------------------------------------------
printf '\n# %d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
