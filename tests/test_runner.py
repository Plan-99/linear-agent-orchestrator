from pathlib import Path
import json
import shlex
import stat
import sys
import time

import pytest

from linear_agent_orchestrator.config import AppConfig, LinearConfig, RunnerConfig
from linear_agent_orchestrator.issue_schema import validate_issue_body
from linear_agent_orchestrator.linear_client import LinearComment, LinearIssue, WorkflowRole, WorkflowState
from linear_agent_orchestrator.runner import (
    RunnerError,
    RunnerPaths,
    RunnerResult,
    ShellRunner,
    StructuredResult,
    ValidationEvidence,
    _runner_result_to_state,
    _truncate_tail,
    execution_key,
    load_structured_result,
    process_issue_once,
    process_next_issue_once,
    redact_sensitive_text,
    render_command_template,
    render_context,
)
from linear_agent_orchestrator.state import StateStore


VALID_BODY = """## Task
Write an artifact.

## Acceptance Criteria
- Artifact exists

## Validation
- Inspect output
"""


class FakeLinear:
    def __init__(self):
        self.comments = []
        self.state_updates = []
        self.states = {
            WorkflowRole.QUEUE: WorkflowState("state-todo", "Todo", "unstarted"),
            WorkflowRole.IN_PROGRESS: WorkflowState("state-started", "In Progress", "started"),
            WorkflowRole.DONE: WorkflowState("state-done", "Done", "completed"),
        }

    def create_comment(self, issue_id, body):
        comment_id = f"comment-{len(self.comments) + 1}"
        self.comments.append({"id": comment_id, "issue_id": issue_id, "body": body})
        return comment_id

    def list_issue_comments(self, issue_id, *, first=50):
        return tuple(LinearComment(id=comment["id"], body=comment["body"]) for comment in self.comments if comment["issue_id"] == issue_id)

    def update_issue_state(self, issue_id, state_id):
        self.state_updates.append({"issue_id": issue_id, "state_id": state_id})
        return True

    def resolve_workflow_states(self, config):
        return self.states


class FlakyLinear(FakeLinear):
    def __init__(self, *, fail_start_comments=0, fail_result_comments=0, fail_failure_comments=0, fail_schema_comments=0, fail_done=0):
        super().__init__()
        self.fail_start_comments = fail_start_comments
        self.fail_result_comments = fail_result_comments
        self.fail_failure_comments = fail_failure_comments
        self.fail_schema_comments = fail_schema_comments
        self.fail_done = fail_done

    def create_comment(self, issue_id, body):
        if "Started processing" in body and self.fail_start_comments:
            self.fail_start_comments -= 1
            raise RuntimeError("transient start comment failure")
        if "Runner completed successfully" in body and self.fail_result_comments:
            self.fail_result_comments -= 1
            raise RuntimeError("transient comment failure")
        if "Runner failed" in body and self.fail_failure_comments:
            self.fail_failure_comments -= 1
            raise RuntimeError("transient failure comment failure")
        if "will not execute a runner" in body and self.fail_schema_comments:
            self.fail_schema_comments -= 1
            raise RuntimeError("transient schema comment failure")
        return super().create_comment(issue_id, body)

    def update_issue_state(self, issue_id, state_id):
        if state_id == self.states[WorkflowRole.DONE].id and self.fail_done:
            self.fail_done -= 1
            raise RuntimeError("transient done failure")
        return super().update_issue_state(issue_id, state_id)


class TransitionFailingLinear(FakeLinear):
    def __init__(self, *, fail_resolve=0, fail_in_progress=0):
        super().__init__()
        self.fail_resolve = fail_resolve
        self.fail_in_progress = fail_in_progress

    def resolve_workflow_states(self, config):
        if self.fail_resolve:
            self.fail_resolve -= 1
            raise RuntimeError("temporary workflow lookup failure")
        return super().resolve_workflow_states(config)

    def update_issue_state(self, issue_id, state_id):
        if state_id == self.states[WorkflowRole.IN_PROGRESS].id and self.fail_in_progress:
            self.fail_in_progress -= 1
            raise RuntimeError("temporary in-progress transition failure")
        return super().update_issue_state(issue_id, state_id)


def make_issue(body=VALID_BODY, identifier="DEV-1", updated_at="2026-07-09T14:00:00Z"):
    return LinearIssue(
        id=f"issue-{identifier}",
        identifier=identifier,
        title="Runner story",
        description=body,
        url="https://linear.app/test/issue/DEV-1",
        updated_at=updated_at,
        state_id="state-todo",
        state_name="Todo",
        project_id="project-test",
        project_name="TEST",
        schema=validate_issue_body(body),
    )


def issue_state(issue, *, state_id=None, state_name=None):
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "title": issue.title,
        "description": issue.description,
        "url": issue.url,
        "updated_at": issue.updated_at,
        "state_id": state_id if state_id is not None else issue.state_id,
        "state_name": state_name if state_name is not None else issue.state_name,
        "project_id": issue.project_id,
        "project_name": issue.project_name,
    }


def runner_config(tmp_path, command, timeout=5, allowed_env=()):
    return RunnerConfig(
        command_template=command,
        timeout_seconds=timeout,
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
        allowed_env=allowed_env,
    )


def app_config(tmp_path, command, timeout=5):
    return AppConfig(
        linear=LinearConfig(team_id="DEV", project_name="TEST"),
        runner=runner_config(tmp_path, command, timeout=timeout),
    )


def app_config_with_lease(tmp_path, command, *, ttl=5, renewal=1, timeout=5):
    config = app_config(tmp_path, command, timeout=timeout)
    return AppConfig(
        linear=config.linear,
        runner=config.runner,
        lease_ttl_seconds=ttl,
        lease_renewal_interval_seconds=renewal,
    )


# The runner rejects brace placeholders inside command tokens, so the payload
# is built with dict() to keep the inline script brace-free.
RESULT_WRITE_SNIPPET = (
    "import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(dict("
    'version=1, status="completed", summary="done", '
    'validation=[dict(description="check", command="true", exit_code=0, evidence="ok")])))'
)


def result_command(extra="", args=""):
    """Build a command that writes a valid structured result before optional extra statements."""

    script = RESULT_WRITE_SNIPPET + (f"; {extra}" if extra else "")
    command = f"{sys.executable} -c '{script}' {{result_path}}"
    return f"{command} {args}" if args else command


