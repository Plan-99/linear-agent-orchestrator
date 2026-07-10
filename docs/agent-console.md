# Agent console — lifecycle JSONL contract & tmux boundary review

The interactive agent console lets an operator watch (and, where the backend
supports it, steer) a live agent run inside the container through a tmux
session, while a host supervisor consumes an append-only lifecycle JSONL to
attach to and reap sessions. This document is the authoritative **v1 contract**
and the security review of the host/container boundary.

Canonical constants/helpers/validator: `src/linear_agent_orchestrator/console_schema.py`.

## Feature gate & configuration (backend-neutral)

| env | default | meaning |
| --- | --- | --- |
| `AGENT_CONSOLE` | `auto` | Master gate. `auto` = enable inside a host tmux, headless otherwise; `true`/`false` force. |
| `AGENT_CONSOLE_LOG_DIR` | `/workspace/logs/console` | Container path for the host-readable per-run JSONL + output logs. |
| `AGENT_CONSOLE_SOCKET_DIR` | `/tmp/lao-console` | Container-private per-run tmux socket/argv root. Never bind-mounted. |
| `AGENT_CONSOLE_DIRECT_INPUT` | `false` | Explicit capability metadata: may the console send keystrokes to the agent? Never inferred from the backend name. Forced `true` per run when `AGENT_INTERACTIVE=true` (the TUI genuinely reads the PTY). |
| `AGENT_CONSOLE_OPEN_TARGET` | `window` | Where the supervisor opens each run: `window` (daemon keeps window 0, each run gets its own `lao:<run_id>` window so team-mode backends can split panes inside it) or `split` (sibling pane in the daemon's window). |
| `AGENT_CONSOLE_FOCUS` | `true` | Switch the tmux client to the newly opened run window/pane. `false` keeps the operator where they are. |
| `AGENT_CONSOLE_LAYOUT` | `main-vertical` | Split mode only: window layout re-applied when a run pane opens, keeping the daemon (first pane) as the main pane. One of `main-vertical` \| `main-horizontal` \| `tiled` \| `even-vertical` \| `even-horizontal` \| `none`. |
| `AGENT_CONSOLE_HOST_LOG_DIR` | `${LAO_RUNTIME_DIR}/logs/console` | The **host console directory itself**, bind-mounted at `/workspace/logs/console`; override for host scripts. |

The inner private tmux server enables `mouse on` plus a 50k-line scrollback, so
an attached client can wheel-scroll into copy-mode and click between inner
panes (e.g. a team-mode backend splitting its own session). A read-only
(`attach -r`) client cannot do either, so the supervisor opens non-direct-input
runs with `console-attach.sh --interactive`: a read-write tmux client without
the `--write` gate. Typed keys reach the PTY but the bundled one-shot adapters
never read them; `--write` (gated on `supports_direct_input`) remains the only
honest "operator steers the backend" mode, and manual attach still defaults to
read-only.

- Default is **`auto`**: `scripts/daemon.sh` enables the console when it runs
  inside a host tmux and falls back to the normal headless path otherwise;
  `true`/`false` force the choice.
- Compose bind-mounts the host console directory
  `${AGENT_CONSOLE_HOST_LOG_DIR:-${LAO_RUNTIME_DIR:-./.runtime}/logs/console}`
  at `/workspace/logs/console` — the exact directory the host resolver watches.
  It is durable and host-owned under the checkout's `.runtime/logs/console`.
  `scripts/bootstrap.sh` creates `.runtime/logs`.

The contract module provides env parsers (`console_enabled`,
`direct_input_enabled`, `resolve_console_log_dir`) plus event/path helpers;
`config.py` maps the runtime values into `RunnerConfig`.

## Path layout

Per `run_id` (a uuid4 hex string, validated `^[a-f0-9]{32}$`, shared with the
host scripts):

```
container-private (NOT bind-mounted):
  /tmp/lao-console/<run_id>/tmux.sock     # tmux control socket
  /tmp/lao-console/<run_id>/argv          # backend argv (may name secrets)
  /tmp/lao-console/<run_id>/exit_code

host-readable (bind-mounted under .runtime/logs/console):
  <AGENT_CONSOLE_LOG_DIR>/<run_id>/lifecycle.jsonl
  <AGENT_CONSOLE_LOG_DIR>/<run_id>/output.log
```

The tmux session is named `lao-<run_id>`. Because `run_id` is strictly
`[a-f0-9]{32}`, the session name and every derived path contain no `.`/`:`
(tmux target delimiters), path separators, or shell metacharacters.

## Lifecycle JSONL format

- One JSON object per line, `O_APPEND`, newline-terminated.
- Consumers must **tolerate a torn final line** (a crash mid-write): parse
  line-by-line and skip a trailing partial line rather than failing the stream.
- For cleanly observed runs, `session_started` precedes exactly one of
  `session_ended` | `session_killed` for a given `run_id`. Abrupt container/PID 1
  loss can leave only `session_started`; the host supervisor verifies the
  private tmux session and treats such persisted records as orphans.

### Emitter events (frozen v1)

`session_started`
```json
{"event":"session_started","run_id":"<hex>","issue_id":"<id|null>","issue_identifier":"<DEV-1|null>","session":"lao-<run_id>","socket":"/tmp/lao-console/<run_id>/tmux.sock","backend":"omx","backend_pid":1234,"supports_direct_input":false,"log_path":"<AGENT_CONSOLE_LOG_DIR>/<run_id>/output.log","ts":"<iso8601>"}
```

`session_ended`
```json
{"event":"session_ended","run_id":"<hex>","exit_code":0,"ts":"<iso8601>"}
```

`session_killed`  (`reason` ∈ `timeout` | `teardown` | `start_failed`)
```json
{"event":"session_killed","run_id":"<hex>","reason":"timeout","ts":"<iso8601>"}
```

Terminal kill semantics: `timeout` = the run exceeded its deadline;
`start_failed` = the tmux session or backend process failed to start;
`teardown` is reserved for a future explicit graceful reaper. A
`session_killed` is terminal and mutually exclusive with `session_ended` for
the same cleanly observed run.

Each record also carries `schema_version` (currently `1`). Keys serialize
sorted for stable diffs.

### Supervisor events (reserved)

The host supervisor may append `session_orphaned`, `attached`, `pane_closed`.
Readers must treat unknown event types as informational, not errors, so the
supervisor vocabulary can grow without breaking the emitter contract.

## Model-neutral behavior (honesty requirement)

**Every backend gets live observation.** Read-only attach works for any run
regardless of backend, because it only mirrors the tmux pane.

**Direct typing is explicit capability metadata, never inferred from the backend
name.** Whether keystrokes reach the agent is controlled solely by
`AGENT_CONSOLE_DIRECT_INPUT` (config `agent_console_direct_input`, default
`false`) and copied verbatim into `session_started.supports_direct_input`.

The bundled one-shot adapters — `omx`, `codex`, `claude` — consume their prompt
over stdin once and do not read an interactive TTY, so they run with direct
input `false`. But this is a property of *how the adapter is run*, not of its
name: any backend or custom harness that actually drives an interactive PTY can
opt in by setting `AGENT_CONSOLE_DIRECT_INPUT=true`. Deriving the capability
from the backend label would wrongly block future interactive OMX/Claude/Codex
harness modes, so the contract deliberately avoids it.

The UI/console must not be coupled to any single backend: it renders an
observe-only session unless the run's `supports_direct_input` field is `true`.
`session_started.supports_direct_input` carries the explicit metadata to
consumers so they never have to probe the backend.

## Host attach

Read-only (the safe default):

```bash
docker exec -it <container> \
  tmux -S /tmp/lao-console/<run_id>/tmux.sock attach -r -t lao-<run_id>
```

`console_schema.attach_command(container, run_id, read_only=True)` builds this
as an argv list (never a shell string). Dropping `-r` gives a writable attach.

## Security review — host/container tmux boundary

Trust direction: **the host reaches into the container using its own
`docker` CLI; the container never sees the host tmux server or the Docker
socket.** No new host→container privilege is granted by this feature.

- **No Docker socket in the container.** Unchanged: `/var/run/docker.sock` is
  never mounted. `docker exec` is a host-side capability; a host that can run it
  already has full control of the container, so attach does not widen the
  boundary.
- **Control socket is container-private.** `tmux.sock`, `argv`, and `exit_code`
  live under `/tmp/lao-console/<run_id>/` and are **not** bind-mounted. The host
  cannot obtain a writable tmux control socket via a shared filesystem; it must
  go through `docker exec`, which is already privileged and auditable.
- **Injection-safe identifiers.** `run_id` is validated `^[a-f0-9]{32}$`; the
  session name and all paths are derived from it, so issue content or backend
  output can never inject tmux targets or shell tokens. `attach_command` returns
  argv, not a shell string.
- **`-r` and `supports_direct_input` are an honest UI capability gate, NOT an
  authorization control.** A host user who can run `docker exec` is already
  trusted and can bypass everything here — they may attach read/write, drop
  `-r`, or `send-keys` directly regardless of `supports_direct_input`. The flag
  and the read-only default exist so a well-behaved console UI truthfully shows
  whether typing will reach the agent (false for one-shot adapters that don't
  read the PTY); they do not, and are not claimed to, reflexively refuse
  keystrokes from a trusted host operator. The trust boundary remains "who can
  reach the Docker/container", not the attach flags.
- **Session reaping is authoritative at the process level.** The core wrapper
  timeout is the authoritative deadline (surfaced as `session_killed reason=
  timeout`); on daemon crash the container process exit reaps the in-container
  tmux session, so no orphaned interactive session outlives the run.
- **Secret exposure surface (gap to enforce).** `lifecycle.jsonl` is designed to
  be secret-free: no argv, no env, no agent output — only ids, pids, paths,
  timestamps. `redact_event` is a defensive second pass. **However
  `output.log` is a raw capture of the agent's terminal and can contain
  whatever the agent prints (including secrets it was given).** It is
  bind-mounted host-readable. Treat `output.log` with the same sensitivity as
  agent artifacts: host-owned under `.runtime`, never published, and not subject
  to automatic redaction. `argv` is kept in the container-private run dir
  precisely because it may name secrets.
- **Runtime state trust level.** The console log root is orchestrator-owned
  runtime state under `.runtime`, never an issue-controlled path, consistent
  with the SQLite/artifacts trust boundary in
  [collaboration-ready-worker.md](collaboration-ready-worker.md).
- **Lifecycle metadata is not an authorization/audit boundary.** The YOLO
  backend runs as the same container UID and can rewrite its bind-mounted
  `lifecycle.jsonl` or `output.log`. Consumers freeze identity/capability from
  the first parsed `session_started` and verify live sessions through the
  container-private socket, but these files remain operational UI hints—not
  tamper-proof evidence against the agent itself. Strong isolation would
  require a separate backend UID/process boundary or an authenticated emitter.

## Emitter integration sketch

```python
from .console_schema import (
    build_session_started, build_session_ended, build_session_killed,
    redact_event, serialize_event, session_name, socket_path, output_log_path,
)

started = build_session_started(
    run_id=run_id,
    session=session_name(run_id),
    socket=socket_path(run_id),
    backend=config.runner.backend,
    backend_pid=pid,
    log_path=output_log_path(run_id, log_root=console_log_dir),  # from AGENT_CONSOLE_LOG_DIR
    supports_direct_input=direct_input,  # explicit, from AGENT_CONSOLE_DIRECT_INPUT
    issue_id=issue.id,
    issue_identifier=issue.identifier,
)
append(serialize_event(redact_event(started)) + "\n")   # O_APPEND, best-effort
```

Writing the lifecycle log is best-effort: a failure to append must be logged
and swallowed, never allowed to abort issue processing or leave a session
un-reaped.
