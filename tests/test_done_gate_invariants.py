"""Fast adversarial unit tests for the strict result.json Done gate.

``RunnerResult.completed_semantically`` is the single predicate that decides
whether a Linear issue may move to Done. The tmux UI epic must preserve it
unchanged (task 8). These tests pin the predicate directly -- no subprocess, no
Linear -- so a regression is caught cheaply and unambiguously.

The most important adversarial case: a run that *timed out* but whose backend
managed to flush a syntactically valid ``result.json`` before the process group
was killed must NOT be treated as complete. Transport success (exit 0, no
timeout) is a required precondition for trusting the structured result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from linear_agent_orchestrator.runner import (
    RunnerPaths,
    RunnerResult,
    StructuredResult,
    ValidationEvidence,
)


def _paths() -> RunnerPaths:
    stub = Path("/nonexistent")
    return RunnerPaths(stub, stub, stub, stub, stub, stub, stub)


def _valid_structured() -> StructuredResult:
    return StructuredResult(
        version=1,
        status="completed",
        summary="done",
        validation=(ValidationEvidence("check", "true", 0, "ok"),),
    )


def make_result(**overrides: object) -> RunnerResult:
    fields: dict[str, object] = dict(
        issue_id="issue-1",
        issue_identifier="DEV-1",
        command=("lao-agent-runner",),
        paths=_paths(),
        exit_code=0,
        stdout="",
        stderr="",
        timed_out=False,
        timeout_seconds=5,
        structured_result=_valid_structured(),
        result_error=None,
    )
    fields.update(overrides)
    return RunnerResult(**fields)  # type: ignore[arg-type]


def test_fully_valid_completed_run_is_semantically_complete():
    assert make_result().completed_semantically is True


def test_timeout_with_late_valid_result_is_not_complete():
    """A killed-on-timeout run cannot reach Done even if a valid result was flushed."""

    timed_out = make_result(timed_out=True, exit_code=None)
    assert timed_out.succeeded is False
    assert timed_out.completed_semantically is False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exit_code": None}, "no transport exit code"),
        ({"exit_code": 1}, "nonzero transport exit"),
        ({"structured_result": None}, "missing structured result"),
        (
            {"structured_result": StructuredResult(1, "blocked", "s", (ValidationEvidence("c", "true", 0, "ok"),))},
            "status not completed",
        ),
        (
            {"structured_result": StructuredResult(1, "completed", "s", ())},
            "completed without validation evidence",
        ),
        (
            {"structured_result": StructuredResult(1, "completed", "s", (ValidationEvidence("c", "true", 1, "boom"),))},
            "validation exit code nonzero",
        ),
        (
            {"structured_result": StructuredResult(1, "completed", "s", (ValidationEvidence("c", "true", 0, "   "),))},
            "validation evidence is blank",
        ),
    ],
)
def test_incomplete_or_untrusted_runs_never_reach_done(overrides: dict[str, object], reason: str):
    assert make_result(**overrides).completed_semantically is False, reason
