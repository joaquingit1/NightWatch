from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Tiredness = Literal["energized", "tired"]
IntakeStatus = Literal["pending", "acknowledged", "escorted", "declined"]


@dataclass(frozen=True)
class IntakeResponse:
    response_id: str
    session_id: str
    interaction_id: str | None
    created_ts: float
    consent_analysis: bool
    tiredness: Tiredness
    wants_escort: bool
    status: IntakeStatus
    name_alias: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_id": self.response_id,
            "session_id": self.session_id,
            "interaction_id": self.interaction_id,
            "created_ts": self.created_ts,
            "consent_analysis": self.consent_analysis,
            "tiredness": self.tiredness,
            "wants_escort": self.wants_escort,
            "status": self.status,
            "name_alias": self.name_alias,
            "source": self.source,
        }


class IntakeDatabase:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS intake_responses (
                    response_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    interaction_id TEXT,
                    created_ts REAL NOT NULL,
                    consent_analysis INTEGER NOT NULL,
                    tiredness TEXT NOT NULL,
                    wants_escort INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    name_alias TEXT,
                    source TEXT NOT NULL DEFAULT 'qr_form'
                );
                CREATE INDEX IF NOT EXISTS idx_intake_created
                    ON intake_responses (created_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_intake_pending_escort
                    ON intake_responses (status, wants_escort, created_ts DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(intake_responses)"
                ).fetchall()
            }
            if "interaction_id" not in columns:
                conn.execute(
                    "ALTER TABLE intake_responses ADD COLUMN interaction_id TEXT"
                )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intake_interaction
                    ON intake_responses (interaction_id, created_ts DESC)
                """
            )

    def count_recent_submissions(self, session_id: str, window_seconds: float = 3600.0) -> int:
        cutoff = time.time() - window_seconds
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM intake_responses
                WHERE session_id = ? AND created_ts >= ?
                """,
                (session_id, cutoff),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def insert(
        self,
        *,
        session_id: str,
        interaction_id: str | None = None,
        consent_analysis: bool,
        tiredness: Tiredness,
        wants_escort: bool,
        name_alias: str | None = None,
        source: str = "qr_form",
    ) -> IntakeResponse:
        # UI-design intentionally contains only the tiredness and escort
        # questions. Keep the legacy consent column for compatibility, but do
        # not erase the two answers the visitor actually selected.
        status: IntakeStatus = "pending"

        record = IntakeResponse(
            response_id=uuid.uuid4().hex,
            session_id=session_id,
            interaction_id=interaction_id,
            created_ts=time.time(),
            consent_analysis=consent_analysis,
            tiredness=tiredness,
            wants_escort=wants_escort,
            status=status,
            name_alias=name_alias,
            source=source,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO intake_responses (
                    response_id, session_id, interaction_id, created_ts, consent_analysis,
                    tiredness, wants_escort, status, name_alias, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.response_id,
                    record.session_id,
                    record.interaction_id,
                    record.created_ts,
                    int(record.consent_analysis),
                    record.tiredness,
                    int(record.wants_escort),
                    record.status,
                    record.name_alias,
                    record.source,
                ),
            )
        return record

    def list_latest(self, limit: int = 20) -> list[IntakeResponse]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM intake_responses
                ORDER BY created_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_pending_escort(self, limit: int = 20) -> list[IntakeResponse]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM intake_responses
                WHERE wants_escort = 1 AND status = 'pending'
                ORDER BY created_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def update_status(self, response_id: str, status: IntakeStatus) -> IntakeResponse | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE intake_responses SET status = ? WHERE response_id = ?",
                (status, response_id),
            )
            row = conn.execute(
                "SELECT * FROM intake_responses WHERE response_id = ?",
                (response_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> IntakeResponse:
        return IntakeResponse(
            response_id=row["response_id"],
            session_id=row["session_id"],
            interaction_id=row["interaction_id"],
            created_ts=float(row["created_ts"]),
            consent_analysis=bool(row["consent_analysis"]),
            tiredness=row["tiredness"],
            wants_escort=bool(row["wants_escort"]),
            status=row["status"],
            name_alias=row["name_alias"],
            source=row["source"],
        )


def routing_hint(
    consent_analysis: bool,
    wants_escort: bool,
    tiredness: Tiredness = "tired",
) -> Literal["escort", "observe", "declined"]:
    del consent_analysis  # retained in the API solely for older clients
    if wants_escort and tiredness == "tired":
        return "escort"
    return "observe"
