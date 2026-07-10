import json

import pytest

from linear_agent_orchestrator.console_schema import (
    CONSOLE_SCHEMA_VERSION,
    DEFAULT_CONSOLE_LOG_DIR,
    EMITTER_EVENTS,
    KILL_REASONS,
    ConsoleEvent,
    attach_command,
    build_session_ended,
    build_session_killed,
    build_session_started,
    console_enabled,
    direct_input_enabled,
    is_valid_run_id,
    lifecycle_jsonl_path,
    output_log_path,
    private_run_dir,
    redact_event,
    resolve_console_log_dir,
    serialize_event,
    session_name,
    socket_path,
    validate_event,
)


RUN_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _started(**overrides):
    kwargs = dict(
        run_id=RUN_ID,
        session=session_name(RUN_ID),
        socket=socket_path(RUN_ID),
        backend="omx",
        backend_pid=4321,
        log_path=output_log_path(RUN_ID),
        issue_id="issue-DEV-1",
        issue_identifier="DEV-1",
        ts="2026-07-10T12:40:00+00:00",
    )
    kwargs.update(overrides)
    return build_session_started(**kwargs)


def test_run_id_validation_and_derived_names_are_injection_safe():
    assert len(RUN_ID) == 32
    assert is_valid_run_id(RUN_ID)
    assert not is_valid_run_id("has.dot")
    assert not is_valid_run_id("has:colon")
    assert not is_valid_run_id(RUN_ID.upper())  # must be lowercase hex
    assert not is_valid_run_id(RUN_ID[:31])  # exactly 32 chars
    assert not is_valid_run_id("g" * 32)  # hex only
    session = session_name(RUN_ID)
    assert session == f"lao-{RUN_ID}"
    assert "." not in session and ":" not in session
    for bad in ("bad;id", "a b", "../etc"):
        with pytest.raises(ValueError):
            session_name(bad)


def test_path_layout_splits_private_and_host_readable():
    assert private_run_dir(RUN_ID) == f"/tmp/lao-console/{RUN_ID}"
    assert socket_path(RUN_ID) == f"/tmp/lao-console/{RUN_ID}/tmux.sock"
    assert lifecycle_jsonl_path(RUN_ID) == f"{DEFAULT_CONSOLE_LOG_DIR}/{RUN_ID}/lifecycle.jsonl"
    assert output_log_path(RUN_ID) == f"{DEFAULT_CONSOLE_LOG_DIR}/{RUN_ID}/output.log"
    # The container-private socket is never under the bind-mounted log root.
    assert not socket_path(RUN_ID).startswith(DEFAULT_CONSOLE_LOG_DIR)


def test_attach_command_is_argv_and_read_only_by_default():
    cmd = attach_command("lao-container", RUN_ID)
    assert cmd == [
        "docker", "exec", "-it", "lao-container",
        "tmux", "-S", socket_path(RUN_ID), "attach", "-r", "-t", session_name(RUN_ID),
    ]
    assert "-r" not in attach_command("lao-container", RUN_ID, read_only=False)


def test_session_started_carries_backend_and_direct_input_and_validates():
    event = _started()
    assert event["schema_version"] == CONSOLE_SCHEMA_VERSION
    assert event["backend"] == "omx"
    # Direct input defaults off (explicit metadata, not derived from backend).
    assert event["supports_direct_input"] is False
    assert validate_event(event) == []


def test_direct_input_is_explicit_metadata_not_backend_derived():
    # Any backend defaults observe-only unless direct input is explicitly set.
    for backend in ("omx", "codex", "claude", "some-future-harness"):
        assert _started(backend=backend)["supports_direct_input"] is False
    # Explicit opt-in works for ANY backend (no name coupling).
    assert _started(backend="omx", supports_direct_input=True)["supports_direct_input"] is True
    assert _started(backend="custom", supports_direct_input=True)["supports_direct_input"] is True


def test_direct_input_enabled_env_defaults_off():
    assert direct_input_enabled(None) is False
    assert direct_input_enabled("false") is False
    for on in ("true", "1", "yes", "on", "ENABLED"):
        assert direct_input_enabled(on) is True


def test_session_ended_and_killed_shapes_validate():
    ended = build_session_ended(run_id=RUN_ID, exit_code=0, ts="2026-07-10T12:41:00+00:00")
    assert ended["event"] == ConsoleEvent.SESSION_ENDED
    assert validate_event(ended) == []
    for reason in KILL_REASONS:
        killed = build_session_killed(run_id=RUN_ID, reason=reason, ts="2026-07-10T12:41:00+00:00")
        assert validate_event(killed) == []


def test_validate_event_flags_missing_fields_bad_reason_and_run_id():
    started = _started()
    del started["backend_pid"]
    assert any("missing required field: backend_pid" in e for e in validate_event(started))

    bad_reason = build_session_killed(run_id=RUN_ID, reason="nope", ts="2026-07-10T12:41:00+00:00")
    assert any("reason must be one of" in e for e in validate_event(bad_reason))

    bad_run = build_session_ended(run_id="Bad.Id", exit_code=0, ts="2026-07-10T12:41:00+00:00")
    assert any("run_id must match" in e for e in validate_event(bad_run))

    assert validate_event("nope") == ["event must be a JSON object"]
    assert any("unknown event type" in e for e in validate_event(
        {"schema_version": 1, "event": "bogus", "run_id": RUN_ID, "ts": "2026-07-10T12:41:00+00:00"}
    ))


def test_serialize_event_is_single_sorted_json_line():
    line = serialize_event(_started())
    assert "\n" not in line
    parsed = json.loads(line)
    assert list(parsed.keys()) == sorted(parsed.keys())
    assert parsed["event"] == "session_started"


def test_all_emitter_events_have_a_required_field_contract():
    from linear_agent_orchestrator.console_schema import REQUIRED_FIELDS

    for event_type in EMITTER_EVENTS:
        assert event_type in REQUIRED_FIELDS
        assert REQUIRED_FIELDS[event_type][0] == "event"


def test_redact_event_scrubs_secret_in_string_values():
    event = _started(log_path="/workspace/logs/console/x/output.log LINEAR_API_KEY=lin_api_secret")
    redacted = redact_event(event, environ={"LINEAR_API_KEY": "lin_api_secret"})
    assert "lin_api_secret" not in json.dumps(redacted)
    assert redacted["backend"] == "omx"  # non-secret fields preserved


def test_console_enabled_gate_defaults_off():
    assert console_enabled(None) is False
    assert console_enabled("false") is False
    assert console_enabled("0") is False
    for on in ("true", "1", "yes", "on", "ENABLED"):
        assert console_enabled(on) is True


def test_resolve_console_log_dir_default_and_trim():
    assert resolve_console_log_dir(None) == DEFAULT_CONSOLE_LOG_DIR
    assert resolve_console_log_dir("  ") == DEFAULT_CONSOLE_LOG_DIR
    assert resolve_console_log_dir("/workspace/logs/console/") == "/workspace/logs/console"
