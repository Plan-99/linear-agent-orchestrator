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
{"backend":"forged","event":"session_started","run_id":"$R1","supports_direct_input":true,"ts":"2026-07-10T12:50:01+00:00"}
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
check "hostlog default" "/repo/.runtime/logs/console" \
  "$(unset AGENT_CONSOLE_HOST_LOG_DIR LAO_RUNTIME_DIR; resolve_host_console_log_dir /repo)"
check "hostlog runtime-abs" "/data/rt/logs/console" \
  "$(unset AGENT_CONSOLE_HOST_LOG_DIR; export LAO_RUNTIME_DIR=/data/rt; resolve_host_console_log_dir /repo)"
check "hostlog runtime-rel" "/repo/rel/logs/console" \
  "$(unset AGENT_CONSOLE_HOST_LOG_DIR; export LAO_RUNTIME_DIR=rel; resolve_host_console_log_dir /repo)"
ENV_REPO="$TMP/env-repo"; mkdir -p "$ENV_REPO"
printf 'LAO_RUNTIME_DIR=.state\n' > "$ENV_REPO/.env"
check "hostlog dotenv runtime" "$ENV_REPO/.state/logs/console" \
  "$(unset AGENT_CONSOLE_HOST_LOG_DIR LAO_RUNTIME_DIR; resolve_host_console_log_dir "$ENV_REPO")"
printf 'LAO_RUNTIME_DIR=.state\nAGENT_CONSOLE_HOST_LOG_DIR=operator-logs\n' > "$ENV_REPO/.env"
check "hostlog dotenv explicit" "$ENV_REPO/operator-logs" \
  "$(unset AGENT_CONSOLE_HOST_LOG_DIR LAO_RUNTIME_DIR; resolve_host_console_log_dir "$ENV_REPO")"
check "hostlog process override wins" "/process/wins" \
  "$(export AGENT_CONSOLE_HOST_LOG_DIR=/process/wins LAO_RUNTIME_DIR=/ignored; resolve_host_console_log_dir "$ENV_REPO")"
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
  di="$(CONSOLE_FORCE_NO_JQ="$force" jsonl_field "$CD/$R1/lifecycle.jsonl" session_started supports_direct_input)"
  check "first-start capability is immutable ($fm)" "false" "$di"
  bk="$(CONSOLE_FORCE_NO_JQ="$force" jsonl_field "$CD/$R1/lifecycle.jsonl" session_started backend)"
  check "first-start backend is immutable ($fm)" "omx" "$bk"
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

# --interactive: read-write tmux client (scroll/mouse) without the direct-input
# gate — allowed even for non-direct backends.
out="$("$ATTACH" --run "$R1" --log-dir "$CD" --interactive --print-cmd 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && ok "attach --interactive allowed for non-direct backend" || bad "attach --interactive should be allowed (exit $rc)"
check "attach --interactive is read-write client" "attach -t lao-$R1" "$out"
[[ "$out" != *"attach -r"* ]] && ok "attach --interactive drops -r" || bad "attach --interactive must not use -r"

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
# Default open target: one window per run (daemon keeps window 0), focused.
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' 2>&1)"
check "supervisor R1 interactive window" "[interactive] new-window -a -t %1" "$out"
check "supervisor R1 passes --interactive" "--interactive" "$out"
check "supervisor R2 read-write window" "[read-write (direct input)] new-window -a -t %1" "$out"
check "supervisor focuses new window by default" "; focus" "$out"
[[ "$out" != *"select-layout"* ]] && ok "supervisor window mode skips layout" || bad "window mode must not apply layout"

out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' --focus false 2>&1)"
[[ "$out" != *"; focus"* ]] && ok "supervisor --focus false stays put" || bad "--focus false must not focus"
out="$(AGENT_CONSOLE_FOCUS=false "$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' 2>&1)"
[[ "$out" != *"; focus"* ]] && ok "supervisor reads AGENT_CONSOLE_FOCUS env" || bad "AGENT_CONSOLE_FOCUS=false must not focus"
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' --focus maybe 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "supervisor rejects invalid focus" || bad "supervisor should reject invalid focus"
[[ "$out" != *"lao:$R3"* ]] && ok "supervisor skips ended run" || bad "supervisor must skip ended run"
[[ "$out" != *"lao:$R4"* ]] && ok "supervisor skips killed run" || bad "supervisor must skip killed run"

