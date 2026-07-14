"""Per-run git worktree isolation: branch commit, preservation, and preflight."""

from pathlib import Path
import shutil
import subprocess

import pytest

from linear_agent_orchestrator.config import RunnerConfig
from linear_agent_orchestrator.issue_schema import validate_issue_body
from linear_agent_orchestrator.linear_client import LinearIssue
from linear_agent_orchestrator.runner import RunnerError, ShellRunner, execution_key

VALID_BODY = """## Task
Write an artifact.

## Acceptance Criteria
- Artifact exists

## Validation
- Inspect output
"""

COMPLETED_RESULT = (
    '{"version":1,"status":"completed","summary":"done",'
    '"validation":[{"description":"t","command":"true","exit_code":0,"evidence":"ok"}],'
    '"artifacts":[],"files_changed":["agent-output.txt"]}'
)
FAILED_RESULT = '{"version":1,"status":"failed","summary":"nope","validation":[]}'


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, check=False)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    return repo


def make_wrapper(tmp_path, *, result_json, touch_file=True):
    """Fake standard wrapper: writes a change into $AGENT_PROJECT_DIR and a result to $2.

    Uses only shell builtins so it works under the runner's allowlisted (empty) env.
    """

    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "lao-agent-runner"
    touch = 'echo "agent was here" > "$AGENT_PROJECT_DIR/agent-output.txt"\n' if touch_file else ""
    wrapper.write_text(
        "#!/bin/bash\n"
        f"{touch}"
        f"printf '%s' '{result_json}' > \"$2\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def make_issue(identifier="DEV-1"):
    return LinearIssue(
        id=f"issue-{identifier}",
        identifier=identifier,
        title="Worktree story",
        description=VALID_BODY,
        url=None,
        updated_at="2026-07-10T14:00:00Z",
        state_id="state-todo",
        state_name="Todo",
        project_id="project-test",
        project_name="TEST",
        schema=validate_issue_body(VALID_BODY),
    )


def make_config(tmp_path, repo, wrapper, *, isolation=True):
    return RunnerConfig(
        command_template=f"{wrapper} {{prompt_path}} {{result_path}}",
        timeout_seconds=30,
        work_dir=str(tmp_path / "work"),
        artifacts_dir=str(tmp_path / "artifacts"),
        allowed_env=(),
        project_dir=str(repo),
        worktree_isolation=isolation,
    )


def test_successful_run_commits_to_local_branch_and_cleans_up(tmp_path):
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    config = make_config(tmp_path, repo, wrapper)
    issue = make_issue()

    result = ShellRunner(config).run(issue)

    assert result.completed_semantically
    key8 = execution_key(issue, config)[:8]
    assert result.worktree_branch == f"lao/DEV-1-{key8}"
    # Base repository never sees the agent's edit and stays clean.
    assert git(repo, "status", "--porcelain").stdout.strip() == ""
    assert not (repo / "agent-output.txt").exists()
    # The branch holds exactly the agent's commit.
    show = git(repo, "show", "--stat", result.worktree_branch)
    assert "agent-output.txt" in show.stdout
    assert "lao: DEV-1 Worktree story" in show.stdout
    # The throwaway worktree is gone.
    assert not (Path(config.work_dir) / "worktrees" / "DEV-1").exists()
    assert "worktrees/DEV-1" not in git(repo, "worktree", "list").stdout


def test_prompt_and_env_point_at_the_isolated_worktree(tmp_path):
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    config = make_config(tmp_path, repo, wrapper)
    runner = ShellRunner(config)

    result = runner.run(make_issue())

    worktree = str(Path(config.work_dir) / "worktrees" / "DEV-1")
    prompt = result.paths.prompt_path.read_text(encoding="utf-8")
    assert worktree in prompt
    assert str(repo) not in prompt


def test_failed_run_keeps_dirty_worktree_for_inspection(tmp_path):
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=FAILED_RESULT)
    config = make_config(tmp_path, repo, wrapper)

    result = ShellRunner(config).run(make_issue())

    assert not result.completed_semantically
    assert result.worktree_branch is None
    assert result.worktree_note and "kept for inspection" in result.worktree_note
    worktree = Path(config.work_dir) / "worktrees" / "DEV-1"
    assert (worktree / "agent-output.txt").exists()
    # Base repo stays clean either way.
    assert git(repo, "status", "--porcelain").stdout.strip() == ""


def test_completed_run_without_changes_leaves_no_branch(tmp_path):
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT, touch_file=False)
    config = make_config(tmp_path, repo, wrapper)

    result = ShellRunner(config).run(make_issue())

    assert result.completed_semantically
    assert result.worktree_branch is None
    assert result.worktree_note and "without file changes" in result.worktree_note
    assert "lao/DEV-1" not in git(repo, "branch", "--list", "lao/*").stdout
    assert not (Path(config.work_dir) / "worktrees" / "DEV-1").exists()


def test_stale_worktree_from_previous_run_is_replaced(tmp_path):
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    config = make_config(tmp_path, repo, wrapper)
    stale = Path(config.work_dir) / "worktrees" / "DEV-1"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("stale", encoding="utf-8")

    result = ShellRunner(config).run(make_issue())

    assert result.completed_semantically
    assert result.worktree_branch is not None
    show = git(repo, "show", "--stat", result.worktree_branch)
    assert "leftover.txt" not in show.stdout


def test_branch_collision_gets_unique_suffix(tmp_path):
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    config = make_config(tmp_path, repo, wrapper)
    issue = make_issue()
    key8 = execution_key(issue, config)[:8]
    git(repo, "branch", f"lao/DEV-1-{key8}")

    result = ShellRunner(config).run(issue)

    assert result.worktree_branch == f"lao/DEV-1-{key8}-2"


