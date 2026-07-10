from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import random

from linear_agent_orchestrator.config import BackoffConfig, load_config
from linear_agent_orchestrator.state import ClaimStatus, StateStore, compute_backoff_delay


def ts(seconds=0):
    return datetime(2026, 7, 9, 14, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def open_store(path, now=0):
    return StateStore(path, clock=lambda: ts(now))


def test_acquire_claim_prevents_duplicate_active_claims(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        first = store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=900, now=ts())
        duplicate = store.acquire_claim("issue-1", "owner-b", lease_ttl_seconds=900, now=ts(1))

    assert first.acquired is True
    assert duplicate.acquired is False
    assert duplicate.claim.owner_id == "owner-a"
    assert duplicate.claim.status == ClaimStatus.CLAIMED.value
    assert duplicate.claim.attempt_count == 1


def test_renewal_extends_expiry_for_owner_only(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts())
        assert store.renew_claim("issue-1", "owner-b", lease_ttl_seconds=100, now=ts(10)) is None
        renewed = store.renew_claim("issue-1", "owner-a", lease_ttl_seconds=200, now=ts(10))

    assert renewed is not None
    assert renewed.status == ClaimStatus.RENEWED.value
    assert renewed.renewed_at == ts(10)
    assert renewed.expires_at == ts(210)


def test_expired_claim_can_be_reaped_and_reclaimed(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=10, now=ts())
        assert store.reap_stale_leases(now=ts(9)) == 0
        assert store.reap_stale_leases(now=ts(11)) == 1
        stale = store.get_claim("issue-1")
        reclaimed = store.acquire_claim("issue-1", "owner-b", lease_ttl_seconds=20, now=ts(12))

    assert stale.status == ClaimStatus.STALE.value
    assert stale.owner_id is None
    assert reclaimed.acquired is True
    assert reclaimed.claim.owner_id == "owner-b"
    assert reclaimed.claim.attempt_count == 2
    assert reclaimed.claim.acquired_at == ts()
    assert reclaimed.claim.expires_at == ts(32)


def test_complete_and_failed_terminal_transitions_are_owner_guarded(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts())
        assert store.complete_claim("issue-1", "owner-b", now=ts(1)) is None
        completed = store.complete_claim("issue-1", "owner-a", now=ts(2), execution_key="exec-1")
        after_terminal = store.acquire_claim("issue-1", "owner-b", lease_ttl_seconds=100, now=ts(3))

        store.acquire_claim("issue-2", "owner-a", lease_ttl_seconds=100, now=ts())
        failed = store.fail_claim("issue-2", "owner-a", error="boom", now=ts(4), execution_key="exec-2")

    assert completed.status == ClaimStatus.COMPLETED.value
    assert completed.completed_at == ts(2)
    assert completed.execution_key == "exec-1"
    assert after_terminal.acquired is False
    assert failed.status == ClaimStatus.FAILED.value
    assert failed.last_error == "boom"
    assert failed.terminal is True


def test_failed_same_execution_key_blocks_without_retry_loop(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts(), execution_key="exec-1")
        failed = store.fail_claim("issue-1", "owner-a", error="boom", now=ts(1), execution_key="exec-1")
        retry = store.acquire_claim(
            "issue-1",
            "owner-b",
            lease_ttl_seconds=100,
            now=ts(2),
            execution_key="exec-1",
            allow_failed_retry=True,
        )

    assert failed.status == ClaimStatus.FAILED.value
    assert retry.acquired is False
    assert retry.claim.status == ClaimStatus.FAILED.value
    assert retry.claim.execution_key == "exec-1"
    assert retry.claim.attempt_count == 1


def test_failed_new_execution_key_reacquires_when_explicitly_allowed(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts(), execution_key="exec-1")
        store.fail_claim("issue-1", "owner-a", error="boom", now=ts(1), execution_key="exec-1")
        safe_default = store.acquire_claim("issue-1", "owner-b", lease_ttl_seconds=100, now=ts(2), execution_key="exec-2")
        retry = store.acquire_claim(
            "issue-1",
            "owner-b",
            lease_ttl_seconds=100,
            now=ts(3),
            execution_key="exec-2",
            allow_failed_retry=True,
        )

    assert safe_default.acquired is False
    assert retry.acquired is True
    assert retry.claim.status == ClaimStatus.CLAIMED.value
    assert retry.claim.owner_id == "owner-b"
    assert retry.claim.execution_key == "exec-2"
    assert retry.claim.attempt_count == 2
    assert retry.claim.last_error is None
    assert retry.claim.expires_at == ts(103)


def test_completed_blocks_even_with_new_execution_key_and_retry_allowed(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts(), execution_key="exec-1")
        store.complete_claim("issue-1", "owner-a", now=ts(1), execution_key="exec-1")
        retry = store.acquire_claim(
            "issue-1",
            "owner-b",
            lease_ttl_seconds=100,
            now=ts(2),
            execution_key="exec-2",
            allow_failed_retry=True,
        )

    assert retry.acquired is False
    assert retry.claim.status == ClaimStatus.COMPLETED.value
    assert retry.claim.execution_key == "exec-1"
    assert retry.claim.attempt_count == 1


def test_active_lease_blocks_even_with_new_execution_key_and_retry_allowed(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts(), execution_key="exec-1")
        retry = store.acquire_claim(
            "issue-1",
            "owner-b",
            lease_ttl_seconds=100,
            now=ts(2),
            execution_key="exec-2",
            allow_failed_retry=True,
        )

    assert retry.acquired is False
    assert retry.claim.status == ClaimStatus.CLAIMED.value
    assert retry.claim.owner_id == "owner-a"
    assert retry.claim.execution_key == "exec-1"
    assert retry.claim.expires_at == ts(100)


def test_release_allows_reclaim_without_terminal_status(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts())
        released = store.release_claim("issue-1", "owner-a", now=ts(5))
        reclaimed = store.acquire_claim("issue-1", "owner-b", lease_ttl_seconds=100, now=ts(6))

    assert released.status == ClaimStatus.RELEASED.value
    assert released.owner_id is None
    assert reclaimed.acquired is True
    assert reclaimed.claim.owner_id == "owner-b"


def test_execution_and_comment_dedupe_are_issue_scoped(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        assert store.record_execution_once("issue-1", "hash-a", owner_id="owner-a") is True
        assert store.record_execution_once("issue-1", "hash-a", owner_id="owner-a") is False
        assert store.record_execution_once("issue-2", "hash-a", owner_id="owner-a") is True
        assert store.has_execution("issue-1", "hash-a") is True

        assert store.record_comment_once("issue-1", "schema-missing", comment_id="comment-1") is True
        assert store.record_comment_once("issue-1", "schema-missing", comment_id="comment-2") is False
        assert store.record_comment_once("issue-2", "schema-missing", comment_id="comment-3") is True
        assert store.has_comment("issue-1", "schema-missing") is True


def test_state_persists_across_reopening_database(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db) as store:
        store.acquire_claim("issue-1", "owner-a", lease_ttl_seconds=100, now=ts())
        store.record_execution_once("issue-1", "exec")
        store.record_comment_once("issue-1", "comment")
        assert store.schema_version() == 5

    with open_store(db) as reopened:
        claim = reopened.get_claim("issue-1")
        history = reopened.history_for_issue("issue-1")
        has_execution = reopened.has_execution("issue-1", "exec")
        has_comment = reopened.has_comment("issue-1", "comment")

    assert claim.owner_id == "owner-a"
    assert claim.expires_at == ts(100)
    assert has_execution is True
    assert has_comment is True
    assert [row["event"] for row in history] == ["claim_acquired"]


def test_multi_connection_claim_acquisition_allows_exactly_one_owner(tmp_path):
    db = tmp_path / "state.sqlite3"
    with open_store(db):
        pass

    def acquire(owner):
        with StateStore(db) as store:
            return store.acquire_claim("issue-concurrent", owner, lease_ttl_seconds=900, now=ts()).acquired

    owners = [f"owner-{i}" for i in range(12)]
    with ThreadPoolExecutor(max_workers=len(owners)) as pool:
        results = list(pool.map(acquire, owners))

    assert results.count(True) == 1
    with StateStore(db) as store:
        claim = store.get_claim("issue-concurrent")
    assert claim.owner_id in owners
    assert claim.status == ClaimStatus.CLAIMED.value
    assert claim.attempt_count == 1


def test_backoff_uses_configured_defaults_and_bounds():
    config = load_config(env={}).backoff

    assert config == BackoffConfig(initial_seconds=5, max_seconds=300, max_attempts=5)
    assert compute_backoff_delay(1, config, jitter=False).delay_seconds == 5
    assert compute_backoff_delay(4, config, jitter=False).delay_seconds == 40
    capped = compute_backoff_delay(10, config, jitter=False)
    assert capped.delay_seconds == 80
    assert capped.exhausted is True


def test_backoff_jitter_is_within_half_to_full_delay():
    delay = compute_backoff_delay(3, BackoffConfig(initial_seconds=5, max_seconds=300, max_attempts=5), rng=random.Random(1))

    assert 10 <= delay.delay_seconds <= 20
    assert delay.exhausted is False