# Split mode (opt-in): sibling panes with layout management.
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' --open-target split 2>&1)"
check "supervisor split mode uses split-window" "[interactive] split-window -t %1" "$out"
check "supervisor split applies default layout" "select-layout main-vertical" "$out"
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' --open-target split --layout tiled 2>&1)"
check "supervisor honors --layout" "select-layout tiled" "$out"
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' --layout bogus 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "supervisor rejects invalid layout" || bad "supervisor should reject invalid layout"
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' --open-target bogus 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "supervisor rejects invalid open-target" || bad "supervisor should reject invalid open-target"
out="$(AGENT_CONSOLE_LAYOUT=even-vertical "$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' --open-target split 2>&1)"
check "supervisor reads AGENT_CONSOLE_LAYOUT env" "select-layout even-vertical" "$out"
out="$(AGENT_CONSOLE_OPEN_TARGET=split "$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' 2>&1)"
check "supervisor reads AGENT_CONSOLE_OPEN_TARGET env" "split-window -t %1" "$out"

# A directory whose name is not a valid run id must be ignored entirely.
mkdir -p "$CD/NOTHEX"; printf '{"event":"session_started","run_id":"NOTHEX"}\n' > "$CD/NOTHEX/lifecycle.jsonl"
out="$("$SUP" --once --dry-run --log-dir "$CD" --daemon-pane '%1' 2>&1)"
[[ "$out" != *"NOTHEX"* ]] && ok "supervisor ignores non-hex run dir" || bad "supervisor must ignore non-hex run dir"
rm -rf "$CD/NOTHEX"

out="$("$SUP" --once --headless --log-dir "$CD" 2>&1)"
check "supervisor headless R1 interactive" "run $R1 live (headless) [interactive]" "$out"
check "supervisor headless R2 read-write" "run $R2 live (headless) [read-write (direct input)]" "$out"
check "supervisor headless hint keeps custom log dir" "--log-dir $CD" "$out"

# Anti-spoof: R5's issue text embeds a fake session_ended; must stay LIVE.
out="$("$ATTACH" --run "$R5" --log-dir "$CD" --print-cmd 2>&1)"
check "spoofed terminal text stays live (attach)" "attach -r -t lao-$R5" "$out"
out="$("$SUP" --once --dry-run --log-dir "$CD" 2>&1)"
check "spoofed terminal text stays live (supervisor)" "lao:$R5" "$out"

out="$("$SUP" --once --dry-run --log-dir "$CD" --interval 0 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok "supervisor rejects --interval 0" || bad "supervisor should reject --interval 0"

# --- real tmux: open_pane window mode must accept a daemon PANE id -----------
# The dry-run path never executes tmux, so target-type mistakes (new-window -t
# with a pane id fails with "can't specify pane here") only surface here.
if command -v tmux >/dev/null 2>&1; then
  IT_SOCK="$TMP/it-tmux.sock"
  if tmux -S "$IT_SOCK" new-session -d -s it -x 80 -y 24 'sleep 60' 2>/dev/null; then
    IT_PANE="$(tmux -S "$IT_SOCK" list-panes -t it -F '#{pane_id}' | head -1)"
    # A long-lived attach stub keeps the run window alive for focus assertions.
    IT_ATTACH="$TMP/it-attach"; printf '#!/bin/sh\nsleep 30\n' > "$IT_ATTACH"; chmod +x "$IT_ATTACH"
    res="$(
      set +e
      source "$SUP" 2>/dev/null
      export TMUX="$IT_SOCK,0,0"
      DRY_RUN=0; OPEN_TARGET=window; LAYOUT=main-vertical; FOCUS=false
      LOG_DIR="$CD"; CONTAINER=lao-daemon; ATTACH_SCRIPT=/bin/echo
      DAEMON_PANE="$IT_PANE"
      open_pane "$R1" >/dev/null 2>&1 && echo "OPENED-WINDOW"
      OPEN_TARGET=split
      open_pane "$R1" >/dev/null 2>&1 && echo "OPENED-SPLIT"
      OPEN_TARGET=window; FOCUS=true; ATTACH_SCRIPT="$IT_ATTACH"
      open_pane "$R2" >/dev/null 2>&1 && echo "OPENED-FOCUSED"
      tmux display-message -p -t it '#{window_name}' 2>/dev/null
    )"
    [[ "$res" == *OPENED-WINDOW* ]] && ok "real tmux: window mode opens from pane id" || bad "real tmux window open failed"
    [[ "$res" == *OPENED-SPLIT* ]] && ok "real tmux: split mode opens from pane id" || bad "real tmux split open failed"
    [[ "$res" == *OPENED-FOCUSED* ]] && ok "real tmux: focused window opens" || bad "real tmux focused open failed"
    [[ "$res" == *"lao:$R2"* ]] && ok "real tmux: focus switches to the new run window" || bad "focus did not switch active window: $res"
    tmux -S "$IT_SOCK" kill-server 2>/dev/null || true
  else
    ok "real tmux: skipped (no server available)"
  fi
else
  ok "real tmux: skipped (tmux not installed)"
fi

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
