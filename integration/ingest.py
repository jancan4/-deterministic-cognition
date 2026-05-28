"""
Ingest model task results into the semantic candidate ledger.

Entry point: commit_ingest() or dry_run_ingest().

Governance gate (ENFORCED):
  - governance_rule candidates are suppressed by default.
  - Admission requires allow_governance_candidates=True.
  - Suppression count is always reported even in dry-run.

Stale detection:
  - Compares packet.substrate_assembly_hash against the current active assembly hash.
  - Does NOT compare assembly IDs — a new unrelated assembly in a different tag set
    must not incorrectly invalidate a valid packet.
  - If hashes differ and commit=True, the caller must pass force_stale=True.

Deduplication:
  - Exact: by candidate_id (derived from result_id + candidate_index).
  - No content-similarity or semantic deduplication.

Write boundary:
  - commit_ingest() is the only function that writes to the DB.
  - It writes to semantic_execution_runs and semantic_candidate_events only.
  - It never writes to memory_events directly.
  - All promotion requires subsequent memory-review approve.
"""
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from memory.models import VALID_EVENT_TYPES

MAX_CANDIDATES_DEFAULT = 20
GOVERNANCE_EVENT_TYPE = "governance_rule"


@dataclass
class IngestReport:
    packet_id: str
    result_id: str
    total_parsed: int
    written: int
    skipped_duplicate: int
    skipped_unknown_type: int
    skipped_governance: int
    errors: List[str] = field(default_factory=list)
    is_stale: bool = False
    dry_run: bool = True

    def summary_lines(self) -> List[str]:
        lines = [
            f"packet_id          : {self.packet_id[:16]}",
            f"result_id          : {self.result_id[:16]}",
            f"mode               : {'dry-run' if self.dry_run else 'commit'}",
            f"stale              : {self.is_stale}",
            f"total_parsed       : {self.total_parsed}",
            f"written            : {self.written}",
            f"skipped_duplicate  : {self.skipped_duplicate}",
            f"skipped_unknown_type: {self.skipped_unknown_type}",
            f"skipped_governance : {self.skipped_governance}",
        ]
        for e in self.errors:
            lines.append(f"ERROR: {e}")
        return lines


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def check_stale(db_path: str, packet_assembly_hash: str) -> Tuple[bool, Optional[str]]:
    """
    Compare packet.substrate_assembly_hash against current active assembly hash.

    Returns (is_stale, current_active_hash_or_None).
    is_stale=False if no active assembly exists (new DB — not stale by definition).
    """
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT assembly_hash FROM context_assembly_log "
                "WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return False, None
        finally:
            conn.close()
    except Exception:
        return False, None

    if row is None:
        return False, None

    current_hash = row["assembly_hash"]
    return (current_hash != packet_assembly_hash), current_hash


def dry_run_ingest(
    task_result,
    allow_governance_candidates: bool = False,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
) -> IngestReport:
    """
    Validate what would be written without writing anything.

    governance_rule candidates are counted in skipped_governance but NOT
    displayed unless allow_governance_candidates=True (behaviour matches commit).
    """
    report = IngestReport(
        packet_id=task_result.packet_id,
        result_id=task_result.result_id,
        total_parsed=len(task_result.parsed_candidates),
        written=0,
        skipped_duplicate=0,
        skipped_unknown_type=0,
        skipped_governance=0,
        dry_run=True,
    )

    if len(task_result.parsed_candidates) > max_candidates:
        report.errors.append(
            f"Candidate count {len(task_result.parsed_candidates)} exceeds "
            f"max_candidates={max_candidates}. Pass --max-candidates to override."
        )
        return report

    for cand in task_result.parsed_candidates:
        et = cand.proposed_event_type
        if et == GOVERNANCE_EVENT_TYPE and not allow_governance_candidates:
            report.skipped_governance += 1
        elif et not in VALID_EVENT_TYPES:
            report.skipped_unknown_type += 1
        else:
            report.written += 1

    return report


