"""Durable SQLite state for Linear issue claims, dedupe, and retries.

G003 owns local coordination state only. Linear remains source of truth for queue
membership and final issue status; this module prevents duplicate local work and
records idempotency markers for later runner/comment stories.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
import json
import random
import sqlite3
from typing import Any, Callable

from .config import BackoffConfig

SCHEMA_VERSION = 5
TERMINAL_STATUSES = {"completed", "failed"}
ACTIVE_STATUSES = {"claimed", "renewed"}
Clock = Callable[[], datetime]


class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    RENEWED = "renewed"
    COMPLETED = "completed"
    FAILED = "failed"
    RELEASED = "released"
    STALE = "stale"


@dataclass(frozen=True)
class ClaimRecord:
    issue_id: str
    owner_id: str | None
    status: str
    attempt_count: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    last_error: str | None = None
    execution_key: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass(frozen=True)
class ClaimAttempt:
    acquired: bool
    claim: ClaimRecord


@dataclass(frozen=True)
class BackoffDelay:
    attempt: int
    delay_seconds: float
    exhausted: bool


@dataclass(frozen=True)
class CommentRecord:
    issue_id: str
    comment_key: str
    comment_id: str | None
    owner_id: str | None
    created_at: datetime

    @property
    def sent(self) -> bool:
        return self.comment_id is not None


class StateStore:
    """SQLite-backed durable state store.

    Each claim row is keyed by Linear issue ID. Operations that acquire, renew,
    complete, fail, reap, and dedupe are guarded in SQLite transactions so later
    daemon code can call them safely across process restarts.
    """

    def __init__(self, path: str | Path, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or _utcnow
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def migrate(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA_SQL)
            self._ensure_finalization_issue_json_column()
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def _ensure_finalization_issue_json_column(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(finalization_state)").fetchall()}
        if "issue_json" not in columns:
            self._conn.execute("ALTER TABLE finalization_state ADD COLUMN issue_json TEXT")

    def acquire_claim(
        self,
        issue_id: str,
        owner_id: str,
        *,
        lease_ttl_seconds: int,
        execution_key: str | None = None,
        allow_failed_retry: bool = False,
        now: datetime | None = None,
    ) -> ClaimAttempt:
        """Atomically acquire, resume, or reclaim an issue lease.

        A guarded BEGIN IMMEDIATE transaction serializes competing connections.
        Different owners cannot acquire an unexpired active lease; the same
        owner can resume/renew its active lease without incrementing attempts.
        Completed claims are always terminal. Failed claims are terminal unless
        callers explicitly allow a retry with a different execution key.
        """

        current_time = _coerce_utc(now or self.clock())
        expires_at = current_time + timedelta(seconds=lease_ttl_seconds)
        self._begin_immediate()
        try:
            existing = self._select_claim(issue_id)
            if existing and existing.status == ClaimStatus.COMPLETED.value:
                self._conn.commit()
                return ClaimAttempt(False, existing)
            if existing and existing.status == ClaimStatus.FAILED.value:
                if not allow_failed_retry or execution_key is None or existing.execution_key == execution_key:
                    self._conn.commit()
                    return ClaimAttempt(False, existing)
                attempt_count = existing.attempt_count + 1
                self._conn.execute(
                    """
                    UPDATE claims
                    SET owner_id = ?, status = ?, attempt_count = ?, renewed_at = ?,
                        expires_at = ?, completed_at = NULL, last_error = NULL,
                        execution_key = ?, updated_at = ?
                    WHERE issue_id = ? AND status = 'failed' AND execution_key IS NOT ?
                    """,
                    (
                        owner_id,
                        ClaimStatus.CLAIMED.value,
                        attempt_count,
                        _to_text(current_time),
                        _to_text(expires_at),
                        execution_key,
                        _to_text(current_time),
                        issue_id,
                        execution_key,
                    ),
                )
                self._insert_history(
                    issue_id,
                    owner_id,
                    "claim_acquired",
                    current_time,
                    {"attempt_count": attempt_count, "retry_of_failed_execution_key": existing.execution_key},
                )
                claim = self._require_claim(issue_id)
                self._conn.commit()
                return ClaimAttempt(True, claim)
            if existing and existing.status in ACTIVE_STATUSES and existing.expires_at > current_time:
                if existing.owner_id == owner_id:
                    self._conn.execute(
                        """
                        UPDATE claims
                        SET status = ?, renewed_at = ?, expires_at = ?,
                            execution_key = COALESCE(?, execution_key), updated_at = ?
                        WHERE issue_id = ? AND owner_id = ? AND status IN ('claimed', 'renewed')
                        """,
                        (
                            ClaimStatus.RENEWED.value,
                            _to_text(current_time),
                            _to_text(expires_at),
                            execution_key,
                            _to_text(current_time),
                            issue_id,
                            owner_id,
                        ),
                    )
                    self._insert_history(issue_id, owner_id, "claim_resumed", current_time, {"expires_at": _to_text(expires_at)})
                    claim = self._require_claim(issue_id)
                    self._conn.commit()
                    return ClaimAttempt(True, claim)
                self._conn.commit()
                return ClaimAttempt(False, existing)

            if existing:
                attempt_count = existing.attempt_count + 1
                acquired_at = existing.acquired_at
                rowcount = self._conn.execute(
                    """
                    UPDATE claims
                    SET owner_id = ?, status = ?, attempt_count = ?, renewed_at = ?,
                        expires_at = ?, completed_at = NULL, last_error = NULL,
                        execution_key = COALESCE(?, execution_key), updated_at = ?
                    WHERE issue_id = ?
                      AND status NOT IN ('completed', 'failed')
                      AND (status NOT IN ('claimed', 'renewed') OR expires_at <= ? OR owner_id = ?)
                    """,
                    (
                        owner_id,
                        ClaimStatus.CLAIMED.value,
                        attempt_count,
                        _to_text(current_time),
                        _to_text(expires_at),
                        execution_key,
                        _to_text(current_time),
                        issue_id,
                        _to_text(current_time),
                        owner_id,
                    ),
                ).rowcount
            else:
                attempt_count = 1
                acquired_at = current_time
                try:
                    self._conn.execute(
                        """
                        INSERT INTO claims (
                          issue_id, owner_id, status, attempt_count, acquired_at,
                          renewed_at, expires_at, completed_at, last_error, execution_key, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                        """,
                        (
                            issue_id,
                            owner_id,
                            ClaimStatus.CLAIMED.value,
                            attempt_count,
                            _to_text(acquired_at),
                            _to_text(current_time),
                            _to_text(expires_at),
                            execution_key,
                            _to_text(current_time),
                        ),
                    )
                    rowcount = 1
                except sqlite3.IntegrityError:
                    rowcount = 0

            if rowcount != 1:
                claim = self._require_claim(issue_id)
                self._conn.commit()
                return ClaimAttempt(False, claim)
            self._insert_history(issue_id, owner_id, "claim_acquired", current_time, {"attempt_count": attempt_count})
            claim = self._require_claim(issue_id)
            self._conn.commit()
            return ClaimAttempt(True, claim)
        except Exception:
            self._conn.rollback()
            raise

    def _require_claim(self, issue_id: str) -> ClaimRecord:
        claim = self._select_claim(issue_id)
        if claim is None:
            raise RuntimeError(f"claim row missing for issue {issue_id}")
        return claim

    def renew_claim(
        self,
        issue_id: str,
        owner_id: str,
        *,
        lease_ttl_seconds: int,
        now: datetime | None = None,
    ) -> ClaimRecord | None:
        """Renew an active lease only for its current owner."""

        current_time = _coerce_utc(now or self.clock())
        expires_at = current_time + timedelta(seconds=lease_ttl_seconds)
        with self._conn:
            rowcount = self._conn.execute(
                """
                UPDATE claims
                SET status = ?, renewed_at = ?, expires_at = ?, updated_at = ?
                WHERE issue_id = ?
                  AND owner_id = ?
                  AND status IN ('claimed', 'renewed')
                  AND expires_at > ?
                """,
                (
                    ClaimStatus.RENEWED.value,
                    _to_text(current_time),
                    _to_text(expires_at),
                    _to_text(current_time),
                    issue_id,
                    owner_id,
                    _to_text(current_time),
                ),
            ).rowcount
            if rowcount != 1:
                return None
            self._insert_history(issue_id, owner_id, "claim_renewed", current_time, {"expires_at": _to_text(expires_at)})
            return self.get_claim(issue_id)

    def complete_claim(
        self,
        issue_id: str,
        owner_id: str,
        *,
        now: datetime | None = None,
        execution_key: str | None = None,
    ) -> ClaimRecord | None:
        return self._terminal_transition(
            issue_id,
            owner_id,
            ClaimStatus.COMPLETED,
            now=now,
            error=None,
            execution_key=execution_key,
        )

    def fail_claim(
        self,
        issue_id: str,
        owner_id: str,
        *,
        error: str,
        now: datetime | None = None,
        execution_key: str | None = None,
    ) -> ClaimRecord | None:
        return self._terminal_transition(
            issue_id,
            owner_id,
            ClaimStatus.FAILED,
            now=now,
            error=error,
            execution_key=execution_key,
        )

    def release_claim(self, issue_id: str, owner_id: str, *, now: datetime | None = None) -> ClaimRecord | None:
        """Release an owned active lease without marking terminal."""

        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            rowcount = self._conn.execute(
                """
                UPDATE claims
                SET status = ?, owner_id = NULL, expires_at = ?, updated_at = ?
                WHERE issue_id = ? AND owner_id = ? AND status IN ('claimed', 'renewed')
                """,
                (ClaimStatus.RELEASED.value, _to_text(current_time), _to_text(current_time), issue_id, owner_id),
            ).rowcount
            if rowcount != 1:
                return None
            self._insert_history(issue_id, owner_id, "claim_released", current_time, {})
            return self.get_claim(issue_id)

    def reap_stale_leases(self, *, now: datetime | None = None) -> int:
        """Mark expired active leases stale and ownerless for later reclaim."""

        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            stale_rows = self._conn.execute(
                """
                SELECT issue_id, owner_id FROM claims
                WHERE status IN ('claimed', 'renewed') AND expires_at <= ?
                """,
                (_to_text(current_time),),
            ).fetchall()
            for row in stale_rows:
                self._conn.execute(
                    """
                    UPDATE claims
                    SET status = ?, owner_id = NULL, updated_at = ?
                    WHERE issue_id = ?
                    """,
                    (ClaimStatus.STALE.value, _to_text(current_time), row["issue_id"]),
                )
                self._insert_history(row["issue_id"], row["owner_id"], "claim_reaped_stale", current_time, {})
            return len(stale_rows)

    def get_claim(self, issue_id: str) -> ClaimRecord | None:
        return self._select_claim(issue_id)

    def list_claims(self) -> tuple[ClaimRecord, ...]:
        rows = self._conn.execute("SELECT * FROM claims ORDER BY issue_id").fetchall()
        return tuple(_claim_from_row(row) for row in rows)

    def record_execution_once(
        self,
        issue_id: str,
        execution_key: str,
        *,
        owner_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record an execution idempotency key; return False if already seen."""

        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            try:
                self._conn.execute(
                    """
                    INSERT INTO execution_dedupe (issue_id, execution_key, owner_id, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (issue_id, execution_key, owner_id, _json(metadata or {}), _to_text(current_time)),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def has_execution(self, issue_id: str, execution_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM execution_dedupe WHERE issue_id = ? AND execution_key = ?",
            (issue_id, execution_key),
        ).fetchone()
        return row is not None

    def ensure_comment_intent(
        self,
        issue_id: str,
        comment_key: str,
        *,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> CommentRecord:
        """Persist local intent to create/reconcile an owned Linear comment.

        A NULL ``comment_id`` means pending intent only, not sent. Callers must
        reconcile Linear by marker before creating and mark sent only after a
        comment ID is known.
        """

        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO comment_dedupe (issue_id, comment_key, comment_id, owner_id, created_at)
                VALUES (?, ?, NULL, ?, ?)
                ON CONFLICT(issue_id, comment_key) DO NOTHING
                """,
                (issue_id, comment_key, owner_id, _to_text(current_time)),
            )
        record = self.get_comment_record(issue_id, comment_key)
        assert record is not None
        return record

    def mark_comment_sent(
        self,
        issue_id: str,
        comment_key: str,
        comment_id: str,
        *,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> CommentRecord:
        """Mark a pending/existing comment intent as sent once a Linear ID is known."""

        if not comment_id:
            raise ValueError("comment_id is required to mark a comment sent")
        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO comment_dedupe (issue_id, comment_key, comment_id, owner_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(issue_id, comment_key) DO UPDATE SET
                  comment_id = COALESCE(comment_dedupe.comment_id, excluded.comment_id),
                  owner_id = COALESCE(comment_dedupe.owner_id, excluded.owner_id)
                """,
                (issue_id, comment_key, comment_id, owner_id, _to_text(current_time)),
            )
        record = self.get_comment_record(issue_id, comment_key)
        assert record is not None
        return record

    def record_comment_once(
        self,
        issue_id: str,
        comment_key: str,
        *,
        comment_id: str | None = None,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Compatibility wrapper: record a comment key as already sent.

        New outbox code should use ``ensure_comment_intent`` before Linear I/O
        and ``mark_comment_sent`` after reconciliation/create. For historical
        callers/tests that omit an ID, an empty non-NULL value means sent but ID
        unavailable.
        """

        current_time = _coerce_utc(now or self.clock())
        sent_id = comment_id if comment_id is not None else ""
        with self._conn:
            try:
                self._conn.execute(
                    """
                    INSERT INTO comment_dedupe (issue_id, comment_key, comment_id, owner_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (issue_id, comment_key, sent_id, owner_id, _to_text(current_time)),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def has_comment(self, issue_id: str, comment_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM comment_dedupe WHERE issue_id = ? AND comment_key = ? AND comment_id IS NOT NULL",
            (issue_id, comment_key),
        ).fetchone()
        return row is not None

    def get_comment_id(self, issue_id: str, comment_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT comment_id FROM comment_dedupe WHERE issue_id = ? AND comment_key = ? AND comment_id IS NOT NULL",
            (issue_id, comment_key),
        ).fetchone()
        return row["comment_id"] if row else None

    def get_comment_record(self, issue_id: str, comment_key: str) -> CommentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM comment_dedupe WHERE issue_id = ? AND comment_key = ?",
            (issue_id, comment_key),
        ).fetchone()
        return _comment_from_row(row) if row else None

    def upsert_active_work(
        self,
        issue_id: str,
        execution_key: str,
        *,
        issue: dict[str, Any],
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO active_work (issue_id, execution_key, issue_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(issue_id) DO UPDATE SET
                  execution_key = excluded.execution_key,
                  issue_json = excluded.issue_json,
                  updated_at = excluded.updated_at
                """,
                (issue_id, execution_key, _json(issue), _to_text(current_time), _to_text(current_time)),
            )
            self._insert_history(issue_id, owner_id, "active_work_persisted", current_time, {})

    def list_active_work(self) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            """
            SELECT aw.*, claims.status AS claim_status, claims.owner_id AS claim_owner_id
            FROM active_work aw
            JOIN claims ON claims.issue_id = aw.issue_id
            LEFT JOIN finalization_state fs
              ON fs.issue_id = aw.issue_id AND fs.execution_key = aw.execution_key
            WHERE claims.status IN ('claimed', 'renewed', 'released', 'stale')
              AND fs.issue_id IS NULL
            ORDER BY aw.updated_at, aw.issue_id
            """
        ).fetchall()
        return tuple(
            {
                "issue_id": row["issue_id"],
                "execution_key": row["execution_key"],
                "issue": json.loads(row["issue_json"]),
                "claim_status": row["claim_status"],
                "claim_owner_id": row["claim_owner_id"],
            }
            for row in rows
        )

    def record_result_comment_finalization(
        self,
        issue_id: str,
        execution_key: str,
        comment_key: str,
        comment_id: str,
        *,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not comment_id:
            raise ValueError("comment_id is required to finalize result comment")
        current_time = _coerce_utc(now or self.clock())
        self._begin_immediate()
        try:
            self._conn.execute(
                """
                INSERT INTO comment_dedupe (issue_id, comment_key, comment_id, owner_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(issue_id, comment_key) DO UPDATE SET
                  comment_id = COALESCE(comment_dedupe.comment_id, excluded.comment_id),
                  owner_id = COALESCE(comment_dedupe.owner_id, excluded.owner_id)
                """,
                (issue_id, comment_key, comment_id, owner_id, _to_text(current_time)),
            )
            row = self._conn.execute(
                "SELECT comment_id FROM comment_dedupe WHERE issue_id = ? AND comment_key = ?",
                (issue_id, comment_key),
            ).fetchone()
            stored_comment_id = row["comment_id"] if row else comment_id
            self._conn.execute(
                """
                UPDATE finalization_state
                SET result_comment_id = COALESCE(result_comment_id, ?), updated_at = ?
                WHERE issue_id = ? AND execution_key = ?
                """,
                (stored_comment_id, _to_text(current_time), issue_id, execution_key),
            )
            self._insert_history(issue_id, owner_id, "finalization_comment_posted", current_time, {"comment_id": stored_comment_id})
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_finalization(self, issue_id: str, execution_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM finalization_state WHERE issue_id = ? AND execution_key = ?",
            (issue_id, execution_key),
        ).fetchone()
        if row is None:
            return None
        return {
            "issue_id": row["issue_id"],
            "execution_key": row["execution_key"],
            "result": json.loads(row["result_json"]),
            "issue": json.loads(row["issue_json"]) if row["issue_json"] else None,
            "result_comment_id": row["result_comment_id"],
            "done_transitioned": bool(row["done_transitioned"]),
        }

    def upsert_finalization(
        self,
        issue_id: str,
        execution_key: str,
        *,
        result: dict[str, Any],
        issue: dict[str, Any] | None = None,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO finalization_state (
                  issue_id, execution_key, result_json, issue_json, result_comment_id, done_transitioned, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, 0, ?, ?)
                ON CONFLICT(issue_id, execution_key) DO UPDATE SET
                  result_json = excluded.result_json,
                  issue_json = COALESCE(excluded.issue_json, finalization_state.issue_json),
                  updated_at = excluded.updated_at
                """,
                (issue_id, execution_key, _json(result), _json(issue) if issue else None, _to_text(current_time), _to_text(current_time)),
            )
            self._insert_history(issue_id, owner_id, "finalization_pending", current_time, {})

    def mark_finalization_comment(
        self,
        issue_id: str,
        execution_key: str,
        comment_id: str | None,
        *,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            self._conn.execute(
                """
                UPDATE finalization_state
                SET result_comment_id = COALESCE(result_comment_id, ?), updated_at = ?
                WHERE issue_id = ? AND execution_key = ?
                """,
                (comment_id, _to_text(current_time), issue_id, execution_key),
            )
            self._insert_history(issue_id, owner_id, "finalization_comment_posted", current_time, {"comment_id": comment_id})

    def mark_finalization_done(
        self,
        issue_id: str,
        execution_key: str,
        *,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            self._conn.execute(
                """
                UPDATE finalization_state
                SET done_transitioned = 1, updated_at = ?
                WHERE issue_id = ? AND execution_key = ?
                """,
                (_to_text(current_time), issue_id, execution_key),
            )
            self._insert_history(issue_id, owner_id, "finalization_done_transitioned", current_time, {})

    def list_pending_finalizations(self) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            """
            SELECT fs.*, claims.status AS claim_status, claims.owner_id AS claim_owner_id
            FROM finalization_state fs
            LEFT JOIN claims ON claims.issue_id = fs.issue_id
            WHERE fs.done_transitioned = 0
               OR claims.status IS NULL
               OR claims.status != 'completed'
            ORDER BY fs.updated_at, fs.issue_id
            """
        ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            pending.append(
                {
                    "issue_id": row["issue_id"],
                    "execution_key": row["execution_key"],
                    "result": json.loads(row["result_json"]),
                    "issue": json.loads(row["issue_json"]) if row["issue_json"] else None,
                    "result_comment_id": row["result_comment_id"],
                    "done_transitioned": bool(row["done_transitioned"]),
                    "claim_status": row["claim_status"],
                    "claim_owner_id": row["claim_owner_id"],
                }
            )
        return tuple(pending)

    def history_for_issue(self, issue_id: str) -> tuple[sqlite3.Row, ...]:
        rows = self._conn.execute(
            "SELECT * FROM claim_history WHERE issue_id = ? ORDER BY id",
            (issue_id,),
        ).fetchall()
        return tuple(rows)

    def _terminal_transition(
        self,
        issue_id: str,
        owner_id: str,
        status: ClaimStatus,
        *,
        now: datetime | None,
        error: str | None,
        execution_key: str | None,
    ) -> ClaimRecord | None:
        current_time = _coerce_utc(now or self.clock())
        with self._conn:
            rowcount = self._conn.execute(
                """
                UPDATE claims
                SET status = ?, completed_at = ?, renewed_at = ?, expires_at = ?,
                    last_error = ?, execution_key = COALESCE(?, execution_key), updated_at = ?
                WHERE issue_id = ?
                  AND owner_id = ?
                  AND status IN ('claimed', 'renewed')
                  AND expires_at > ?
                """,
                (
                    status.value,
                    _to_text(current_time),
                    _to_text(current_time),
                    _to_text(current_time),
                    error,
                    execution_key,
                    _to_text(current_time),
                    issue_id,
                    owner_id,
                    _to_text(current_time),
                ),
            ).rowcount
            if rowcount != 1:
                return None
            self._insert_history(issue_id, owner_id, f"claim_{status.value}", current_time, {"error": error} if error else {})
            return self.get_claim(issue_id)

    def _begin_immediate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _select_claim(self, issue_id: str) -> ClaimRecord | None:
        row = self._conn.execute("SELECT * FROM claims WHERE issue_id = ?", (issue_id,)).fetchone()
        return _claim_from_row(row) if row else None

    def _insert_history(self, issue_id: str, owner_id: str | None, event: str, at: datetime, details: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO claim_history (issue_id, owner_id, event, details_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (issue_id, owner_id, event, _json(details), _to_text(at)),
        )


def compute_backoff_delay(
    attempt: int,
    config: BackoffConfig,
    *,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> BackoffDelay:
    """Return bounded exponential backoff delay for 1-indexed attempts."""

    if attempt <= 0:
        raise ValueError("attempt must be positive")
    exhausted = attempt > config.max_attempts
    capped_attempt = max(1, min(attempt, config.max_attempts))
    raw_delay = min(config.max_seconds, config.initial_seconds * (2 ** (capped_attempt - 1)))
    if jitter:
        generator = rng or random.Random()
        delay = generator.uniform(raw_delay / 2, raw_delay)
    else:
        delay = float(raw_delay)
    return BackoffDelay(attempt=attempt, delay_seconds=delay, exhausted=exhausted)


def _comment_from_row(row: sqlite3.Row) -> CommentRecord:
    return CommentRecord(
        issue_id=row["issue_id"],
        comment_key=row["comment_key"],
        comment_id=row["comment_id"],
        owner_id=row["owner_id"],
        created_at=_from_text(row["created_at"]),
    )


def _claim_from_row(row: sqlite3.Row) -> ClaimRecord:
    return ClaimRecord(
        issue_id=row["issue_id"],
        owner_id=row["owner_id"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        acquired_at=_from_text(row["acquired_at"]),
        renewed_at=_from_text(row["renewed_at"]),
        expires_at=_from_text(row["expires_at"]),
        completed_at=_from_text(row["completed_at"]) if row["completed_at"] else None,
        last_error=row["last_error"],
        execution_key=row["execution_key"],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_text(value: datetime) -> str:
    return _coerce_utc(value).isoformat()


def _from_text(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, default=str)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  issue_id TEXT PRIMARY KEY,
  owner_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('claimed', 'renewed', 'completed', 'failed', 'released', 'stale')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  acquired_at TEXT NOT NULL,
  renewed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  completed_at TEXT,
  last_error TEXT,
  execution_key TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_claims_status_expires ON claims(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_claims_owner_status ON claims(owner_id, status);

CREATE TABLE IF NOT EXISTS claim_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id TEXT NOT NULL,
  owner_id TEXT,
  event TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(issue_id) REFERENCES claims(issue_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_claim_history_issue ON claim_history(issue_id, id);

CREATE TABLE IF NOT EXISTS execution_dedupe (
  issue_id TEXT NOT NULL,
  execution_key TEXT NOT NULL,
  owner_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY(issue_id, execution_key)
);

CREATE TABLE IF NOT EXISTS comment_dedupe (
  issue_id TEXT NOT NULL,
  comment_key TEXT NOT NULL,
  comment_id TEXT,
  owner_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(issue_id, comment_key)
);


CREATE TABLE IF NOT EXISTS active_work (
  issue_id TEXT PRIMARY KEY,
  execution_key TEXT NOT NULL,
  issue_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(issue_id) REFERENCES claims(issue_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS finalization_state (
  issue_id TEXT NOT NULL,
  execution_key TEXT NOT NULL,
  result_json TEXT NOT NULL,
  issue_json TEXT,
  result_comment_id TEXT,
  done_transitioned INTEGER NOT NULL DEFAULT 0 CHECK (done_transitioned IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(issue_id, execution_key),
  FOREIGN KEY(issue_id) REFERENCES claims(issue_id) ON DELETE CASCADE
);
"""
