"""
Semantic execution ledger.

Persists every semantic extraction run and its candidate outputs before any
memory commit decision is made. This makes generated-but-not-promoted
candidates reconstructible from the source system even when --commit is not
passed.

Tables
------
semantic_execution_runs   — one row per run_semantic_task() call
semantic_candidate_events — one row per CandidateMemoryEvent generated

Provenance chain
----------------
  source_documents.source_id
       ↓
  ingestion_runs.source_id
       ↓
  semantic_execution_runs.source_id  (this module)
       ↓
  semantic_candidate_events.semantic_run_id
       ↓ (on promotion)
  memory_events.id   (via promoted_memory_id)

ID derivation
-------------
  run_id        = request_id from ModelExecutionResult
                  sha256(adapter_name + NUL + adapter_version + NUL + task_type + NUL + input_text)[:16]
  candidate_id  = sha256(run_id + NUL + str(candidate_index))[:16]
  input_hash    = sha256(input_text)[:16]

input_hash lets you find all runs for a given text across adapters; run_id
differentiates them by adapter identity.

Continuity bundle portability
------------------------------
As of schema 1.1, continuity/exporter.py exports semantic_execution_runs and
semantic_candidate_events for all promoted candidates. Import via
continuity/importer.py restores the full semantic provenance chain.

  - promoted memory_events: portable (schema 1.0+)
  - evidence strings (semantic:<adapter> | run:<run_id> | candidate:<id>): portable (1.0+)
  - full semantic provenance (normalized_result, source_span, provenance): portable (1.1+)
  - unpromoted/rejected candidates: source-system-local only (never bundled)

Governing invariants
--------------------
  - No database write occurs unless an explicit record_run() or
    promote_candidate() call is made.
  - record_run() is idempotent: same run_id → INSERT OR IGNORE (skip).
  - promote_candidate() is the only function that writes to memory_events.
    It calls memory.service.add_memory_event() with status='unresolved'.
  - Candidate status transitions: candidate → promoted | rejected only.
    No other transitions are valid.
  - All writes use PRAGMA journal_mode=WAL and PRAGMA foreign_keys=ON.
"""
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from semantic.pipeline import SemanticPipelineResult

VALID_RUN_STATUSES = ('completed', 'failed', 'partial')
VALID_CANDIDATE_STATUSES = ('candidate', 'promoted', 'rejected')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_execution_runs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                 TEXT    NOT NULL UNIQUE,
    task_id                TEXT    NOT NULL,
    task_type              TEXT    NOT NULL,
    adapter_name           TEXT    NOT NULL,
    adapter_version        TEXT    NOT NULL,
    input_hash             TEXT    NOT NULL,
    input_text             TEXT    NOT NULL,
    source_id              TEXT,
    source_span_json       TEXT,
    execution_policy_json  TEXT    NOT NULL DEFAULT '{}',
    model_metadata_json    TEXT    NOT NULL DEFAULT '{}',
    raw_output_json        TEXT,
    normalized_result_json TEXT    NOT NULL,
    candidate_count        INTEGER NOT NULL DEFAULT 0,
    promoted_count         INTEGER NOT NULL DEFAULT 0,
    status                 TEXT    NOT NULL CHECK (status IN (
                               'completed','failed','partial'
                           )),
    started_at             TEXT    NOT NULL,
    completed_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sem_runs_task_type  ON semantic_execution_runs(task_type);
CREATE INDEX IF NOT EXISTS idx_sem_runs_adapter    ON semantic_execution_runs(adapter_name);
CREATE INDEX IF NOT EXISTS idx_sem_runs_source_id  ON semantic_execution_runs(source_id);
CREATE INDEX IF NOT EXISTS idx_sem_runs_input_hash ON semantic_execution_runs(input_hash);
CREATE INDEX IF NOT EXISTS idx_sem_runs_started_at ON semantic_execution_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_sem_runs_status     ON semantic_execution_runs(status);