def commit_ingest(
    db_path: str,
    task_result,
    allow_governance_candidates: bool = False,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
    force_stale: bool = False,
) -> IngestReport:
    """
    Write valid candidates to the semantic ledger.

    Writes one semantic_execution_runs row (idempotent via INSERT OR IGNORE)
    and one semantic_candidate_events row per valid candidate.

    Never writes to memory_events. Promotion to memory requires:
      memory-review promote → memory-review approve

    Stop conditions:
      - Candidate count > max_candidates → abort, return error report
      - stale assembly hash and not force_stale → abort, return error report
      - governance_rule candidate and not allow_governance_candidates → skip (not abort)
      - Unknown event_type → skip (not abort)
    """
    report = IngestReport(
        packet_id=task_result.packet_id,
        result_id=task_result.result_id,
        total_parsed=len(task_result.parsed_candidates),
        written=0,
        skipped_duplicate=0,
        skipped_unknown_type=0,
        skipped_governance=0,
        dry_run=False,
    )

    is_stale, current_hash = check_stale(db_path, task_result.substrate_assembly_hash)
    report.is_stale = is_stale

    if is_stale and not force_stale:
        report.errors.append(
            f"Assembly hash mismatch. Packet references {task_result.substrate_assembly_hash[:12]}, "
            f"current active assembly is {(current_hash or 'none')[:12]}. "
            f"Pass --force-stale to override."
        )
        return report

    if len(task_result.parsed_candidates) > max_candidates:
        report.errors.append(
            f"Candidate count {len(task_result.parsed_candidates)} exceeds "
            f"max_candidates={max_candidates}. Pass --max-candidates to override. "
            f"No candidates written."
        )
        return report

    conn = _connect(db_path)
    try:
        from semantic.ledger import _ensure_schema, derive_candidate_id
        _ensure_schema(conn)

        now = _now()
        extraction_method = f"integration_packet:{task_result.adapter_target}"
        provenance_dict = {
            "packet_id": task_result.packet_id,
            "result_id": task_result.result_id,
            "adapter_target": task_result.adapter_target,
            "source": "model_task",
        }

        input_hash = hashlib.sha256(
            task_result.raw_output_text.encode("utf-8")
        ).hexdigest()[:16]

        conn.execute(
            """INSERT OR IGNORE INTO semantic_execution_runs (
                run_id, task_id, task_type, adapter_name, adapter_version,
                input_hash, input_text, source_id, source_span_json,
                execution_policy_json, model_metadata_json,
                raw_output_json, normalized_result_json,
                candidate_count, promoted_count, status,
                started_at, completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_result.result_id,
                task_result.task_id,
                "memory_candidate_classification",
                task_result.adapter_target,
                task_result.model_version,
                input_hash,
                task_result.raw_output_text[:4000],
                None, None,
                json.dumps({"source": "integration_packet"}, sort_keys=True),
                json.dumps({"packet_id": task_result.packet_id}, sort_keys=True),
                json.dumps({"raw": task_result.raw_output_text[:2000]}, sort_keys=True),
                json.dumps({}),
                len(task_result.parsed_candidates), 0,
                "completed",
                task_result.executed_at, task_result.executed_at,
            )
        )

        for cand in task_result.parsed_candidates:
            et = cand.proposed_event_type
            if et == GOVERNANCE_EVENT_TYPE and not allow_governance_candidates:
                report.skipped_governance += 1
                continue
            if et not in VALID_EVENT_TYPES:
                report.skipped_unknown_type += 1
                continue

            candidate_id = derive_candidate_id(task_result.result_id, cand.candidate_index)

            existing = conn.execute(
                "SELECT 1 FROM semantic_candidate_events WHERE candidate_id = ?",
                (candidate_id,)
            ).fetchone()
            if existing:
                report.skipped_duplicate += 1
                continue

            title = (cand.proposed_content[:100] if cand.proposed_content else "(no content)")
            summary = (cand.proposed_content[:500] if cand.proposed_content else "")

            conn.execute(
                """INSERT OR IGNORE INTO semantic_candidate_events (
                    candidate_id, semantic_run_id, candidate_index,
                    event_type, title, summary, evidence, source,
                    confidence, source_id, source_span_json,
                    extraction_method, provenance_json, tags_json,
                    status, promoted_memory_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_id,
                    task_result.result_id,
                    cand.candidate_index,
                    et,
                    title,
                    summary,
                    None,
                    "model_task",
                    1,
                    None, None,
                    extraction_method,
                    json.dumps(provenance_dict, sort_keys=True),
                    json.dumps(sorted(cand.proposed_tags), sort_keys=True),
                    "candidate",
                    None,
                    now,
                )
            )
            report.written += 1

        conn.commit()
    finally:
        conn.close()

    return report
