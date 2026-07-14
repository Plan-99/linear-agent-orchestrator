import sys

import pytest

from linear_agent_orchestrator.config import AppConfig, LinearConfig, RunnerConfig
from linear_agent_orchestrator.daemon import build_live_daemon_components, run_daemon
from linear_agent_orchestrator.issue_schema import validate_issue_body
from linear_agent_orchestrator.linear_client import LinearComment, LinearIssue, LinearTransportError, WorkflowRole, WorkflowState
from linear_agent_orchestrator.runner import ShellRunner
from linear_agent_orchestrator.state import StateStore


VALID_BODY = """## Task
Write an artifact.

## Acceptance Criteria
- Artifact exists

## Validation
- Inspect output
"""


@pytest.mark.parametrize("state_db_path", [None, ""])
def test_live_daemon_components_require_state_database_path(state_db_path):
    with pytest.raises(ValueError, match="STATE_DB_PATH is required"):
        build_live_daemon_components(AppConfig(state_db_path=state_db_path))


class FakePollingLinear:
    def __init__(self, issues):
        self.issues = tuple(issues)
        self.comments = []
        self.state_updates = []
        self.states = {
            WorkflowRole.QUEUE: WorkflowState("state-todo", "Todo", "unstarted"),
            WorkflowRole.IN_PROGRESS: WorkflowState("state-started", "In Progress", "started"),
            WorkflowRole.DONE: WorkflowState("state-done", "Done", "completed"),
            WorkflowRole.ATTENTION: WorkflowState("state-review", "In Review", "started"),
        }

    def iter_todo_issues(self, config, first=10):
        self.first = first
        return self.issues

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


def make_issue(body=VALID_BODY, identifier="DEV-1"):
    return LinearIssue(
        id=f"issue-{identifier}",
        identifier=identifier,
        title="Daemon story",
        description=body,
        url="https://linear.app/test/issue/DEV-1",
        updated_at="2026-07-09T14:00:00Z",
        state_id="state-todo",
        state_name="Todo",
        project_id="project-test",
        project_name="TEST",
        schema=validate_issue_body(body),
    )


def app_config(tmp_path, command, timeout=5):
    return AppConfig(
        linear=LinearConfig(team_id="DEV", project_name="TEST"),
        runner=RunnerConfig(
            command_template=command,
            timeout_seconds=timeout,
            work_dir=str(tmp_path / "work"),
            artifacts_dir=str(tmp_path / "artifacts"),
        ),
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


class StopAfterSleep:
    def __init__(self):
        self.requested = False
        self.sleeps = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.requested = True


class FlakyLinear(FakePollingLinear):
    def __init__(self, issues):
        super().__init__(issues)
        self.calls = 0

    def iter_todo_issues(self, config, first=10):
        self.calls += 1
        if self.calls == 1:
            raise LinearTransportError("temporary", transient=True)
        return super().iter_todo_issues(config, first)


class TransitionFailingLinear(FakePollingLinear):
    def __init__(self, issues, *, fail_in_progress=0):
        super().__init__(issues)
        self.fail_in_progress = fail_in_progress

    def update_issue_state(self, issue_id, state_id):
        if state_id == self.states[WorkflowRole.IN_PROGRESS].id and self.fail_in_progress:
            self.fail_in_progress -= 1
            raise RuntimeError("temporary in-progress transition failure")
        return super().update_issue_state(issue_id, state_id)


def test_daemon_bounded_execution_runs_one_iteration(tmp_path):
    counter = tmp_path / "counter.txt"
    config = app_config(tmp_path, result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter)))
    fake = FakePollingLinear([make_issue(identifier="DEV-DAEMON")])
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = run_daemon(
            config=config,
            state=store,
            linear=fake,
            runner=ShellRunner(config.runner),
            owner_id="daemon",
            max_iterations=1,
            sleep=lambda seconds: None,
        )

    assert result.iterations == 1
    assert result.last_status == "completed"
    assert counter.read_text() == "ran"


def test_daemon_no_candidate_gracefully_sleeps_until_stopped(tmp_path):
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    config = AppConfig(linear=config.linear, runner=config.runner, poll_interval_seconds=7)
    fake = FakePollingLinear([])
    stop = StopAfterSleep()
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = run_daemon(
            config=config,
            state=store,
            linear=fake,
            runner=ShellRunner(config.runner),
            owner_id="daemon",
            sleep=stop.sleep,
            stop=stop,
        )

    assert result.iterations == 1
    assert result.stop_requested is True
    assert result.last_status == "no_candidate"
    # Chunked interruptible sleep: the stop request lands during the first 1s
    # chunk and the remaining 6s of the poll interval are never slept.
    assert stop.sleeps == [1.0]