def test_shell_runner_materializes_prompt_context_and_artifacts(tmp_path):
    command = f"{sys.executable} -c 'import pathlib,sys; pathlib.Path(sys.argv[1], \"out.txt\").write_text(\"ok\"); print(pathlib.Path(sys.argv[2]).read_text().splitlines()[0])' {{artifacts_dir}} {{prompt_path}}"
    result = ShellRunner(runner_config(tmp_path, command)).run(make_issue())

    assert result.succeeded is True
    assert result.stdout.strip() == "# Linear Issue DEV-1"
    assert (result.paths.artifacts_dir / "out.txt").read_text() == "ok"
    assert result.paths.context_path.exists()
    assert stat.S_IMODE(result.paths.prompt_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(result.paths.inputs_dir.stat().st_mode) == 0o555


def test_shell_runner_reuses_same_issue_dir_after_read_only_inputs(tmp_path):
    command = f"{sys.executable} -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text().splitlines()[0])' {{prompt_path}}"
    runner = ShellRunner(runner_config(tmp_path, command))
    issue = make_issue(identifier="DEV-REUSE")

    first = runner.run(issue)
    assert first.succeeded is True
    assert first.stdout.strip() == "# Linear Issue DEV-REUSE"
    assert stat.S_IMODE(first.paths.prompt_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(first.paths.context_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(first.paths.inputs_dir.stat().st_mode) == 0o555

    second = runner.run(issue)

    assert second.succeeded is True
    assert second.stdout.strip() == "# Linear Issue DEV-REUSE"
    assert second.paths.issue_dir == first.paths.issue_dir
    assert stat.S_IMODE(second.paths.prompt_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(second.paths.context_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(second.paths.inputs_dir.stat().st_mode) == 0o555


def test_shell_runner_cleanup_unlinks_top_level_symlink_without_touching_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("safe")
    before_mode = stat.S_IMODE(outside.stat().st_mode)
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "DEV-LINK").symlink_to(outside, target_is_directory=True)
    command = f"{sys.executable} -c 'print(\"ok\")'"

    result = ShellRunner(runner_config(tmp_path, command)).run(make_issue(identifier="DEV-LINK"))

    assert result.succeeded is True
    assert marker.read_text() == "safe"
    assert stat.S_IMODE(outside.stat().st_mode) == before_mode
    assert (work_root / "DEV-LINK").is_dir()
    assert not (work_root / "DEV-LINK").is_symlink()


def test_runner_timeout_captures_timeout_result(tmp_path):
    command = f"{sys.executable} -c 'import time; time.sleep(2)'"

    result = ShellRunner(runner_config(tmp_path, command, timeout=1)).run(make_issue())

    assert result.succeeded is False
    assert result.timed_out is True
    assert result.exit_code is None


def test_runner_timeout_kills_descendant_process_group(tmp_path):
    marker = tmp_path / "descendant-survived.txt"
    parent_script = tmp_path / "spawn-descendant.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "child = \"import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text('survived')\"\n"
        "subprocess.Popen([sys.executable, '-c', child, sys.argv[1]])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    command = f"{sys.executable} {shlex.quote(str(parent_script))} {shlex.quote(str(marker))}"

    result = ShellRunner(runner_config(tmp_path, command, timeout=1)).run(
        make_issue(identifier="DEV-TIMEOUT-GROUP")
    )
    time.sleep(1.5)

    assert result.timed_out is True
    assert not marker.exists()


def test_runner_env_is_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setenv("LAO_ALLOWED", "yes")
    monkeypatch.setenv("LAO_BLOCKED", "no")
    command = f"{sys.executable} -c 'import os; print(os.environ.get(\"LAO_ALLOWED\", \"\") + \"/\" + os.environ.get(\"LAO_BLOCKED\", \"\"))'"

    result = ShellRunner(runner_config(tmp_path, command, allowed_env=("LAO_ALLOWED",))).run(make_issue())

    assert result.stdout.strip() == "yes/"


def test_result_comment_redacts_secret_like_env_values(tmp_path, monkeypatch):
    secret_value = "unit-test-sensitive-value"
    monkeypatch.setenv("LINEAR_API_KEY", secret_value)
    command = result_command('import os; print(os.environ["LINEAR_API_KEY"])')
    config = AppConfig(
        linear=LinearConfig(team_id="DEV", project_name="TEST"),
        runner=runner_config(tmp_path, command, allowed_env=("LINEAR_API_KEY",)),
    )
    fake = FakeLinear()

    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(issue=make_issue(identifier="DEV-SECRET"), config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")

    assert result.status == "completed"
    result_comments = [comment["body"] for comment in fake.comments if "Runner completed successfully" in comment["body"]]
    assert result_comments
    assert secret_value not in result_comments[0]
    assert "[REDACTED]" in result_comments[0]


def test_redaction_catches_non_environment_secret_assignments():
    text = 'API_TOKEN=source-secret {"refresh_token":"file-secret"}'
    redacted = redact_sensitive_text(text)
    assert "source-secret" not in redacted
    assert "file-secret" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_failure_output_truncation_keeps_tail_evidence():
    value = "start-marker\n" + ("x" * 5000) + "\nfinal-error-evidence"
    truncated = _truncate_tail(value, limit=100)
    assert truncated.startswith("... <truncated; showing tail>")
    assert "start-marker" not in truncated
    assert truncated.endswith("final-error-evidence")


def test_command_template_rejects_embedded_placeholder_in_shell_fragment():
    with pytest.raises(RunnerError):
        render_command_template("echo prefix-{issue_identifier}", {"issue_identifier": "DEV-1"})


def test_no_shell_injection_from_issue_identifier_placeholder(tmp_path):
    issue = make_issue(identifier="DEV-1; touch injected")
    command = f"{sys.executable} -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])' /tmp/lao-injection-check {{issue_identifier}}"

    result = ShellRunner(runner_config(tmp_path, command)).run(issue)

    assert result.succeeded is True
    assert Path("/tmp/lao-injection-check").read_text() == "DEV-1; touch injected"
    assert not Path("injected").exists()
    Path("/tmp/lao-injection-check").unlink(missing_ok=True)


def test_process_success_comments_and_moves_done(tmp_path):
    command = result_command('pathlib.Path(sys.argv[2], "evidence.txt").write_text("evidence"); print("done")', "{artifacts_dir}")
    config = app_config(tmp_path, command)
    fake = FakeLinear()
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(
            issue=make_issue(),
            config=config,
            state=store,
            linear=fake,
            runner=ShellRunner(config.runner),
            owner_id="owner-1",
        )
        claim = store.get_claim("issue-DEV-1")

    assert result.status == "completed"
    assert result.executed is True
    assert result.done_transitioned is True
    assert [update["state_id"] for update in fake.state_updates] == ["state-started", "state-done"]
    assert any("Runner completed successfully" in comment["body"] for comment in fake.comments)
    assert claim.status == "completed"


def test_process_renews_lease_during_long_runner_and_blocks_reclaim(tmp_path):
    command = result_command('import time; time.sleep(1.25); print("done")')
    config = app_config_with_lease(tmp_path, command, ttl=1, renewal=0.1, timeout=3)
    fake = FakeLinear()
    issue = make_issue(identifier="DEV-LONG")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        result = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        history = [row["event"] for row in store.history_for_issue(issue.id)]

    assert result.status == "completed"
    assert "claim_renewed" in history
    with StateStore(db) as reopened:
        after_terminal = reopened.acquire_claim(issue.id, "owner-2", lease_ttl_seconds=1)
    assert after_terminal.acquired is False


def test_process_comment_failure_persists_success_and_retry_finalizes_without_rerun(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else "1"); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-COMMENT-RETRY")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_issue_once(issue=issue, config=config, state=store, linear=FlakyLinear(fail_result_comments=1), runner=ShellRunner(config.runner), owner_id="owner-1")

    assert first.status == "finalization_pending"
    assert counter.read_text() == "1"

    fake_retry = FakeLinear()
    with StateStore(db) as store:
        second = process_issue_once(issue=issue, config=config, state=store, linear=fake_retry, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert second.status == "completed"
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert len([c for c in fake_retry.comments if "Runner completed successfully" in c["body"]]) == 1
    assert [update["state_id"] for update in fake_retry.state_updates] == ["state-done"]


def test_process_done_failure_retries_done_without_duplicate_result_comment_or_rerun(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else "1"); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-DONE-RETRY")
    db = tmp_path / "state.sqlite3"
    flaky = FlakyLinear(fail_done=1)

    with StateStore(db) as store:
        first = process_issue_once(issue=issue, config=config, state=store, linear=flaky, runner=ShellRunner(config.runner), owner_id="owner-1")

    assert first.status == "finalization_pending"
    assert counter.read_text() == "1"
    assert len([c for c in flaky.comments if "Runner completed successfully" in c["body"]]) == 1

    retry = FakeLinear()
    with StateStore(db) as store:
        second = process_issue_once(issue=issue, config=config, state=store, linear=retry, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert second.status == "completed"
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert not any("Runner completed successfully" in c["body"] for c in retry.comments)
    assert [update["state_id"] for update in retry.state_updates] == ["state-done"]


def test_process_next_issue_once_recovers_pending_finalization_without_todo_or_rerun(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else "1"); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-PENDING-SWEEP")
    db = tmp_path / "state.sqlite3"
    first_linear = FlakyLinear(fail_done=1)

    with StateStore(db) as store:
        first = process_issue_once(issue=issue, config=config, state=store, linear=first_linear, runner=ShellRunner(config.runner), owner_id="owner-1")

    assert first.status == "finalization_pending"
    assert counter.read_text() == "1"
    assert [update["state_id"] for update in first_linear.state_updates] == ["state-started"]
    assert len([c for c in first_linear.comments if "Runner completed successfully" in c["body"]]) == 1

    recovery_linear = FakePollingLinear([])
    with StateStore(db) as store:
        recovered = process_next_issue_once(config=config, state=store, linear=recovery_linear, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert recovered.status == "completed"
    assert recovered.issue_identifier == "DEV-PENDING-SWEEP"
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert recovery_linear.comments == []
    assert [update["state_id"] for update in recovery_linear.state_updates] == ["state-done"]


def test_process_runner_failure_comments_without_done(tmp_path):
    command = f"{sys.executable} -c 'import sys; print(\"bad\", file=sys.stderr); raise SystemExit(7)'"
    config = app_config(tmp_path, command)
    fake = FakeLinear()
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(issue=make_issue(), config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim("issue-DEV-1")

    assert result.status == "failed"
    assert result.done_transitioned is False
    assert [update["state_id"] for update in fake.state_updates] == ["state-started"]
    assert any("Runner failed" in comment["body"] for comment in fake.comments)
    assert claim.status == "failed"
    assert "code 7" in claim.last_error


def test_process_timeout_comments_without_done(tmp_path):
    command = f"{sys.executable} -c 'import time; time.sleep(2)'"
    config = app_config(tmp_path, command, timeout=1)
    fake = FakeLinear()
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(issue=make_issue(), config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim("issue-DEV-1")

    assert result.status == "failed"
    assert result.runner_result.timed_out is True
    assert [update["state_id"] for update in fake.state_updates] == ["state-started"]
    assert any("Runner timed out" in comment["body"] for comment in fake.comments)
    assert "timed out" in claim.last_error


def test_nonzero_runner_failure_comment_failure_returns_failed_without_done_or_raise(tmp_path):
    command = f"{sys.executable} -c 'import sys; print(\"bad\", file=sys.stderr); raise SystemExit(7)'"
    config = app_config(tmp_path, command)
    fake = FlakyLinear(fail_failure_comments=1)
    issue = make_issue(identifier="DEV-FAIL-COMMENT")
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert result.status == "failed"
    assert result.executed is True
    assert result.done_transitioned is False
    assert result.comment_ids[-1] is None
    assert any("Started processing" in comment["body"] for comment in fake.comments)
    assert not any("Runner failed" in comment["body"] for comment in fake.comments)
    assert "transient failure comment failure" in result.message
    assert claim.status == "failed"
    assert [update["state_id"] for update in fake.state_updates] == ["state-started"]
    assert not any(update["state_id"] == "state-done" for update in fake.state_updates)


def test_process_schema_missing_comments_without_claim_or_execution(tmp_path):
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    fake = FakeLinear()
    invalid_issue = make_issue("## Task\nOnly task")
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(issue=invalid_issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(invalid_issue.id)

    assert result.status == "schema_invalid"
    assert result.claimed is True
    assert result.executed is False
    assert claim.status == "released"
    assert fake.state_updates == []
    assert len(fake.comments) == 1
    assert "Missing required section" in fake.comments[0]["body"]


def test_process_dedupe_prevents_second_execution_and_comments(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else "1")', str(counter))
    config = app_config(tmp_path, command)
    fake = FakeLinear()
    issue = make_issue()
    with StateStore(tmp_path / "state.sqlite3") as store:
        first = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        second = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")

    assert first.status == "completed"
    assert second.status == "not_claimed"
    assert counter.read_text() == "1"
    assert len([c for c in fake.comments if "Runner completed successfully" in c["body"]]) == 1


@pytest.mark.parametrize(
    ("linear_kwargs", "expected_error", "expected_active"),
    [
        ({"fail_resolve": 1}, "temporary workflow lookup failure", 0),
        ({"fail_in_progress": 1}, "temporary in-progress transition failure", 1),
    ],
)
def test_in_progress_transition_failure_returns_pending_without_execution(tmp_path, caplog, linear_kwargs, expected_error, expected_active):
    counter = tmp_path / "counter.txt"
    config = app_config(tmp_path, result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter)))
    issue = make_issue(identifier="DEV-INPROG-PENDING")
    exec_key = execution_key(issue, config.runner)
    fake = TransitionFailingLinear(**linear_kwargs)

    with StateStore(tmp_path / "state.sqlite3") as store:
        with caplog.at_level("WARNING", logger="linear_agent_orchestrator.runner"):
            result = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)
        active = store.list_active_work()
        has_execution = store.has_execution(issue.id, exec_key)

    assert result.status == "in_progress_transition_pending"
    assert result.claimed is True
    assert result.executed is False
    assert result.done_transitioned is False
    assert result.issue_url == issue.url
    assert "Runner was not started" in result.message
    assert expected_error in result.message
    assert claim.active is True
    assert claim.execution_key == exec_key
    assert len(active) == expected_active
    if expected_active:
        assert active[0]["execution_key"] == exec_key
    assert has_execution is False
    assert not counter.exists()
    assert not any("Started processing" in comment["body"] for comment in fake.comments)
    assert not any("Runner completed successfully" in comment["body"] for comment in fake.comments)
    assert any(
        record.message == "in_progress_transition_pending"
        and record.issue_id == issue.id
        and record.issue_identifier == issue.identifier
        and expected_error in record.error
        for record in caplog.records
    )


def test_active_work_retry_ensures_in_progress_then_executes_once(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-INPROG-RETRY")
    db = tmp_path / "state.sqlite3"
    flaky = TransitionFailingLinear(fail_in_progress=1)

    with StateStore(db) as store:
        first = process_issue_once(issue=issue, config=config, state=store, linear=flaky, runner=ShellRunner(config.runner), owner_id="owner-1")
        assert first.status == "in_progress_transition_pending"
        assert not counter.exists()

    recovery = FakePollingLinear([])
    with StateStore(db) as store:
        second = process_next_issue_once(config=config, state=store, linear=recovery, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert second.status == "completed"
    assert second.executed is True
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert [update["state_id"] for update in recovery.state_updates] == ["state-started", "state-done"]

class FakePollingLinear(FakeLinear):
    def __init__(self, issues):
        super().__init__()
        self.issues = tuple(issues)

    def iter_todo_issues(self, config, first=10):
        self.first = first
        return self.issues


class InterruptingRunner:
    def preflight_workspace(self):
        return []

    def run(self, issue):
        raise KeyboardInterrupt("simulated interruption")


def test_start_comment_failure_and_interruption_recover_from_active_work_without_todo(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('pathlib.Path(sys.argv[2]).write_text("1"); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-ACTIVE-START")
    db = tmp_path / "state.sqlite3"
    first_linear = FlakyLinear(fail_start_comments=1)

    with StateStore(db) as store:
        with pytest.raises(KeyboardInterrupt):
            process_issue_once(issue=issue, config=config, state=store, linear=first_linear, runner=InterruptingRunner(), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert claim.active is True
    assert [update["state_id"] for update in first_linear.state_updates] == ["state-started"]
    assert first_linear.comments == []

    recovery_linear = FakePollingLinear([])
    with StateStore(db) as store:
        recovered = process_next_issue_once(config=config, state=store, linear=recovery_linear, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert recovered.status == "failed_in_doubt"
    assert recovered.executed is False
    assert not counter.exists()
    assert claim.status == "failed"
    assert "execution intent already exists" in recovered.message
    assert recovery_linear.state_updates == []


class CrashOnSecondActiveWorkStore(StateStore):
    def __init__(self, path):
        super().__init__(path)
        self.upsert_calls = 0

    def upsert_active_work(self, issue_id, execution_key, *, issue, owner_id=None, now=None):
        self.upsert_calls += 1
        if self.upsert_calls == 2:
            assert issue["state_name"] == "In Progress"
            raise KeyboardInterrupt("simulated operator interruption before persisted In Progress snapshot")
        return super().upsert_active_work(issue_id, execution_key, issue=issue, owner_id=owner_id, now=now)


class CountingShellRunner:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.calls = 0

    def preflight_workspace(self):
        return self.wrapped.preflight_workspace()

    def run(self, issue):
        self.calls += 1
        return self.wrapped.run(issue)


def test_in_progress_update_crash_before_second_active_snapshot_recovers_and_executes_once(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-INPROG-CRASH")
    exec_key = execution_key(issue, config.runner)
    db = tmp_path / "state.sqlite3"
    first_linear = FakeLinear()
    interrupted_runner = CountingShellRunner(ShellRunner(config.runner))

    with CrashOnSecondActiveWorkStore(db) as store:
        with pytest.raises(KeyboardInterrupt, match="simulated operator interruption"):
            process_issue_once(
                issue=issue,
                config=config,
                state=store,
                linear=first_linear,
                runner=interrupted_runner,
                owner_id="owner-1",
            )
        claim = store.get_claim(issue.id)
        active = store.list_active_work()
        has_execution = store.has_execution(issue.id, exec_key)

    assert [update["state_id"] for update in first_linear.state_updates] == ["state-started"]
    assert first_linear.comments == []
    assert interrupted_runner.calls == 0
    assert not counter.exists()
    assert has_execution is False
    assert claim.active is True
    assert claim.execution_key == exec_key
    assert claim.attempt_count == 1
    assert len(active) == 1
    assert active[0]["execution_key"] == exec_key
    assert active[0]["issue"]["state_name"] == "Todo"

    recovery_linear = FakePollingLinear([])
    recovery_runner = CountingShellRunner(ShellRunner(config.runner))
    with StateStore(db) as store:
        recovered = process_next_issue_once(
            config=config,
            state=store,
            linear=recovery_linear,
            runner=recovery_runner,
            owner_id="owner-1",
        )
        recovered_claim = store.get_claim(issue.id)
        recovered_has_execution = store.has_execution(issue.id, exec_key)

    assert recovered.status == "completed"
    assert recovered.executed is True
    assert recovered.done_transitioned is True
    assert recovery_runner.calls == 1
    assert counter.read_text() == "1"
    assert [update["state_id"] for update in recovery_linear.state_updates] == ["state-started", "state-done"]
    assert len([c for c in recovery_linear.comments if "Started processing" in c["body"]]) == 1
    assert len([c for c in recovery_linear.comments if "Runner completed successfully" in c["body"]]) == 1
    assert recovered_claim.status == "completed"
    assert recovered_claim.execution_key == exec_key
    assert recovered_claim.attempt_count == 1
    assert recovered_has_execution is True


def test_active_work_persisted_before_finalization_recovers_without_todo(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('pathlib.Path(sys.argv[2]).write_text("1"); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-ACTIVE-CRASH")
    exec_key = execution_key(issue, config.runner)
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        store.acquire_claim(issue.id, "owner-1", lease_ttl_seconds=config.lease_ttl_seconds, execution_key=exec_key)
        store.upsert_active_work(
            issue.id,
            exec_key,
            issue=issue_state(issue, state_id="state-started", state_name="In Progress"),
            owner_id="owner-1",
        )

    recovery_linear = FakePollingLinear([])
    with StateStore(db) as store:
        recovered = process_next_issue_once(config=config, state=store, linear=recovery_linear, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert recovered.status == "completed"
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert [update["state_id"] for update in recovery_linear.state_updates] == ["state-done"]


def test_result_comment_dedupe_without_finalization_mark_does_not_duplicate_comment(tmp_path):
    command = result_command('print("done")')
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-COMMENT-CRASH")
    exec_key = execution_key(issue, config.runner)
    result = ShellRunner(config.runner).run(issue)
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        store.acquire_claim(issue.id, "owner-1", lease_ttl_seconds=config.lease_ttl_seconds, execution_key=exec_key)
        store.upsert_active_work(issue.id, exec_key, issue=issue_state(issue), owner_id="owner-1")
        store.upsert_finalization(
            issue.id,
            exec_key,
            result=_runner_result_to_state(result),
            issue=issue_state(issue),
            owner_id="owner-1",
        )
        store.record_comment_once(issue.id, f"result:{exec_key}", comment_id="comment-existing", owner_id="owner-1")

    recovery_linear = FakePollingLinear([])
    with StateStore(db) as store:
        recovered = process_next_issue_once(config=config, state=store, linear=recovery_linear, runner=ShellRunner(config.runner), owner_id="owner-1")
        finalization = store.get_finalization(issue.id, exec_key)

    assert recovered.status == "completed"
    assert recovery_linear.comments == []
    assert [update["state_id"] for update in recovery_linear.state_updates] == ["state-done"]
    assert finalization["result_comment_id"] == "comment-existing"


def test_schema_comment_failure_returns_schema_invalid_and_later_valid_runs(tmp_path):
    invalid = make_issue("## Task\nOnly task", identifier="DEV-BAD-COMMENT")
    valid = make_issue(identifier="DEV-GOOD-AFTER-BAD-COMMENT")
    counter = tmp_path / "counter.txt"
    config = app_config(tmp_path, result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter)))
    fake = FlakyLinear(fail_schema_comments=1)
    fake.issues = (invalid, valid)
    fake.iter_todo_issues = lambda config, first=10: fake.issues

    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_next_issue_once(config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner", first=3)
        invalid_claim = store.get_claim(invalid.id)

    assert result.status == "completed"
    assert result.issue_identifier == "DEV-GOOD-AFTER-BAD-COMMENT"
    assert counter.read_text() == "ran"
    assert invalid_claim.status == "released"
    assert not any("Missing required section" in c["body"] for c in fake.comments)
    assert [update["state_id"] for update in fake.state_updates] == ["state-started", "state-done"]


def test_schema_clarification_lease_blocks_overlap_then_retry_posts_once(tmp_path):
    invalid = make_issue("## Task\nOnly task", identifier="DEV-BAD-LEASE")
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    fake = FakeLinear()
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        store.acquire_claim(invalid.id, "owner-1", lease_ttl_seconds=config.lease_ttl_seconds, execution_key="schema:schema-missing")
        overlapped = process_issue_once(issue=invalid, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-2")

    assert overlapped.status == "not_claimed"
    assert fake.comments == []

    with StateStore(db) as store:
        store.release_claim(invalid.id, "owner-1")
        retried = process_issue_once(issue=invalid, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-2")
        claim = store.get_claim(invalid.id)

    assert retried.status == "schema_invalid"
    assert claim.status == "released"
    assert len([c for c in fake.comments if "Missing required section" in c["body"]]) == 1


def test_process_next_issue_once_handles_malformed_issue_before_valid(tmp_path):
    invalid = make_issue("## Task\nOnly task", identifier="DEV-BAD")
    valid = make_issue(identifier="DEV-GOOD")
    counter = tmp_path / "counter.txt"
    config = app_config(tmp_path, result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter)))
    fake = FakePollingLinear([invalid, valid])
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_next_issue_once(config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner", first=3)

    assert result.status == "completed"
    assert result.issue_identifier == "DEV-GOOD"
    assert result.executed is True
    assert counter.read_text() == "ran"
    assert fake.first == 3
    assert len(fake.comments) == 3  # invalid clarification, valid start, valid result
    assert "Missing required section" in fake.comments[0]["body"]
    assert [update["state_id"] for update in fake.state_updates] == ["state-started", "state-done"]


def test_process_next_issue_once_all_malformed_returns_no_executable_candidate(tmp_path):
    invalid = make_issue("## Task\nOnly task", identifier="DEV-BAD")
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    fake = FakePollingLinear([invalid])
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_next_issue_once(config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner", first=3)

    assert result.status == "no_executable_candidate"
    assert result.issue_identifier == "DEV-BAD"
    assert result.executed is False
    assert len(fake.comments) == 1


def test_process_next_issue_once_restart_idempotency_skips_terminal_claim(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else "1")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-RESTART")
    db = tmp_path / "state.sqlite3"
    first_fake = FakePollingLinear([issue])
    with StateStore(db) as store:
        first = process_next_issue_once(config=config, state=store, linear=first_fake, runner=ShellRunner(config.runner), owner_id="owner")

    second_fake = FakePollingLinear([issue])
    with StateStore(db) as reopened:
        second = process_next_issue_once(config=config, state=reopened, linear=second_fake, runner=ShellRunner(config.runner), owner_id="owner")

    assert first.status == "completed"
    assert second.status == "no_candidate"
    assert counter.read_text() == "1"
    assert second_fake.comments == []
    assert second_fake.state_updates == []

class BrokenRunner:
    def preflight_workspace(self):
        return []

    def run(self, issue):
        raise PermissionError("cannot create /workspace/work/DEV-TEST")


def test_process_runner_materialization_exception_fails_and_comments_without_done(tmp_path):
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    fake = FakeLinear()
    issue = make_issue(identifier="DEV-PERM")
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=BrokenRunner(), owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert result.status == "failed"
    assert result.claimed is True
    assert result.executed is False
    assert result.done_transitioned is False
    assert [update["state_id"] for update in fake.state_updates] == ["state-started"]
    assert claim.status == "failed"
    assert "PermissionError" in claim.last_error
    assert any("Runner setup or execution failed" in comment["body"] for comment in fake.comments)
    assert not any(update["state_id"] == "state-done" for update in fake.state_updates)

class CrashAfterCreateLinear(FakePollingLinear):
    def __init__(self, issues, *, crash_on):
        super().__init__(issues)
        self.crash_on = crash_on
        self.crashed = False

    def create_comment(self, issue_id, body):
        comment_id = f"comment-{len(self.comments) + 1}"
        self.comments.append({"id": comment_id, "issue_id": issue_id, "body": body})
        if self.crash_on in body and not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated crash after Linear comment creation")
        return comment_id


def test_result_comment_reconciles_crash_after_create_without_duplicate_or_rerun(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else "1"); print("done")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-RESULT-CRASH")
    fake = CrashAfterCreateLinear([issue], crash_on="Runner completed successfully")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_next_issue_once(config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner")

    assert first.status == "finalization_pending"
    assert counter.read_text() == "1"
    assert len([c for c in fake.comments if "Runner completed successfully" in c["body"]]) == 1

    with StateStore(db) as store:
        second = process_next_issue_once(config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner")
        claim = store.get_claim(issue.id)

    assert second.status == "completed"
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert len([c for c in fake.comments if "Runner completed successfully" in c["body"]]) == 1
    assert [update["state_id"] for update in fake.state_updates] == ["state-started", "state-done"]


def test_schema_clarification_reconciles_crash_after_create_then_processes_valid_issue(tmp_path):
    invalid = make_issue("## Task\nOnly task", identifier="DEV-SCHEMA-CRASH")
    valid = make_issue(identifier="DEV-AFTER-SCHEMA")
    counter = tmp_path / "counter.txt"
    config = app_config(tmp_path, result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter)))
    fake = CrashAfterCreateLinear([invalid, valid], crash_on="will not execute a runner")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_next_issue_once(config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner")

    assert first.status == "completed"
    assert first.issue_identifier == "DEV-AFTER-SCHEMA"
    assert len([c for c in fake.comments if "Missing required section" in c["body"]]) == 1

    with StateStore(db) as store:
        result = process_next_issue_once(config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner")

    assert result.status == "no_candidate"
    assert counter.read_text() == "ran"
    assert len([c for c in fake.comments if "Missing required section" in c["body"]]) == 1
    assert len([c for c in fake.comments if "Runner completed successfully" in c["body"]]) == 1


def test_process_next_issue_once_retries_failed_issue_after_updated_at_changes_and_completes(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command(
        "p=pathlib.Path(sys.argv[2]); "
        "n=int(p.read_text()) if p.exists() else 0; "
        "p.write_text(str(n+1)); "
        "raise SystemExit(3 if n == 0 else 0)",
        str(counter),
    )
    config = app_config(tmp_path, command)
    original = make_issue(identifier="DEV-RETRY", updated_at="2026-07-09T14:00:00Z")
    updated = make_issue(identifier="DEV-RETRY", updated_at="2026-07-09T15:00:00Z")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([original]), runner=ShellRunner(config.runner), owner_id="owner")
        failed_claim = store.get_claim(original.id)

    with StateStore(db) as store:
        second = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([updated]), runner=ShellRunner(config.runner), owner_id="owner")
        completed_claim = store.get_claim(updated.id)

    assert first.status == "failed"
    assert failed_claim.status == "failed"
    assert second.status == "completed"
    assert counter.read_text() == "2"
    assert completed_claim.status == "completed"
    assert completed_claim.attempt_count == 2
    assert completed_claim.last_error is None


def test_process_next_issue_once_same_failed_execution_key_no_candidate_with_reason(tmp_path):
    counter = tmp_path / "counter.txt"
    command = f"{sys.executable} -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); raise SystemExit(7)' {counter}"
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-FAILED-SAME", updated_at="2026-07-09T14:00:00Z")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([issue]), runner=ShellRunner(config.runner), owner_id="owner")
    with StateStore(db) as store:
        second = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([issue]), runner=ShellRunner(config.runner), owner_id="owner")
        claim = store.get_claim(issue.id)

    assert first.status == "failed"
    assert second.status == "no_candidate"
    assert "failed_same_execution_key=1" in second.message
    assert counter.read_text() == "1"
    assert claim.status == "failed"
    assert claim.attempt_count == 1


def test_process_next_issue_once_completed_stays_skipped_despite_changed_execution_key(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str((int(p.read_text()) if p.exists() else 0)+1))', str(counter))
    config = app_config(tmp_path, command)
    original = make_issue(identifier="DEV-COMPLETED-CHANGED", updated_at="2026-07-09T14:00:00Z")
    updated = make_issue(identifier="DEV-COMPLETED-CHANGED", updated_at="2026-07-09T15:00:00Z")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([original]), runner=ShellRunner(config.runner), owner_id="owner")
    with StateStore(db) as store:
        second = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([updated]), runner=ShellRunner(config.runner), owner_id="owner")
        claim = store.get_claim(updated.id)

    assert first.status == "completed"
    assert second.status == "no_candidate"
    assert "completed=1" in second.message
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert claim.attempt_count == 1


class CountingRunner:
    def __init__(self):
        self.calls = 0

    def preflight_workspace(self):
        return []

    def run(self, issue):
        self.calls += 1
        raise AssertionError("runner must not be invoked")


def test_pre_recorded_execution_without_finalization_fails_closed_without_rerun(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-IN-DOUBT")
    exec_key = execution_key(issue, config.runner)
    fake = FakeLinear()
    runner = CountingRunner()

    with StateStore(tmp_path / "state.sqlite3") as store:
        store.acquire_claim(issue.id, "owner-1", lease_ttl_seconds=config.lease_ttl_seconds, execution_key=exec_key)
        store.record_execution_once(issue.id, exec_key, owner_id="owner-1")
        result = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=runner, owner_id="owner-1")
        claim = store.get_claim(issue.id)

    assert result.status == "failed_in_doubt"
    assert result.executed is False
    assert result.done_transitioned is False
    assert runner.calls == 0
    assert not counter.exists()
    assert claim.status == "failed"
    assert claim.execution_key == exec_key
    assert "execution intent already exists" in result.message
    assert fake.state_updates == []
    assert any("prior execution state is in doubt" in comment["body"] for comment in fake.comments)


def test_active_work_config_drift_fails_closed_without_execution_then_fresh_key_retries(tmp_path):
    counter = tmp_path / "counter.txt"
    old_config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    new_config = app_config(tmp_path, result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter)))
    issue = make_issue(identifier="DEV-DRIFT")
    stored_key = execution_key(issue, old_config.runner)
    current_key = execution_key(issue, new_config.runner)
    assert stored_key != current_key
    db = tmp_path / "state.sqlite3"
    no_run = CountingRunner()

    with StateStore(db) as store:
        store.acquire_claim(issue.id, "owner-1", lease_ttl_seconds=old_config.lease_ttl_seconds, execution_key=stored_key)
        store.upsert_active_work(
            issue.id,
            stored_key,
            issue=issue_state(issue, state_id="state-started", state_name="In Progress"),
            owner_id="owner-1",
        )
        drifted = process_next_issue_once(config=new_config, state=store, linear=FakePollingLinear([]), runner=no_run, owner_id="owner-1")
        drift_claim = store.get_claim(issue.id)

    assert drifted.status == "failed_in_doubt"
    assert drifted.executed is False
    assert no_run.calls == 0
    assert not counter.exists()
    assert drift_claim.status == "failed"
    assert drift_claim.execution_key == stored_key
    assert f"stored execution_key={stored_key}" in drifted.message
    assert f"current execution_key={current_key}" in drifted.message

    with StateStore(db) as store:
        retried = process_next_issue_once(config=new_config, state=store, linear=FakePollingLinear([issue]), runner=ShellRunner(new_config.runner), owner_id="owner-1")
        retry_claim = store.get_claim(issue.id)

    assert retried.status == "completed"
    assert counter.read_text() == "ran"
    assert retry_claim.status == "completed"
    assert retry_claim.execution_key == current_key
    assert retry_claim.attempt_count == 2


def test_active_work_config_drift_preserves_competing_owner_lease(tmp_path):
    old_config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    new_config = app_config(tmp_path, f"{sys.executable} -c 'print(\"new\")'")
    issue = make_issue(identifier="DEV-DRIFT-LEASE")
    stored_key = execution_key(issue, old_config.runner)
    no_run = CountingRunner()

    with StateStore(tmp_path / "state.sqlite3") as store:
        store.acquire_claim(issue.id, "owner-1", lease_ttl_seconds=old_config.lease_ttl_seconds, execution_key=stored_key)
        store.upsert_active_work(
            issue.id,
            stored_key,
            issue=issue_state(issue, state_id="state-started", state_name="In Progress"),
            owner_id="owner-1",
        )
        result = process_next_issue_once(config=new_config, state=store, linear=FakePollingLinear([]), runner=no_run, owner_id="owner-2")
        claim = store.get_claim(issue.id)

    assert result.status == "not_claimed"
    assert result.message == "active_lease:claimed"
    assert no_run.calls == 0
    assert claim.status == "claimed"
    assert claim.owner_id == "owner-1"
    assert claim.execution_key == stored_key


def test_process_next_issue_once_retries_failed_issue_after_description_changes_and_completes(tmp_path):
    counter = tmp_path / "counter.txt"
    command = result_command(
        "p=pathlib.Path(sys.argv[2]); "
        "n=int(p.read_text()) if p.exists() else 0; "
        "p.write_text(str(n+1)); "
        "raise SystemExit(3 if n == 0 else 0)",
        str(counter),
    )
    config = app_config(tmp_path, command)
    original = make_issue(identifier="DEV-RETRY-DESC", updated_at="2026-07-09T14:00:00Z")
    changed = make_issue(body=VALID_BODY + "\nAdditional detail.\n", identifier="DEV-RETRY-DESC", updated_at="2026-07-09T14:00:00Z")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([original]), runner=ShellRunner(config.runner), owner_id="owner")
    with StateStore(db) as store:
        second = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([changed]), runner=ShellRunner(config.runner), owner_id="owner")
        claim = store.get_claim(changed.id)

    assert first.status == "failed"
    assert second.status == "completed"
    assert counter.read_text() == "2"
    assert claim.status == "completed"
    assert claim.attempt_count == 2


def test_process_next_issue_once_retries_failed_issue_after_command_template_changes_and_completes(tmp_path):
    counter = tmp_path / "counter.txt"
    failing_config = app_config(tmp_path, f"{sys.executable} -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(\"1\"); raise SystemExit(4)' {counter}")
    passing_config = app_config(tmp_path, result_command('p=pathlib.Path(sys.argv[2]); p.write_text(str(int(p.read_text())+1))', str(counter)))
    issue = make_issue(identifier="DEV-RETRY-COMMAND")
    db = tmp_path / "state.sqlite3"

    with StateStore(db) as store:
        first = process_next_issue_once(config=failing_config, state=store, linear=FakePollingLinear([issue]), runner=ShellRunner(failing_config.runner), owner_id="owner")
    with StateStore(db) as store:
        second = process_next_issue_once(config=passing_config, state=store, linear=FakePollingLinear([issue]), runner=ShellRunner(passing_config.runner), owner_id="owner")
        claim = store.get_claim(issue.id)

    assert first.status == "failed"
    assert second.status == "completed"
    assert counter.read_text() == "2"
    assert claim.status == "completed"
    assert claim.attempt_count == 2


def test_start_comment_failure_logs_structured_warning_and_continues(tmp_path, caplog):
    counter = tmp_path / "counter.txt"
    command = result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter))
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-START-LOG")
    fake = FlakyLinear(fail_start_comments=1)

    with StateStore(tmp_path / "state.sqlite3") as store:
        with caplog.at_level("WARNING", logger="linear_agent_orchestrator.runner"):
            result = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")

    assert result.status == "completed"
    assert counter.read_text() == "ran"
    record = next(record for record in caplog.records if record.message == "start_comment_failed")
    assert record.issue_id == issue.id
    assert record.issue_identifier == issue.identifier
    assert "RuntimeError: transient start comment failure" == record.error


# ---------------------------------------------------------------------------
# Structured-result / Done-gate regression contract
# ---------------------------------------------------------------------------


def make_result_payload(**overrides):
    payload = {
        "version": 1,
        "status": "completed",
        "summary": "done",
        "validation": [{"description": "check", "command": "true", "exit_code": 0, "evidence": "ok"}],
    }
    payload.update(overrides)
    return payload


def copy_payload_command(tmp_path, payload, *, exit_code=0):
    """Command that copies a prebuilt payload file to {result_path}, then exits."""

    source = tmp_path / "payload.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    script = "import shutil,sys; shutil.copyfile(sys.argv[2], sys.argv[1]); raise SystemExit(int(sys.argv[3]))"
    return f"{sys.executable} -c '{script}' {{result_path}} {source} {exit_code}"


def process_single(tmp_path, command, identifier):
    config = app_config(tmp_path, command)
    fake = FakeLinear()
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(
            issue=make_issue(identifier=identifier),
            config=config,
            state=store,
            linear=fake,
            runner=ShellRunner(config.runner),
            owner_id="owner-1",
        )
        claim = store.get_claim(f"issue-{identifier}")
    return result, fake, claim


def test_exit_zero_without_result_file_fails_and_never_moves_done(tmp_path):
    result, fake, claim = process_single(tmp_path, f"{sys.executable} -c 'print(\"done\")'", "DEV-NO-RESULT")

    assert result.status == "failed"
    assert result.done_transitioned is False
    assert claim.status == "failed"
    assert not any(update["state_id"] == "state-done" for update in fake.state_updates)
    failure = next(c["body"] for c in fake.comments if "Runner did not report semantic completion" in c["body"])
    assert "structured result file was not produced" in failure


def test_stale_result_from_previous_run_is_cleared_and_does_not_move_done(tmp_path):
    config = app_config(tmp_path, f"{sys.executable} -c 'print(\"done\")'")
    stale = Path(config.runner.artifacts_dir) / "DEV-STALE" / "result.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(make_result_payload()), encoding="utf-8")
    fake = FakeLinear()

    with StateStore(tmp_path / "state.sqlite3") as store:
        result = process_issue_once(
            issue=make_issue(identifier="DEV-STALE"),
            config=config,
            state=store,
            linear=fake,
            runner=ShellRunner(config.runner),
            owner_id="owner-1",
        )

    assert result.status == "failed"
    assert result.done_transitioned is False
    assert not any(update["state_id"] == "state-done" for update in fake.state_updates)


def test_malformed_result_json_fails_with_contract_error_comment(tmp_path):
    command = f"{sys.executable} -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(\"not-json\")' {{result_path}}"
    result, fake, claim = process_single(tmp_path, command, "DEV-MALFORMED")

    assert result.status == "failed"
    assert result.done_transitioned is False
    assert claim.status == "failed"
    failure = next(c["body"] for c in fake.comments if "Runner did not report semantic completion" in c["body"])
    assert "Result contract error" in failure


@pytest.mark.parametrize(
    ("payload_overrides", "identifier"),
    [
        ({"status": "failed"}, "DEV-SEM-FAILED"),
        ({"status": "blocked"}, "DEV-SEM-BLOCKED"),
        ({"status": "needs_clarification"}, "DEV-SEM-CLARIFY"),
        ({"validation": []}, "DEV-NO-VALIDATION"),
        (
            {"validation": [{"description": "check", "command": "true", "exit_code": 2, "evidence": "boom"}]},
            "DEV-NONZERO-VALIDATION",
        ),
    ],
)
def test_non_completed_or_unvalidated_result_never_moves_done(tmp_path, payload_overrides, identifier):
    command = copy_payload_command(tmp_path, make_result_payload(**payload_overrides))
    result, fake, claim = process_single(tmp_path, command, identifier)

    assert result.status == "failed"
    assert result.done_transitioned is False
    assert claim.status == "failed"
    assert not any(update["state_id"] == "state-done" for update in fake.state_updates)


def test_nonzero_exit_with_valid_completed_result_never_moves_done(tmp_path):
    command = copy_payload_command(tmp_path, make_result_payload(), exit_code=5)
    result, fake, claim = process_single(tmp_path, command, "DEV-TRANSPORT-FAIL")

    assert result.status == "failed"
    assert result.done_transitioned is False
    assert claim.status == "failed"
    assert not any(update["state_id"] == "state-done" for update in fake.state_updates)


def test_completed_and_validated_result_moves_done(tmp_path):
    command = copy_payload_command(tmp_path, make_result_payload())
    result, fake, claim = process_single(tmp_path, command, "DEV-SEM-DONE")

    assert result.status == "completed"
    assert result.done_transitioned is True
    assert claim.status == "completed"
    assert [update["state_id"] for update in fake.state_updates] == ["state-started", "state-done"]


def test_completed_semantically_rejects_blank_validation_evidence(tmp_path):
    paths = RunnerPaths(*(tmp_path,) * 7)
    result = RunnerResult(
        issue_id="issue-x",
        issue_identifier="DEV-X",
        command=("true",),
        paths=paths,
        exit_code=0,
        stdout="",
        stderr="",
        timed_out=False,
        timeout_seconds=5,
        structured_result=StructuredResult(
            1, "completed", "done", (ValidationEvidence("check", "true", 0, "   "),)
        ),
    )

    assert result.succeeded is True
    assert result.completed_semantically is False


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ("[]", "must be a JSON object"),
        (json.dumps(make_result_payload(version="1")), "version must be integer 1"),
        (json.dumps(make_result_payload(version=True)), "version must be integer 1"),
        (json.dumps(make_result_payload(status="finished")), "status must be one of"),
        (json.dumps(make_result_payload(summary="  ")), "summary must be a nonempty string"),
        (json.dumps(make_result_payload(validation={})), "validation must be a JSON array"),
        (
            json.dumps(make_result_payload(validation=[{"description": "d", "command": "c", "exit_code": True, "evidence": "e"}])),
            "exit_code must be an integer",
        ),
        (
            json.dumps(make_result_payload(validation=[{"description": "d", "command": "c", "exit_code": 0, "evidence": " "}])),
            "evidence must be a nonempty string",
        ),
        (json.dumps(make_result_payload(artifacts=["/absolute/path"])), "relative path without '..'"),
        (json.dumps(make_result_payload(files_changed=["a/../escape"])), "relative path without '..'"),
    ],
)
def test_load_structured_result_rejects_contract_violations(tmp_path, payload, expected_error):
    result_path = tmp_path / "result.json"
    result_path.write_text(payload, encoding="utf-8")

    parsed, error = load_structured_result(result_path)

    assert parsed is None
    assert expected_error in error


def test_load_structured_result_missing_file_and_symlink_rejected(tmp_path):
    missing, missing_error = load_structured_result(tmp_path / "absent.json")
    assert missing is None
    assert "was not produced" in missing_error

    target = tmp_path / "target.json"
    target.write_text(json.dumps(make_result_payload()), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    linked, link_error = load_structured_result(link)
    assert linked is None
    assert "must not be a symlink" in link_error


def test_load_structured_result_valid_roundtrip(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(make_result_payload(artifacts=["out/report.md"], files_changed=["src/app.py"])),
        encoding="utf-8",
    )

    parsed, error = load_structured_result(result_path)

    assert error is None
    assert parsed.version == 1
    assert parsed.status == "completed"
    assert parsed.summary == "done"
    assert parsed.validation == (ValidationEvidence("check", "true", 0, "ok"),)
    assert parsed.artifacts == ("out/report.md",)
    assert parsed.files_changed == ("src/app.py",)


# ---------------------------------------------------------------------------
# Workspace prompt/context and clean-tree preflight contracts
# ---------------------------------------------------------------------------


def test_prompt_and_context_carry_workspace_and_result_contract(tmp_path):
    config = runner_config(tmp_path, f"{sys.executable} -c 'print(1)'")
    issue = make_issue(identifier="DEV-CONTRACT")

    paths = ShellRunner(config).materialize(issue)

    assert paths.result_path == paths.artifacts_dir / "result.json"
    prompt = paths.prompt_path.read_text(encoding="utf-8")
    assert "## Trusted Workspace" in prompt
    assert config.project_dir in prompt
    assert str(paths.result_path) in prompt
    assert "Transport exit code 0 without this file is a failure." in prompt
    assert "Linear moves to Done only for status `completed`" in prompt
    assert issue.schema.parsed.get("Task") in prompt
    assert issue.schema.parsed.get("Acceptance Criteria") in prompt
    assert issue.schema.parsed.get("Validation") in prompt

    context = json.loads(paths.context_path.read_text(encoding="utf-8"))
    assert context == render_context(issue, project_dir=config.project_dir, result_path=paths.result_path)
    assert context["result_contract"] == {"version": 1, "path": str(paths.result_path)}
    assert context["workspace"] == {"project_dir": config.project_dir, "trusted_repository_count": 1}
    assert context["issue"]["assignee"] == {"id": issue.assignee_id, "name": issue.assignee_name}


def test_materialize_removes_stale_result_file(tmp_path):
    config = runner_config(tmp_path, f"{sys.executable} -c 'print(1)'")
    stale = Path(config.artifacts_dir) / "DEV-1" / "result.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    paths = ShellRunner(config).materialize(make_issue())

    assert paths.result_path == stale
    assert not paths.result_path.exists()


def write_fake_agent_runner(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "lao-agent-runner"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "payload = dict(version=1, status=\"completed\", summary=\"done\", "
        "validation=[dict(description=\"check\", command=\"true\", exit_code=0, evidence=\"ok\")])\n"
        "json.dump(payload, open(sys.argv[2], \"w\"))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_dirty_workspace_blocks_without_state_and_cleanup_retries_same_key(tmp_path, monkeypatch):
    import linear_agent_orchestrator.runner as runner_module

    script = write_fake_agent_runner(tmp_path)
    config = app_config(tmp_path, f"{script} {{prompt_path}} {{result_path}}")
    issue = make_issue(identifier="DEV-DIRTY")
    exec_key = execution_key(issue, config.runner)
    db = tmp_path / "state.sqlite3"
    fake = FakeLinear()
    monkeypatch.setattr(
        runner_module,
        "validate_project_workspace",
        lambda project_dir: [f"Agent project git worktree is dirty: {project_dir}"],
    )

    with StateStore(db) as store:
        blocked = process_issue_once(issue=issue, config=config, state=store, linear=fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        claim = store.get_claim(issue.id)
        active = store.list_active_work()

    assert blocked.status == "workspace_blocked"
    assert blocked.claimed is True
    assert blocked.executed is False
    assert blocked.done_transitioned is False
    assert "dirty" in blocked.message
    assert fake.state_updates == []
    assert claim.status == "released"
    assert not active
    assert any("dirty" in comment["body"] for comment in fake.comments)

    # With the issue absent from Todo, nothing may be resumed or executed from local state.
    no_run = CountingRunner()
    with StateStore(db) as store:
        swept = process_next_issue_once(config=config, state=store, linear=FakePollingLinear([]), runner=no_run, owner_id="owner-1")

    assert swept.status == "no_candidate"
    assert no_run.calls == 0

    # After cleanup the same execution key must complete and transition to Done.
    monkeypatch.setattr(runner_module, "validate_project_workspace", lambda project_dir: [])
    clean_fake = FakeLinear()
    with StateStore(db) as store:
        completed = process_issue_once(issue=issue, config=config, state=store, linear=clean_fake, runner=ShellRunner(config.runner), owner_id="owner-1")
        final_claim = store.get_claim(issue.id)

    assert completed.status == "completed"
    assert completed.done_transitioned is True
    assert final_claim.status == "completed"
    assert final_claim.execution_key == exec_key
    assert [update["state_id"] for update in clean_fake.state_updates] == ["state-started", "state-done"]
