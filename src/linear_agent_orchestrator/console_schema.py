"""Agent console lifecycle v1 — JSONL event contract and path layout.

This module is the *integration contract* for the interactive agent-console
feature. The core emitter (worker-1) writes one JSON object per line to a
per-run ``lifecycle.jsonl``; a host supervisor consumes it to attach to, and
reap, live agent tmux sessions.

It has no dependency on ``runner``/``daemon`` so both the emitter and any
consumer can import it without an import cycle. Contract owner: worker-4.

Trust/secret rules (see ``docs/agent-console.md`` for the full security review):
- ``lifecycle.jsonl`` and ``output.log`` are bind-mounted host-readable; they
  must never contain orchestrator secrets. ``argv`` and the tmux control socket
  stay in a container-private run dir that is NOT bind-mounted.
- ``redact_event`` is a defensive second pass; emitters still pass safe values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
import json
import os
import re


CONSOLE_SCHEMA_VERSION = 1

# Feature gate + log root (container paths). Compose bind-mounts the log root
# from ``.runtime/logs/console`` so per-run JSONL is durable and host-owned.
DEFAULT_CONSOLE_LOG_DIR = "/workspace/logs/console"

# Container-private run dir (NOT bind-mounted): holds the tmux control socket,
# argv, and exit_code. The host reaches these only via its own ``docker exec``.
PRIVATE_RUN_ROOT = "/tmp/lao-console"

SESSION_NAME_PREFIX = "lao-"

# run_id is a uuid4 hex string. One strict regex shared with the host scripts so
# the derived tmux session/target and file paths contain no shell/tmux
# metacharacters (no '.'/':' or path separators can appear).
RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")

# Truthy values for the AGENT_CONSOLE gate and the AGENT_CONSOLE_DIRECT_INPUT
# capability flag.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


class ConsoleEvent:
    """Canonical console lifecycle ``event`` names."""

    # Emitted by the core runner (worker-1).
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    SESSION_KILLED = "session_killed"
    # Reserved for the host supervisor; readers must tolerate these.
    SESSION_ORPHANED = "session_orphaned"
    ATTACHED = "attached"
    PANE_CLOSED = "pane_closed"


EMITTER_EVENTS = frozenset(
    {ConsoleEvent.SESSION_STARTED, ConsoleEvent.SESSION_ENDED, ConsoleEvent.SESSION_KILLED}
)
SUPERVISOR_EVENTS = frozenset(
    {ConsoleEvent.SESSION_ORPHANED, ConsoleEvent.ATTACHED, ConsoleEvent.PANE_CLOSED}
)
EVENT_TYPES = EMITTER_EVENTS | SUPERVISOR_EVENTS

# reason values for session_killed. ``timeout`` = run exceeded its deadline;
# ``teardown`` = daemon shutdown/graceful reap; ``start_failed`` = the session or
# backend process failed to start. These are the terminal kill semantics.
KILL_REASONS = frozenset({"timeout", "teardown", "start_failed"})

# Direct-input capability is EXPLICIT adapter/config metadata, never inferred
# from the backend label. Live observation (read-only attach) works for every
# backend; direct typing only works when an adapter actually reads the PTY. The
# bundled one-shot adapters (omx/codex/claude, invoked once with the prompt on
# stdin) default to false, but ANY backend or custom harness may opt in via
# AGENT_CONSOLE_DIRECT_INPUT when it runs an interactive PTY. Coupling the
# capability to a backend name would wrongly block future interactive modes, so
# it is carried as a plain boolean on session_started.


# Required fields per emitter event (v1 frozen contract for worker-1).
# ``backend`` (descriptive string) and ``supports_direct_input`` (explicit bool
# metadata) are carried on session_started so consumers can render observe-only
# vs. type-capable sessions without probing.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    ConsoleEvent.SESSION_STARTED: (
        "event",
        "run_id",
        "issue_id",
        "issue_identifier",
        "session",
        "socket",
        "backend",
        "backend_pid",
        "supports_direct_input",
        "log_path",
        "ts",
    ),
    ConsoleEvent.SESSION_ENDED: ("event", "run_id", "exit_code", "ts"),
    ConsoleEvent.SESSION_KILLED: ("event", "run_id", "reason", "ts"),
}

_SENSITIVE_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|API_KEY)", re.IGNORECASE)
_SENSITIVE_ASSIGN_RE = re.compile(
    r"(?i)([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|API_KEY)[A-Z0-9_]*\s*[=:]\s*)([^\s`'\";]+)"
)


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def utc_now_iso(clock: Any = None) -> str:
    """Return an ISO-8601 UTC timestamp, matching the JSON log formatter."""

    now = clock() if clock is not None else datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Identity / path layout (single source of truth for emitter + supervisor)
# --------------------------------------------------------------------------- #
def is_valid_run_id(run_id: Any) -> bool:
    return isinstance(run_id, str) and bool(RUN_ID_RE.match(run_id))


def session_name(run_id: str) -> str:
    """tmux session name for a run; never contains '.'/':' target delimiters."""

    if not is_valid_run_id(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return f"{SESSION_NAME_PREFIX}{run_id}"


def private_run_dir(run_id: str) -> str:
    """Container-private dir (NOT bind-mounted): tmux.sock, argv, exit_code."""

    if not is_valid_run_id(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return f"{PRIVATE_RUN_ROOT}/{run_id}"


def socket_path(run_id: str) -> str:
    return f"{private_run_dir(run_id)}/tmux.sock"


def run_log_dir(run_id: str, *, log_root: str = DEFAULT_CONSOLE_LOG_DIR) -> str:
    """Host-readable per-run dir: lifecycle.jsonl, output.log."""

    if not is_valid_run_id(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return f"{log_root.rstrip('/')}/{run_id}"


def lifecycle_jsonl_path(run_id: str, *, log_root: str = DEFAULT_CONSOLE_LOG_DIR) -> str:
    return f"{run_log_dir(run_id, log_root=log_root)}/lifecycle.jsonl"


def output_log_path(run_id: str, *, log_root: str = DEFAULT_CONSOLE_LOG_DIR) -> str:
    return f"{run_log_dir(run_id, log_root=log_root)}/output.log"


def attach_command(container: str, run_id: str, *, read_only: bool = True) -> list[str]:
    """Build the host-side read-only attach command (argv, not a shell string).

    Read-only (``-r``) is the safe default. Dropping it lets an operator send
    keystrokes into the live session; whether those reach the agent depends on
    the backend adapter (see the honesty note in docs/agent-console.md).
    """

    tmux = ["tmux", "-S", socket_path(run_id), "attach"]
    if read_only:
        tmux.append("-r")
    tmux += ["-t", session_name(run_id)]
    return ["docker", "exec", "-it", container, *tmux]


# --------------------------------------------------------------------------- #
# Event construction / serialization
# --------------------------------------------------------------------------- #
def build_session_started(
    *,
    run_id: str,
    session: str,
    socket: str,
    backend: str,
    backend_pid: int,
    log_path: str,
    supports_direct_input: bool = False,
    issue_id: str | None = None,
    issue_identifier: str | None = None,
    ts: str | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    return {
        "schema_version": CONSOLE_SCHEMA_VERSION,
        "event": ConsoleEvent.SESSION_STARTED,
        "run_id": run_id,
        "issue_id": issue_id,
        "issue_identifier": issue_identifier,
        "session": session,
        "socket": socket,
        "backend": backend,
        "backend_pid": backend_pid,
        "supports_direct_input": bool(supports_direct_input),
        "log_path": log_path,
        "ts": ts or utc_now_iso(clock),
    }


def build_session_ended(
    *, run_id: str, exit_code: int | None, ts: str | None = None, clock: Any = None
) -> dict[str, Any]:
    return {
        "schema_version": CONSOLE_SCHEMA_VERSION,
        "event": ConsoleEvent.SESSION_ENDED,
        "run_id": run_id,
        "exit_code": exit_code,
        "ts": ts or utc_now_iso(clock),
    }


def build_session_killed(
    *, run_id: str, reason: str, ts: str | None = None, clock: Any = None
) -> dict[str, Any]:
    return {
        "schema_version": CONSOLE_SCHEMA_VERSION,
        "event": ConsoleEvent.SESSION_KILLED,
        "run_id": run_id,
        "reason": reason,
        "ts": ts or utc_now_iso(clock),
    }


def serialize_event(event: Mapping[str, Any]) -> str:
    """Serialize to a single compact JSON line (no trailing newline).

    The emitter appends ``serialize_event(...) + "\\n"`` with ``O_APPEND``.
    """

    return json.dumps(event, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# Redaction (defensive second pass)
# --------------------------------------------------------------------------- #
def redact_text(value: str, *, environ: Mapping[str, str] | None = None) -> str:
    if not value:
        return value
    source = os.environ if environ is None else environ
    redacted = value
    for key, secret in source.items():
        if secret and _SENSITIVE_NAME_RE.search(key):
            redacted = redacted.replace(secret, "[REDACTED]")
    return _SENSITIVE_ASSIGN_RE.sub(r"\1[REDACTED]", redacted)


def redact_event(
    event: Mapping[str, Any], *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    return _redact_value(event, environ)  # type: ignore[return-value]


def _redact_value(value: Any, environ: Mapping[str, str] | None) -> Any:
    if isinstance(value, str):
        return redact_text(value, environ=environ)
    if isinstance(value, Mapping):
        return {key: _redact_value(item, environ) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, environ) for item in value]
    return value


# --------------------------------------------------------------------------- #
# Validation (adversarial checks / consumer safety)
# --------------------------------------------------------------------------- #
def validate_event(event: Any) -> list[str]:
    """Return human-readable schema violations for an emitter event.

    Supervisor-only event types are accepted as known but not shape-checked here.
    """

    errors: list[str] = []
    if not isinstance(event, Mapping):
        return ["event must be a JSON object"]

    version = event.get("schema_version")
    if version != CONSOLE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CONSOLE_SCHEMA_VERSION}, got {version!r}")

    event_type = event.get("event")
    if not isinstance(event_type, str) or not event_type:
        errors.append("event must be a non-empty string")
        return errors
    if event_type not in EVENT_TYPES:
        errors.append(f"unknown event type: {event_type!r}")

    run_id = event.get("run_id")
    if not is_valid_run_id(run_id):
        errors.append(f"run_id must match {RUN_ID_RE.pattern}, got {run_id!r}")

    ts = event.get("ts")
    if not isinstance(ts, str) or not ts:
        errors.append("ts must be a non-empty ISO-8601 string")
    else:
        try:
            datetime.fromisoformat(ts)
        except ValueError:
            errors.append(f"ts is not ISO-8601: {ts!r}")

    for required in REQUIRED_FIELDS.get(event_type, ()):  # emitter events only
        if required not in event:
            errors.append(f"{event_type} missing required field: {required}")

    if event_type == ConsoleEvent.SESSION_STARTED:
        pid = event.get("backend_pid")
        if pid is not None and not isinstance(pid, int):
            errors.append("backend_pid must be an integer")
        if "backend" in event and not isinstance(event.get("backend"), str):
            errors.append("backend must be a string")
        direct = event.get("supports_direct_input")
        if "supports_direct_input" in event and not isinstance(direct, bool):
            errors.append("supports_direct_input must be a boolean")
    if event_type == ConsoleEvent.SESSION_ENDED:
        exit_code = event.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            errors.append("exit_code must be an integer or null")
    if event_type == ConsoleEvent.SESSION_KILLED:
        reason = event.get("reason")
        if reason not in KILL_REASONS:
            errors.append(f"reason must be one of {sorted(KILL_REASONS)}, got {reason!r}")

    return errors


# --------------------------------------------------------------------------- #
# Config-surface helpers
# --------------------------------------------------------------------------- #
def console_enabled(raw: str | None) -> bool:
    """Resolve the AGENT_CONSOLE feature gate (default off)."""

    if raw is None:
        return False
    return raw.strip().casefold() in _TRUE_VALUES


def direct_input_enabled(raw: str | None) -> bool:
    """Resolve AGENT_CONSOLE_DIRECT_INPUT explicit capability metadata (default off).

    This is the single source of truth for whether the console may send
    keystrokes to the agent; it is never inferred from the backend name.
    """

    if raw is None:
        return False
    return raw.strip().casefold() in _TRUE_VALUES


def resolve_console_log_dir(raw: str | None) -> str:
    """Resolve AGENT_CONSOLE_LOG_DIR to a container path (default under logs)."""

    if raw is None or not raw.strip():
        return DEFAULT_CONSOLE_LOG_DIR
    return raw.strip().rstrip("/") or DEFAULT_CONSOLE_LOG_DIR