def test_interruptible_sleep_wakes_on_stop_request():
    from linear_agent_orchestrator.daemon import GracefulStop, interruptible_sleep

    stopper = GracefulStop()
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            stopper.requested = True

    interruptible_sleep(60, stopper, fake_sleep)
    assert sleeps == [1.0, 1.0, 1.0]

    # No stop request: the full duration is slept in bounded chunks.
    sleeps.clear()
    interruptible_sleep(2.5, GracefulStop(), sleeps.append)
    assert sleeps == [1.0, 1.0, 0.5]

    # Already-requested stop skips the wait entirely.
    stopped = GracefulStop()
    stopped.requested = True
    sleeps.clear()
    interruptible_sleep(60, stopped, sleeps.append)
    assert sleeps == []


def test_daemon_reaps_stale_leases_each_loop(tmp_path):
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    fake = FakePollingLinear([])
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.acquire_claim("issue-stale", "old-owner", lease_ttl_seconds=1)
        store.clock = lambda: store.get_claim("issue-stale").expires_at
        result = run_daemon(
            config=config,
            state=store,
            linear=fake,
            runner=ShellRunner(config.runner),
            owner_id="daemon",
            max_iterations=1,
            sleep=lambda seconds: None,
        )
        claim = store.get_claim("issue-stale")

    assert result.iterations == 1
    assert claim.status == "stale"


def test_daemon_transient_error_uses_backoff_then_recovers(tmp_path):
    counter = tmp_path / "counter.txt"
    config = app_config(tmp_path, result_command('pathlib.Path(sys.argv[2]).write_text("ran")', str(counter)))
    fake = FlakyLinear([make_issue(identifier="DEV-FLAKY")])
    sleeps = []
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = run_daemon(
            config=config,
            state=store,
            linear=fake,
            runner=ShellRunner(config.runner),
            owner_id="daemon",
            max_iterations=2,
            sleep=sleeps.append,
        )

    assert result.iterations == 2
    assert result.last_status == "completed"
    assert sleeps and sleeps[0] > 0
    assert counter.read_text() == "ran"

class BrokenRunner:
    def preflight_workspace(self):
        return []

    def run(self, issue):
        raise PermissionError("cannot create /workspace/work/DEV-DAEMON")


def test_daemon_runner_exception_returns_failed_status_without_crashing(tmp_path):
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    fake = FakePollingLinear([make_issue(identifier="DEV-DAEMON-FAIL")])
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = run_daemon(
            config=config,
            state=store,
            linear=fake,
            runner=BrokenRunner(),
            owner_id="daemon",
            max_iterations=1,
            sleep=lambda seconds: None,
        )
        claim = store.get_claim("issue-DEV-DAEMON-FAIL")

    assert result.iterations == 1
    assert result.last_status == "failed"
    assert claim.status == "failed"
    assert [update["state_id"] for update in fake.state_updates] == ["state-started", "state-review"]
    assert any("Runner setup or execution failed" in comment["body"] for comment in fake.comments)


def test_daemon_iteration_complete_log_includes_result_message(tmp_path, caplog):
    config = app_config(tmp_path, f"{sys.executable} -c 'raise SystemExit(99)'")
    fake = FakePollingLinear([])
    with StateStore(tmp_path / "state.sqlite3") as store:
        with caplog.at_level("INFO", logger="linear_agent_orchestrator.daemon"):
            run_daemon(
                config=config,
                state=store,
                linear=fake,
                runner=ShellRunner(config.runner),
                owner_id="daemon",
                max_iterations=1,
                sleep=lambda seconds: None,
            )

    record = next(record for record in caplog.records if record.message == "daemon_iteration_complete")
    assert record.status == "no_candidate"
    assert "No processable Todo issue found" in record.result["message"]


def test_daemon_logs_pending_in_progress_transition_then_retries_and_completes_once(tmp_path, caplog):
    counter = tmp_path / "counter.txt"
    command = result_command(
        "p=pathlib.Path(sys.argv[2]); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        'print("done")',
        str(counter),
    )
    config = app_config(tmp_path, command)
    issue = make_issue(identifier="DEV-DAEMON-INPROG")
    fake = TransitionFailingLinear([issue], fail_in_progress=1)

    with StateStore(tmp_path / "state.sqlite3") as store:
        with caplog.at_level("INFO", logger="linear_agent_orchestrator.daemon"):
            result = run_daemon(
                config=config,
                state=store,
                linear=fake,
                runner=ShellRunner(config.runner),
                owner_id="daemon",
                max_iterations=2,
                sleep=lambda seconds: None,
            )
        claim = store.get_claim(issue.id)

    assert result.iterations == 2
    assert result.last_status == "completed"
    assert counter.read_text() == "1"
    assert claim.status == "completed"
    assert [update["state_id"] for update in fake.state_updates] == ["state-started", "state-done"]

    records = [record for record in caplog.records if record.message == "daemon_iteration_complete"]
    assert [record.status for record in records] == ["in_progress_transition_pending", "completed"]
    assert "Runner was not started" in records[0].result["message"]
    assert "temporary in-progress transition failure" in records[0].result["message"]