CREATE TABLE IF NOT EXISTS semantic_candidate_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id       TEXT    NOT NULL UNIQUE,
    semantic_run_id    TEXT    NOT NULL,
    candidate_index    INTEGER NOT NULL CHECK (candidate_index >= 0),
    event_type         TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    summary            TEXT    NOT NULL,
    evidence           TEXT,
    source             TEXT    NOT NULL,
    confidence         INTEGER NOT NULL CHECK (confidence >= 1 AND confidence <= 5),
    source_id          TEXT,
    source_span_json   TEXT,
    extraction_method  TEXT    NOT NULL,
    provenance_json    TEXT    NOT NULL,
    tags_json          TEXT    NOT NULL DEFAULT '[]',
    status             TEXT    NOT NULL CHECK (status IN (
                           'candidate','promoted','rejected'
                       )),
    promoted_memory_id INTEGER,
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sem_cands_run_id       ON semantic_candidate_events(semantic_run_id);
CREATE INDEX IF NOT EXISTS idx_sem_cands_status       ON semantic_candidate_events(status);
CREATE INDEX IF NOT EXISTS idx_sem_cands_promoted_mid ON semantic_candidate_events(promoted_memory_id);
CREATE INDEX IF NOT EXISTS idx_sem_cands_source_id    ON semantic_candidate_events(source_id);
"""


class LedgerError(ValueError):
    pass


class LedgerNotFoundError(KeyError):
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def derive_candidate_id(run_id: str, candidate_index: int) -> str:
    """Deterministic candidate_id: sha256(run_id + NUL + str(index))[:16]."""
    raw = f"{run_id}\x00{candidate_index}".encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def _derive_input_hash(input_text: str) -> str:
    """sha256(input_text)[:16] — adapter-independent fingerprint."""
    return hashlib.sha256(input_text.encode('utf-8')).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SemanticExecutionRun:
    run_id: str
    task_id: str
    task_type: str
    adapter_name: str
    adapter_version: str
    input_hash: str
    input_text: str
    source_id: Optional[str]
    source_span: Optional[dict]
    execution_policy: dict
    model_metadata: dict
    raw_output: Optional[dict]
    normalized_result: dict
    candidate_count: int
    promoted_count: int
    status: str
    started_at: str
    completed_at: str

    def to_dict(self) -> dict:
        return {
            'run_id': self.run_id,
            'task_id': self.task_id,
            'task_type': self.task_type,
            'adapter_name': self.adapter_name,
            'adapter_version': self.adapter_version,
            'input_hash': self.input_hash,
            'input_text': self.input_text,
            'source_id': self.source_id,
            'source_span': self.source_span,
            'execution_policy': self.execution_policy,
            'model_metadata': self.model_metadata,
            'raw_output': self.raw_output,
            'normalized_result': self.normalized_result,
            'candidate_count': self.candidate_count,
            'promoted_count': self.promoted_count,
            'status': self.status,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
        }

    @classmethod
    def from_row(cls, row) -> 'SemanticExecutionRun':
        return cls(
            run_id=row['run_id'],
            task_id=row['task_id'],
            task_type=row['task_type'],
            adapter_name=row['adapter_name'],
            adapter_version=row['adapter_version'],
            input_hash=row['input_hash'],
            input_text=row['input_text'],
            source_id=row['source_id'],
            source_span=json.loads(row['source_span_json']) if row['source_span_json'] else None,
            execution_policy=json.loads(row['execution_policy_json'] or '{}'),
            model_metadata=json.loads(row['model_metadata_json'] or '{}'),
            raw_output=json.loads(row['raw_output_json']) if row['raw_output_json'] else None,
            normalized_result=json.loads(row['normalized_result_json']),
            candidate_count=row['candidate_count'],
            promoted_count=row['promoted_count'],
            status=row['status'],
            started_at=row['started_at'],
            completed_at=row['completed_at'],
        )


@dataclass
class SemanticCandidateEvent:
    candidate_id: str
    semantic_run_id: str
    candidate_index: int
    event_type: str
    title: str
    summary: str
    evidence: Optional[str]
    source: str
    confidence: int
    source_id: Optional[str]
    source_span: Optional[dict]
    extraction_method: str
    provenance: dict
    tags: List[str]
    status: str
    promoted_memory_id: Optional[int]
    created_at: str

    def to_dict(self) -> dict:
        return {
            'candidate_id': self.candidate_id,
            'semantic_run_id': self.semantic_run_id,
            'candidate_index': self.candidate_index,
            'event_type': self.event_type,
            'title': self.title,
            'summary': self.summary,
            'evidence': self.evidence,
            'source': self.source,
            'confidence': self.confidence,
            'source_id': self.source_id,
            'source_span': self.source_span,
            'extraction_method': self.extraction_method,
            'provenance': self.provenance,
            'tags': list(self.tags),
            'status': self.status,
            'promoted_memory_id': self.promoted_memory_id,
            'created_at': self.created_at,
        }

    @classmethod
    def from_row(cls, row) -> 'SemanticCandidateEvent':
        return cls(
            candidate_id=row['candidate_id'],
            semantic_run_id=row['semantic_run_id'],
            candidate_index=row['candidate_index'],
            event_type=row['event_type'],
            title=row['title'],
            summary=row['summary'],
            evidence=row['evidence'],
            source=row['source'],
            confidence=row['confidence'],
            source_id=row['source_id'],
            source_span=json.loads(row['source_span_json']) if row['source_span_json'] else None,
            extraction_method=row['extraction_method'],
            provenance=json.loads(row['provenance_json']),
            tags=json.loads(row['tags_json'] or '[]'),
            status=row['status'],
            promoted_memory_id=row['promoted_memory_id'],
            created_at=row['created_at'],
        )


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def init_ledger(db_path: str) -> None:
    """Create ledger tables and indexes if they do not exist. Idempotent."""
    with _connect(db_path) as conn:
        _ensure_schema(conn)


# ---------------------------------------------------------------------------
# record_run
# ---------------------------------------------------------------------------

def record_run(
    db_path: str,
    pipeline_result: 'SemanticPipelineResult',
    execution_policy: Optional[dict] = None,
    model_metadata: Optional[dict] = None,
) -> SemanticExecutionRun:
    """
    Persist one SemanticPipelineResult to the ledger.

    Writes one row to semantic_execution_runs and one row to
    semantic_candidate_events for each candidate in pipeline_result.candidates.

    Idempotent: if run_id already exists, the existing row is returned
    unchanged without any additional writes. Candidates are likewise skipped
    on duplicate candidate_id.

    Returns the SemanticExecutionRun (existing or newly inserted).
    """
    er = pipeline_result.execution_result
    task = pipeline_result.task
    sr = pipeline_result.semantic_result

    run_id = er.request_id
    input_hash = _derive_input_hash(task.input_text)
    policy_json = json.dumps(execution_policy or {}, sort_keys=True)
    metadata_json = json.dumps(model_metadata or {}, sort_keys=True)
    normalized_json = json.dumps(sr.to_dict(), sort_keys=True) if sr else json.dumps({})
    source_span_json = (
        json.dumps(task.source_span.to_dict(), sort_keys=True)
        if task.source_span else None
    )
    run_status = 'completed' if pipeline_result.success else 'failed'
    candidates = pipeline_result.candidates

    with _connect(db_path) as conn:
        _ensure_schema(conn)

        # Insert run row — skip silently if already exists (idempotent)
        conn.execute(
            """
            INSERT OR IGNORE INTO semantic_execution_runs (
                run_id, task_id, task_type, adapter_name, adapter_version,
                input_hash, input_text, source_id, source_span_json,
                execution_policy_json, model_metadata_json,
                raw_output_json, normalized_result_json,
                candidate_count, promoted_count, status,
                started_at, completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, task.task_id, task.task_type,
                er.adapter_name, er.adapter_version,
                input_hash, task.input_text,
                task.source_id, source_span_json,
                policy_json, metadata_json,
                None,  # raw_output_json — reserved for real adapters
                normalized_json,
                len(candidates), 0,
                run_status,
                er.started_at, er.completed_at,
            ),
        )

        # Insert one candidate row per candidate
        now = _now()
        for idx, cand in enumerate(candidates):
            candidate_id = derive_candidate_id(run_id, idx)
            prov_dict = {}
            if sr and sr.provenance:
                prov_dict = sr.provenance.to_dict()
            cand_span_json = (
                json.dumps(cand.source_span.to_dict(), sort_keys=True)
                if cand.source_span else None
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO semantic_candidate_events (
                    candidate_id, semantic_run_id, candidate_index,
                    event_type, title, summary, evidence, source,
                    confidence, source_id, source_span_json,
                    extraction_method, provenance_json, tags_json,
                    status, promoted_memory_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    candidate_id, run_id, idx,
                    cand.event_type, cand.title, cand.summary,
                    cand.evidence, cand.source,
                    cand.confidence, task.source_id, cand_span_json,
                    cand.extraction_method,
                    json.dumps(prov_dict, sort_keys=True),
                    json.dumps(sorted(cand.tags), sort_keys=True),
                    'candidate', None, now,
                ),
            )

        row = conn.execute(
            "SELECT * FROM semantic_execution_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return SemanticExecutionRun.from_row(row)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_run(db_path: str, run_id: str) -> Optional[SemanticExecutionRun]:
    """Return the SemanticExecutionRun for this run_id, or None."""
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM semantic_execution_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return SemanticExecutionRun.from_row(row) if row else None


def list_runs(
    db_path: str,
    adapter_name: Optional[str] = None,
    task_type: Optional[str] = None,
    source_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[SemanticExecutionRun]:
    """List semantic execution runs, newest first."""
    if status is not None and status not in VALID_RUN_STATUSES:
        raise LedgerError(f"Invalid run status {status!r}. Must be one of: {VALID_RUN_STATUSES}")

    clauses: List[str] = []
    params: list = []

    if adapter_name:
        clauses.append("adapter_name = ?")
        params.append(adapter_name)
    if task_type:
        clauses.append("task_type = ?")
        params.append(task_type)
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT * FROM semantic_execution_runs {where} ORDER BY started_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [SemanticExecutionRun.from_row(r) for r in rows]


def get_candidate(db_path: str, candidate_id: str) -> Optional[SemanticCandidateEvent]:
    """Return the SemanticCandidateEvent for this candidate_id, or None."""
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM semantic_candidate_events WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return SemanticCandidateEvent.from_row(row) if row else None


def list_candidates(
    db_path: str,
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    source_id: Optional[str] = None,
    limit: int = 100,
) -> List[SemanticCandidateEvent]:
    """List semantic candidate events. Newest-created first within each run."""
    if status is not None and status not in VALID_CANDIDATE_STATUSES:
        raise LedgerError(
            f"Invalid candidate status {status!r}. Must be one of: {VALID_CANDIDATE_STATUSES}"
        )

    clauses: List[str] = []
    params: list = []

    if run_id:
        clauses.append("semantic_run_id = ?")
        params.append(run_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT * FROM semantic_candidate_events {where} "
            f"ORDER BY semantic_run_id ASC, candidate_index ASC LIMIT ?",
            params,
        ).fetchall()
        return [SemanticCandidateEvent.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# update_candidate_status (internal ledger transition)
# ---------------------------------------------------------------------------

def update_candidate_status(
    db_path: str,
    candidate_id: str,
    new_status: str,
    promoted_memory_id: Optional[int] = None,
) -> SemanticCandidateEvent:
    """
    Transition a candidate's ledger status.

    Valid transitions:
      candidate → promoted  (requires promoted_memory_id)
      candidate → rejected  (promoted_memory_id must be None)

    Any other transition raises LedgerError.
    """
    if new_status not in ('promoted', 'rejected'):
        raise LedgerError(
            f"update_candidate_status: new_status must be 'promoted' or 'rejected', "
            f"got {new_status!r}"
        )
    if new_status == 'promoted' and promoted_memory_id is None:
        raise LedgerError(
            "update_candidate_status: promoted_memory_id is required when new_status='promoted'"
        )
    if new_status == 'rejected' and promoted_memory_id is not None:
        raise LedgerError(
            "update_candidate_status: promoted_memory_id must be None when new_status='rejected'"
        )

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM semantic_candidate_events WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise LedgerNotFoundError(
                f"Candidate {candidate_id!r} not found in ledger"
            )
        current_status = row['status']
        if current_status != 'candidate':
            raise LedgerError(
                f"Cannot transition candidate {candidate_id!r} from "
                f"{current_status!r} to {new_status!r}. "
                f"Only 'candidate' status can be transitioned."
            )
        conn.execute(
            "UPDATE semantic_candidate_events "
            "SET status = ?, promoted_memory_id = ? "
            "WHERE candidate_id = ?",
            (new_status, promoted_memory_id, candidate_id),
        )
        if new_status == 'promoted':
            conn.execute(
                "UPDATE semantic_execution_runs "
                "SET promoted_count = promoted_count + 1 "
                "WHERE run_id = ?",
                (row['semantic_run_id'],),
            )
        row = conn.execute(
            "SELECT * FROM semantic_candidate_events WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return SemanticCandidateEvent.from_row(row)


# ---------------------------------------------------------------------------
# promote_candidate — the single governed write boundary for semantic promotion
# ---------------------------------------------------------------------------

def promote_candidate(
    db_path: str,
    candidate_id: str,
    approved_by: str,
) -> int:
    """
    The single governed write boundary for semantic-to-memory promotion.

    Steps:
      1. Fetch candidate from ledger (raises LedgerNotFoundError if missing).
      2. Validate candidate is status='candidate' (raises LedgerError otherwise).
      3. Call memory.service.add_memory_event(status='unresolved').
      4. Write evidence string into memory event: references adapter, run_id,
         candidate_id for source-system provenance lookup.
      5. Call update_candidate_status(candidate_id, 'promoted', promoted_memory_id).

    Returns the memory_event id.

    On any failure after step 3 (ledger update fails), the memory event has
    already been written. The caller should handle this by retrying the ledger
    update. In practice this window is tiny (in-process, same connection).

    Known limitation: the memory event's provenance (evidence string referencing
    run_id/candidate_id) is portable via continuity bundles; the ledger rows
    are not yet included in bundles. See module docstring.
    """
    if not approved_by or not approved_by.strip():
        raise LedgerError("approved_by must not be empty")

    cand = get_candidate(db_path, candidate_id)
    if cand is None:
        raise LedgerNotFoundError(f"Candidate {candidate_id!r} not found in ledger")
    if cand.status != 'candidate':
        raise LedgerError(
            f"Candidate {candidate_id!r} has status {cand.status!r}; "
            f"only 'candidate' can be promoted."
        )

    from memory import service as _mem_service

    evidence = (
        f"semantic:{cand.extraction_method} "
        f"| run:{cand.semantic_run_id} "
        f"| candidate:{cand.candidate_id}"
    )

    event = _mem_service.add_memory_event(
        db_path=db_path,
        event_type=cand.event_type,
        title=cand.title,
        summary=cand.summary,
        source=cand.source,
        confidence=cand.confidence,
        status='unresolved',
        created_by=approved_by,
        evidence=evidence,
        tags=list(cand.tags),
    )

    update_candidate_status(db_path, candidate_id, 'promoted', event.id)
    return event.id


