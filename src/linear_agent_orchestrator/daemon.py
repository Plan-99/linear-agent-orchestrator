"""Long-running polling daemon for Linear agent orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import signal
import time
from types import FrameType
from typing import Any, Callable

from .config import AppConfig
from .linear_client import LinearClient, LinearGraphQLError, LinearTransportError
from .runner import LinearReporter, ShellRunner, process_next_issue_once
from .state import StateStore, compute_backoff_delay

LOGGER = logging.getLogger(__name__)
Sleep = Callable[[float], None]
SignalHandler = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None


@dataclass(frozen=True)
class DaemonResult:
    iterations: int
    stop_requested: bool
    last_status: str | None = None


class GracefulStop:
    """Signal-aware stop flag."""

    def __init__(self) -> None:
        self.requested = False
        self._previous: dict[int, SignalHandler] = {}

    def __enter__(self) -> "GracefulStop":
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handler)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        for sig, previous in self._previous.items():
            signal.signal(sig, previous)

    def _handler(self, signum: int, frame: FrameType | None) -> None:  # noqa: ARG002
        LOGGER.info("daemon_stop_requested", extra={"signal": signum})
        self.requested = True


def run_daemon(
    *,
    config: AppConfig,
    state: StateStore,
    linear: LinearReporter,
    runner: ShellRunner,
    owner_id: str,
    max_iterations: int | None = None,
    sleep: Sleep = time.sleep,
    stop: GracefulStop | None = None,
) -> DaemonResult:
    """Run serial polling loop until signaled or max_iterations is reached."""

    stopper = stop or GracefulStop()
    iterations = 0
    transient_attempt = 0
    last_status: str | None = None
    with stopper:
        while not stopper.requested:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            state.reap_stale_leases()
            try:
                result = process_next_issue_once(
                    config=config,
                    state=state,
                    linear=linear,
                    runner=runner,
                    owner_id=owner_id,
                )
                transient_attempt = 0
                last_status = result.status
                LOGGER.info(
                    "daemon_iteration_complete",
                    extra={
                        "iteration": iterations,
                        "status": result.status,
                        "issue_identifier": result.issue_identifier,
                        "executed": result.executed,
                        "done_transitioned": result.done_transitioned,
                        "result": {"message": result.message},
                    },
                )
                if max_iterations is not None and iterations >= max_iterations:
                    break
                if config.poll_interval_seconds > 0:
                    sleep(config.poll_interval_seconds)
            except (LinearGraphQLError, LinearTransportError) as exc:
                transient = bool(getattr(exc, "is_transient", False) or getattr(exc, "transient", False))
                LOGGER.warning(
                    "daemon_linear_error",
                    extra={"iteration": iterations, "transient": transient, "error": str(exc)},
                )
                if transient:
                    transient_attempt += 1
                    delay = compute_backoff_delay(transient_attempt, config.backoff)
                    last_status = "transient_error"
                    if delay.exhausted:
                        LOGGER.error("daemon_backoff_exhausted", extra={"attempt": transient_attempt})
                        raise
                    sleep(delay.delay_seconds)
                    continue
                raise
    return DaemonResult(iterations=iterations, stop_requested=stopper.requested, last_status=last_status)


def build_live_daemon_components(config: AppConfig) -> tuple[StateStore, LinearClient, ShellRunner]:
    """Construct production daemon components from config."""

    return StateStore(config.state_db_path), LinearClient.from_config(config.linear), ShellRunner(config.runner)
