"""Parallel worker pool: claims dedupe, both-issues coverage, config gates."""

from pathlib import Path
import json
import sys

import pytest

from linear_agent_orchestrator.config import AppConfig, LinearConfig, RunnerConfig, load_config
from linear_agent_orchestrator.daemon import run_daemon
from linear_agent_orchestrator.issue_schema import validate_issue_body
from linear_agent_orchestrator.linear_client import LinearIssue, WorkflowRole, WorkflowState
from linear_agent_orchestrator.runner import ShellRunner
from linear_agent_orchestrator.state import StateStore

VALID_BODY = """## Task
Do the work.

## Acceptance Criteria
- Done

## Validation
- Check
"""

# O_APPEND keeps concurrent one-line writes atomic across worker processes.
RESULT_SNIPPET = (
    "import json, sys; "
    "payload = dict(version=1, status=\"completed\", summary=\"done\", "
    "validation=[dict(description=\"check\", command=\"true\", exit_code=0, evidence=\"ok\")]); "
    "json.dump(payload, open(sys.argv[1], \"w\")); "
    "open(sys.argv[2], \"a\").write(sys.argv[3] + chr(10))"
)


def make_issue(identifier):
    return LinearIssue(
        id=f"issue-{identifier}",
        identifier=identifier,
        title="Parallel story",
        description=VALID_BODY,
        url=None,
        updated_at="2026-07-10T15:00:00Z",
        state_id="state-todo",
        state_name="Todo",
        project_id="project-test",
        project_name="TEST",
        schema=validate_issue_body(VALID_BODY),
    )


class ThreadSafeFakeLinear:
    """Minimal Linear fake shared by workers; list appends are GIL-atomic."""

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
        return self.issues

    def create_comment(self, issue_id, body):
        comment_id = f"comment-{len(self.comments) + 1}"
        self.comments.append({"id": comment_id, "issue_id": issue_id, "body": body})
        return comment_id

    def list_issue_comments(self, issue_id, *, first=50):
        from linear_agent_orchestrator.linear_client import LinearComment

        return tuple(
            LinearComment(id=comment["id"], body=comment["body"])
            for comment in self.comments
            if comment["issue_id"] == issue_id
        )

    def update_issue_state(self, issue_id, state_id):
        self.state_updates.append({"issue_id": issue_id, "state_id": state_id})
        return True

    def resolve_workflow_states(self, config):
        return self.states


def parallel_config(tmp_path, executed_log, workers=2):
    command = f"{sys.executable} -c '{RESULT_SNIPPET}' {{result_path}} {executed_log} {{issue_identifier}}"
    return AppConfig(
        linear=LinearConfig(team_id="DEV", project_name="TEST"),
        runner=RunnerConfig(
            command_template=command,
            timeout_seconds=15,
            work_dir=str(tmp_path / "work"),
            artifacts_dir=str(tmp_path / "artifacts"),
        ),
        max_parallel_runs=workers,
        poll_interval_seconds=1,
    )


def test_two_workers_process_two_issues_exactly_once(tmp_path):
    executed_log = tmp_path / "executed.log"
    config = parallel_config(tmp_path, executed_log, workers=2)
    fake = ThreadSafeFakeLinear([make_issue("DEV-A"), make_issue("DEV-B")])
    db = tmp_path / "state.sqlite3"

    result = run_daemon(
        config=config,
        owner_id="daemon",
        max_iterations=2,
        sleep=lambda seconds: None,
        component_factory=lambda: (StateStore(db), fake, ShellRunner(config.runner)),
    )

    executed = sorted(executed_log.read_text().split())
    assert executed == ["DEV-A", "DEV-B"], "each issue must execute exactly once"
    assert result.iterations == 4  # 2 workers x 2 iterations
    done_updates = [u for u in fake.state_updates if u["state_id"] == "state-done"]
    assert {u["issue_id"] for u in done_updates} == {"issue-DEV-A", "issue-DEV-B"}
    with StateStore(db) as store:
        owners = {store.get_claim("issue-DEV-A").owner_id, store.get_claim("issue-DEV-B").owner_id}
    assert owners <= {"daemon", "daemon-w2"}


def test_contended_single_issue_executes_once(tmp_path):
    executed_log = tmp_path / "executed.log"
    config = parallel_config(tmp_path, executed_log, workers=3)
    fake = ThreadSafeFakeLinear([make_issue("DEV-ONLY")])
    db = tmp_path / "state.sqlite3"

    run_daemon(
        config=config,
        owner_id="daemon",
        max_iterations=2,
        sleep=lambda seconds: None,
        component_factory=lambda: (StateStore(db), fake, ShellRunner(config.runner)),
    )

    assert executed_log.read_text().split() == ["DEV-ONLY"]
    done_updates = [u for u in fake.state_updates if u["state_id"] == "state-done"]
    assert len(done_updates) == 1


def test_concurrent_state_store_creation_on_fresh_database(tmp_path):
    """Workers opening the same brand-new SQLite file must not hit lock errors."""

    import threading

    db = tmp_path / "fresh.sqlite3"
    barrier = threading.Barrier(8)
    errors = []

    def open_store():
        try:
            barrier.wait(timeout=10)
            with StateStore(db) as store:
                assert store.schema_version() > 0
        except Exception as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []


def test_parallel_requires_component_factory(tmp_path):
    config = parallel_config(tmp_path, tmp_path / "x.log", workers=2)
    with pytest.raises(ValueError, match="component_factory"):
        run_daemon(config=config, owner_id="daemon", max_iterations=1, sleep=lambda s: None)


def test_config_gates_parallel_runs():
    config = load_config(env={"MAX_PARALLEL_RUNS": "3"}, env_files=[])
    assert config.max_parallel_runs == 3
    errors = config.validate_startup(require_secrets=False)
    assert not any("MAX_PARALLEL_RUNS" in error for error in errors)

    without_isolation = load_config(
        env={"MAX_PARALLEL_RUNS": "3", "AGENT_WORKTREE_ISOLATION": "false"},
        env_files=[],
    )
    errors = without_isolation.validate_startup(require_secrets=False)
    assert any("AGENT_WORKTREE_ISOLATION" in error for error in errors)

    zero = load_config(env={"MAX_PARALLEL_RUNS": "0"}, env_files=[])
    assert any("MAX_PARALLEL_RUNS must be positive" in error for error in zero.validate_startup(require_secrets=False))