def test_agent_self_commit_keeps_branch(tmp_path):
    """Regression: an agent that ran `git commit` itself left a clean worktree,
    and the orchestrator deleted the branch as "no changes" — destroying the
    committed work (observed live on DEV-26)."""

    repo = make_repo(tmp_path)
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "lao-agent-runner"
    git_bin = subprocess.run(["which", "git"], text=True, capture_output=True).stdout.strip()
    wrapper.write_text(
        "#!/bin/bash\n"
        "echo 'agent work' > \"$AGENT_PROJECT_DIR/self-committed.txt\"\n"
        f"{git_bin} -C \"$AGENT_PROJECT_DIR\" add -A\n"
        f"{git_bin} -C \"$AGENT_PROJECT_DIR\" -c user.name=agent -c user.email=a@a commit -q -m 'agent: self commit'\n"
        f"printf '%s' '{COMPLETED_RESULT}' > \"$2\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    config = make_config(tmp_path, repo, wrapper)
    issue = make_issue()

    result = ShellRunner(config).run(issue)

    assert result.completed_semantically
    key8 = execution_key(issue, config)[:8]
    assert result.worktree_branch == f"lao/DEV-1-{key8}", "self-committed work must keep its branch"
    show = git(repo, "show", "--stat", result.worktree_branch)
    assert "self-committed.txt" in show.stdout
    assert "agent: self commit" in show.stdout
    assert not (Path(config.work_dir) / "worktrees" / "DEV-1").exists()
    # Base repo untouched either way.
    assert git(repo, "status", "--porcelain").stdout.strip() == ""


def test_failed_run_with_self_commit_keeps_branch_for_inspection(tmp_path):
    repo = make_repo(tmp_path)
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "lao-agent-runner"
    git_bin = subprocess.run(["which", "git"], text=True, capture_output=True).stdout.strip()
    wrapper.write_text(
        "#!/bin/bash\n"
        "echo 'partial' > \"$AGENT_PROJECT_DIR/partial.txt\"\n"
        f"{git_bin} -C \"$AGENT_PROJECT_DIR\" add -A\n"
        f"{git_bin} -C \"$AGENT_PROJECT_DIR\" -c user.name=agent -c user.email=a@a commit -q -m 'agent: partial'\n"
        f"printf '%s' '{FAILED_RESULT}' > \"$2\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    config = make_config(tmp_path, repo, wrapper)

    result = ShellRunner(config).run(make_issue())

    assert not result.completed_semantically
    assert result.worktree_branch is None
    assert result.worktree_note and "branch" in result.worktree_note
    branches = git(repo, "branch", "--list", "lao/DEV-1*").stdout
    assert "lao/DEV-1" in branches, "committed partial work must survive for inspection"


def test_preflight_allows_dirty_base_repo_when_isolated(tmp_path):
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    (repo / "uncommitted.txt").write_text("dirty", encoding="utf-8")

    isolated = ShellRunner(make_config(tmp_path, repo, wrapper, isolation=True))
    legacy = ShellRunner(make_config(tmp_path, repo, wrapper, isolation=False))

    assert isolated.preflight_workspace() == []
    assert any("dirty" in error for error in legacy.preflight_workspace())


def test_stale_registration_without_directory_is_cleared(tmp_path):
    """Regression: a .git/worktrees entry whose directory is gone (operator
    cleanup, crash between rmtree and prune) wedged the issue forever because
    cleanup only ran when the directory existed, while `git worktree add`
    refuses over the registration ("missing but already registered")."""
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    config = make_config(tmp_path, repo, wrapper)
    stale = Path(config.work_dir) / "worktrees" / "DEV-1"
    git(repo, "worktree", "add", "-b", "stale-branch", str(stale), "HEAD")
    shutil.rmtree(stale)

    result = ShellRunner(config).run(make_issue())

    assert result.completed_semantically
    assert "worktrees/DEV-1" not in git(repo, "worktree", "list").stdout


def test_locked_registration_from_killed_add_is_cleared(tmp_path):
    """Regression: a run killed inside `git worktree add` leaves the entry
    locked ("initializing"); single-force remove and prune both skip it, so
    every retry failed with "missing but locked worktree"."""
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    config = make_config(tmp_path, repo, wrapper)
    stale = Path(config.work_dir) / "worktrees" / "DEV-1"
    git(repo, "worktree", "add", "-b", "stale-branch", str(stale), "HEAD")
    git(repo, "worktree", "lock", "--reason", "initializing", str(stale))
    shutil.rmtree(stale)

    result = ShellRunner(config).run(make_issue())

    assert result.completed_semantically
    assert "worktrees/DEV-1" not in git(repo, "worktree", "list").stdout


def test_failed_worktree_add_does_not_leak_branch(tmp_path):
    """`worktree add -b` creates the branch before the checks that can fail
    the add; without cleanup every failed retry leaked one lao/* branch."""
    repo = make_repo(tmp_path)
    wrapper = make_wrapper(tmp_path, result_json=COMPLETED_RESULT)
    config = make_config(tmp_path, repo, wrapper)
    issue = make_issue()
    # A plain file at the worktree path survives cleanup (it is not a
    # registered worktree and not a directory) and makes the add fail.
    blocker = Path(config.work_dir) / "worktrees" / "DEV-1"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RunnerError, match="Failed to create isolated worktree"):
        ShellRunner(config).run(issue)

    assert git(repo, "branch", "--list", "lao/*").stdout.strip() == ""
