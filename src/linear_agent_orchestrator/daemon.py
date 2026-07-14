"""Long-running polling daemon for Linear agent orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import signal
import threading
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
# Builds one worker's private (state, linear, runner) triple; SQLite connections
# must not be shared across threads.
ComponentFactory = Callable[[], tuple[StateStore, LinearReporter, ShellRunner]]


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


def interruptible_sleep(total_seconds: float, stopper: GracefulStop, sleep: Sleep) -> None:
    """Sleep up to total_seconds, waking within ~1s of a stop request.

    PEP 475 makes a plain time.sleep(interval) resume after a signal handler
    returns, so a Ctrl-C during the poll pause would otherwise wait out the full
    interval before the loop notices the stop flag. Sleeping in bounded chunks
    keeps shutdown latency ~1s regardless of the configured interval. A run
    already in flight is still allowed to finish; only idle waits are cut short.
    """

    remaining = float(total_seconds)
    while remaining > 0 and not stopper.requested:
        sleep(min(1.0, remaining))
        remaining -= 1.0


def run_daemon(
    *,
    config: AppConfig,
    state: StateStore | None = None,
    linear: LinearReporter | None = None,
    runner: ShellRunner | None = None,
    owner_id: str,
    max_iterations: int | None = None,
    sleep: Sleep = time.sleep,
    stop: GracefulStop | None = None,
    component_factory: ComponentFactory | None = None,
) -> DaemonResult:
    """Run the polling loop (serial, or MAX_PARALLEL_RUNS workers) until signaled.

    With max_parallel_runs > 1 each worker thread gets its own components from
    component_factory (SQLite connections are not shareable across threads) and
    a distinct claim owner id; claims/leases stop two workers from taking the
    same issue.
    """

    stopper = stop or GracefulStop()
    workers = max(1, config.max_parallel_runs)
    if workers > 1:
        if component_factory is None:
            raise ValueError("max_parallel_runs > 1 requires component_factory")
        return _run_parallel(
            config=config,
            owner_id=owner_id,
            component_factory=component_factory,
            workers=workers,
            max_iterations=max_iterations,
            sleep=sleep,
            stopper=stopper,
        )
    if state is None or linear is None or runner is None:
        raise ValueError("state, linear, and runner are required when max_parallel_runs == 1")
    with stopper:
        return _poll_loop(
            config=config,
            state=state,
            linear=linear,
            runner=runner,
            owner_id=owner_id,
            stopper=stopper,
            max_iterations=max_iterations,
            sleep=sleep,
        )


def _run_parallel(
    *,
    config: AppConfig,
    owner_id: str,
    component_factory: ComponentFactory,
    workers: int,
    max_iterations: int | None,
    sleep: Sleep,
    stopper: GracefulStop,
) -> DaemonResult:
    results: list[DaemonResult | None] = [None] * workers
    errors: list[BaseException | None] = [None] * workers
    # First-time schema migration on a fresh database is not safe to run from
    # many connections at once; serialize component construction.
    factory_lock = threading.Lock()

    def _work(index: int) -> None:
        worker_owner = owner_id if index == 0 else f"{owner_id}-w{index + 1}"
        # Stagger poll starts so workers do not hit Linear in lockstep.
        stagger = config.poll_interval_seconds * index / workers if config.poll_interval_seconds > 0 else 0.0
        try:
            with factory_lock:
                state, linear, runner = component_factory()
            with state:
                results[index] = _poll_loop(
                    config=config,
                    state=state,
                    linear=linear,
                    runner=runner,
                    owner_id=worker_owner,
                    stopper=stopper,
                    max_iterations=max_iterations,
                    sleep=sleep,
                    initial_stagger_seconds=stagger,
                )
        except BaseException as exc:  # noqa: BLE001 - a crashed worker must stop the fleet loudly
            errors[index] = exc
            LOGGER.error("daemon_worker_crashed", extra={"worker": index + 1, "error": str(exc)})
            stopper.requested = True

    with stopper:
        threads = [
            threading.Thread(target=_work, args=(index,), name=f"lao-worker-{index + 1}", daemon=True)
            for index in range(workers)
        ]
        for thread in threads:
            thread.start()
        # Join with a timeout loop so the main thread keeps handling signals.
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.2)
    first_error = next((error for error in errors if error is not None), None)
    if first_error is not None:
        raise first_error
    return DaemonResult(
        iterations=sum(result.iterations for result in results if result),
        stop_requested=stopper.requested,
        last_status=next((result.last_status for result in results if result and result.last_status), None),
    )


def _poll_loop(
    *,
    config: AppConfig,
    state: StateStore,
    linear: LinearReporter,
    runner: ShellRunner,
    owner_id: str,
    stopper: GracefulStop,
    max_iterations: int | None,
    sleep: Sleep,
    initial_stagger_seconds: float = 0.0,
) -> DaemonResult:
    iterations = 0
    transient_attempt = 0
    last_status: str | None = None
    if initial_stagger_seconds > 0:
        interruptible_sleep(initial_stagger_seconds, stopper, sleep)
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
                interruptible_sleep(config.poll_interval_seconds, stopper, sleep)
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
                interruptible_sleep(delay.delay_seconds, stopper, sleep)
                continue
            raise
    return DaemonResult(iterations=iterations, stop_requested=stopper.requested, last_status=last_status)


def build_live_daemon_components(config: AppConfig) -> tuple[StateStore, LinearClient, ShellRunner]:
    """Construct production daemon components from config."""

    if not config.state_db_path:
        raise ValueError("STATE_DB_PATH is required to build live daemon components")
    return StateStore(config.state_db_path), LinearClient.from_config(config.linear), ShellRunner(config.runner)
