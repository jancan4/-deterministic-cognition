from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Configuration defaults (overridable per-call, never global singletons)
# ---------------------------------------------------------------------------

STALE_WARNING_DAYS = 90
STALE_CRITICAL_DAYS = 180
UNRESOLVED_WARNING_DAYS = 30
UNRESOLVED_CRITICAL_DAYS = 60
MAX_RELATED_FANOUT = 10
LOW_CONFIDENCE_ACTIVE_THRESHOLD = 2  # confidence <= this in active/accepted triggers detection

_ACTIVE_STATUSES = ('active', 'accepted')

# Deterministic sort order: lower integer = higher priority in report
_SEVERITY_ORDER: Dict[str, int] = {'critical': 0, 'warning': 1, 'info': 2}


# ---------------------------------------------------------------------------
# GovernanceIssue and GovernanceReport
# ---------------------------------------------------------------------------

@dataclass
class GovernanceIssue:
    issue_type: str
    severity: str        # 'info' | 'warning' | 'critical'
    memory_id: int
    title: str
    rationale: str
    recommended_action: str
    metadata: Optional[Dict] = None

    def to_dict(self) -> dict:
        return {
            'issue_type': self.issue_type,
            'severity': self.severity,
            'memory_id': self.memory_id,
            'title': self.title,
            'rationale': self.rationale,
            'recommended_action': self.recommended_action,
            'metadata': self.metadata,
        }


@dataclass
class GovernanceReport:
    generated_at: str
    total_events: int
    issues: List[GovernanceIssue] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == 'critical')

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == 'warning')

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == 'info')

    def to_dict(self) -> dict:
        return {
            'generated_at': self.generated_at,
            'total_events': self.total_events,
            'critical_count': self.critical_count,
            'warning_count': self.warning_count,
            'info_count': self.info_count,
            'issues': [i.to_dict() for i in self.issues],
        }


# ---------------------------------------------------------------------------
# Retrieval filter (pure function — no DB access)
# ---------------------------------------------------------------------------

@dataclass
class RetrievalFilter:
    exclude_deprecated: bool = False
    suppress_unresolved: bool = False
    min_confidence_active: Optional[int] = None


def filter_events(events: list, governance_filter: RetrievalFilter) -> list:
    """Pure function. Applies governance-aware filtering to a ScoredEvent list.

    Does NOT silently hide governance issues — callers must inspect the
    governance report separately if they need issue visibility.
    """
    result = []
    for scored in events:
        ev = scored.event
        if governance_filter.exclude_deprecated and ev.status == 'deprecated':
            continue
        if governance_filter.suppress_unresolved and ev.status == 'unresolved':
            continue
        if (
            governance_filter.min_confidence_active is not None
            and ev.status in _ACTIVE_STATUSES
            and ev.confidence < governance_filter.min_confidence_active
        ):
            continue
        result.append(scored)
    return result


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _cutoff(days: int) -> str:
    """ISO-8601 UTC string for (now - days). Negative days produce a future cutoff."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------

def detect_stale_memory(
    db_path: str,
    warning_days: int = STALE_WARNING_DAYS,
    critical_days: int = STALE_CRITICAL_DAYS,
) -> List[GovernanceIssue]:
    """Active or proposed events not updated within warning_days."""
    warning_cutoff = _cutoff(warning_days)
    critical_cutoff = _cutoff(critical_days)
    issues: List[GovernanceIssue] = []

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, status, updated_at FROM memory_events"
            " WHERE status IN ('active', 'proposed') AND updated_at < ?"
            " ORDER BY id ASC",
            (warning_cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if row['updated_at'] < critical_cutoff:
            severity = 'critical'
            days_label = f'more than {critical_days} days'
        else:
            severity = 'warning'
            days_label = f'more than {warning_days} days'
        issues.append(GovernanceIssue(
            issue_type='stale_memory',
            severity=severity,
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Event [{row['id']}] '{row['title']}' has status '{row['status']}' "
                f"and has not been updated in {days_label}. "
                f"Last updated: {row['updated_at']}."
            ),
            recommended_action=(
                'Review and transition to accepted, archived, or deprecated '
                'if this event is no longer being actively refined.'
            ),
        ))
    return issues


def detect_conflicts(db_path: str) -> List[GovernanceIssue]:
    """Events connected by an active contradicts link where both sides are active or accepted."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                ml.id AS link_id,
                ml.source_id, ml.target_id,
                ml.created_by, ml.reason, ml.link_confidence,
                ml.link_metadata_json, ml.created_at AS link_created_at,
                e1.title AS src_title, e1.status AS src_status,
                e2.title AS tgt_title, e2.status AS tgt_status
            FROM memory_links ml
            JOIN memory_events e1 ON e1.id = ml.source_id
            JOIN memory_events e2 ON e2.id = ml.target_id
            WHERE ml.relationship = 'contradicts'
              AND ml.status = 'active'
              AND e1.status IN ('active', 'accepted')
              AND e2.status IN ('active', 'accepted')
            ORDER BY ml.source_id ASC, ml.target_id ASC
            """,
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        metadata: Dict = {
            'link_id': row['link_id'],
            'source_id': row['source_id'],
            'target_id': row['target_id'],
            'created_by': row['created_by'],
            'reason': row['reason'],
            'link_confidence': row['link_confidence'],
            'created_at': row['link_created_at'],
        }
        if row['link_metadata_json'] is not None:
            metadata['link_metadata_json'] = row['link_metadata_json']

        issues.append(GovernanceIssue(
            issue_type='conflicting_active',
            severity='critical',
            memory_id=row['source_id'],
            title=row['src_title'],
            rationale=(
                f"Event [{row['source_id']}] '{row['src_title']}' (status: {row['src_status']}) "
                f"contradicts event [{row['target_id']}] '{row['tgt_title']}' "
                f"(status: {row['tgt_status']}), but both are active."
            ),
            recommended_action=(
                'Resolve the contradiction: supersede one event, reject one, '
                'or update both to reflect the reconciled position.'
            ),
            metadata=metadata,
        ))
    return issues


def detect_orphans(db_path: str) -> List[GovernanceIssue]:
    """Events with no connections: absent from memory_links and not referenced by any other event."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title, status FROM memory_events
            WHERE related_ids_json = '[]'
              AND id NOT IN (SELECT source_id FROM memory_links)
              AND id NOT IN (SELECT target_id FROM memory_links)
              AND id NOT IN (
                  SELECT CAST(j.value AS INTEGER)
                  FROM memory_events me, json_each(me.related_ids_json) j
              )
            ORDER BY id ASC
            """,
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        issues.append(GovernanceIssue(
            issue_type='orphaned_event',
            severity='info',
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Event [{row['id']}] '{row['title']}' has no links to or from any other event "
                f"and does not appear in any related_ids list."
            ),
            recommended_action=(
                'Add memory_links or related_ids to connect this event to the knowledge graph, '
                'or archive it if it is intentionally standalone reference material.'
            ),
        ))
    return issues


def detect_missing_evidence(db_path: str) -> List[GovernanceIssue]:
    """validation_result events with no evidence, and accepted/active high-confidence events with no evidence."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title, event_type, status, confidence FROM memory_events
            WHERE (evidence IS NULL OR evidence = '')
              AND (
                  event_type = 'validation_result'
                  OR (status IN ('accepted', 'active') AND confidence >= 4)
              )
            ORDER BY id ASC
            """,
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if row['event_type'] == 'validation_result':
            rationale = (
                f"Event [{row['id']}] '{row['title']}' is a validation_result but has no evidence field. "
                f"Validation results require documented evidence to be auditable."
            )
        else:
            rationale = (
                f"Event [{row['id']}] '{row['title']}' has status '{row['status']}' and "
                f"confidence {row['confidence']} but no supporting evidence recorded."
            )
        issues.append(GovernanceIssue(
            issue_type='missing_evidence',
            severity='warning',
            memory_id=row['id'],
            title=row['title'],
            rationale=rationale,
            recommended_action=(
                'Add an evidence field documenting the source material, validation run, '
                'or experiment that supports this event.'
            ),
        ))
    return issues


def detect_low_confidence_active(
    db_path: str,
    threshold: int = LOW_CONFIDENCE_ACTIVE_THRESHOLD,
) -> List[GovernanceIssue]:
    """Active or accepted events with confidence at or below threshold."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title, status, confidence FROM memory_events
            WHERE status IN ('active', 'accepted') AND confidence <= ?
            ORDER BY id ASC
            """,
            (threshold,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        severity = 'critical' if row['confidence'] == 1 else 'warning'
        issues.append(GovernanceIssue(
            issue_type='low_confidence_active',
            severity=severity,
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Event [{row['id']}] '{row['title']}' is in status '{row['status']}' "
                f"with confidence {row['confidence']} (threshold: {threshold}). "
                f"Low-confidence events in active status may propagate speculative cognition."
            ),
            recommended_action=(
                'Increase confidence through validation, downgrade status to proposed, '
                'or archive if the evidence base is insufficient to support active status.'
            ),
        ))
    return issues


def detect_unresolved_aging(
    db_path: str,
    warning_days: int = UNRESOLVED_WARNING_DAYS,
    critical_days: int = UNRESOLVED_CRITICAL_DAYS,
) -> List[GovernanceIssue]:
    """Unresolved events older than warning_days."""
    warning_cutoff = _cutoff(warning_days)
    critical_cutoff = _cutoff(critical_days)
    issues: List[GovernanceIssue] = []

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title, created_at FROM memory_events
            WHERE status = 'unresolved' AND created_at < ?
            ORDER BY id ASC
            """,
            (warning_cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if row['created_at'] < critical_cutoff:
            severity = 'critical'
            days_label = f'more than {critical_days} days'
        else:
            severity = 'warning'
            days_label = f'more than {warning_days} days'
        issues.append(GovernanceIssue(
            issue_type='unresolved_aging',
            severity=severity,
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Event [{row['id']}] '{row['title']}' has been unresolved for {days_label}. "
                f"Created: {row['created_at']}."
            ),
            recommended_action=(
                'Schedule a review session to resolve, reject, or archive this open question.'
            ),
        ))
    return issues


def detect_deprecated_linked(db_path: str) -> List[GovernanceIssue]:
    """Deprecated events still targeted by active, accepted, or proposed links."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT e.id, e.title, e.status
            FROM memory_events e
            JOIN memory_links ml ON ml.target_id = e.id
            JOIN memory_events src ON src.id = ml.source_id
            WHERE e.status = 'deprecated'
              AND src.status IN ('active', 'accepted', 'proposed')
            ORDER BY e.id ASC
            """,
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        issues.append(GovernanceIssue(
            issue_type='deprecated_linked',
            severity='warning',
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Event [{row['id']}] '{row['title']}' is deprecated but is still linked "
                f"from active, accepted, or proposed events. "
                f"Active doctrine should not depend on deprecated memory."
            ),
            recommended_action=(
                'Update the linking events to reference the superseding event, '
                'or remove the link if the dependency is no longer valid.'
            ),
        ))
    return issues


def detect_duplicate_title(db_path: str) -> List[GovernanceIssue]:
    """Multiple events sharing an exact title."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title FROM memory_events
            WHERE title IN (
                SELECT title FROM memory_events
                GROUP BY title HAVING COUNT(*) > 1
            )
            ORDER BY title ASC, id ASC
            """,
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        issues.append(GovernanceIssue(
            issue_type='duplicate_title',
            severity='warning',
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Event [{row['id']}] '{row['title']}' shares an exact title with one or more "
                f"other events. Duplicate titles indicate potential ontology drift or redundant "
                f"knowledge encoding."
            ),
            recommended_action=(
                'Review duplicates: merge, supersede, or differentiate titles to reflect distinct concepts.'
            ),
        ))
    return issues


def detect_excessive_fanout(
    db_path: str,
    max_fanout: int = MAX_RELATED_FANOUT,
) -> List[GovernanceIssue]:
    """Events whose related_ids_json array exceeds max_fanout entries."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title, json_array_length(related_ids_json) AS fanout
            FROM memory_events
            WHERE json_array_length(related_ids_json) > ?
            ORDER BY id ASC
            """,
            (max_fanout,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        issues.append(GovernanceIssue(
            issue_type='excessive_fanout',
            severity='info',
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Event [{row['id']}] '{row['title']}' references {row['fanout']} related events "
                f"(limit: {max_fanout}). Excessive fanout may cause retrieval pollution and "
                f"expand context with low-relevance events."
            ),
            recommended_action=(
                'Review related_ids and prune weak references. '
                'Use typed memory_links relationships instead of bulk related_ids where possible.'
            ),
        ))
    return issues


def detect_adaptation_lineage_gap(db_path: str) -> List[GovernanceIssue]:
    """Active/accepted adaptation events with no linked validation_result event."""
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.title, e.confidence
            FROM memory_events e
            WHERE e.event_type = 'adaptation'
              AND e.status IN ('active', 'accepted')
              AND NOT EXISTS (
                  SELECT 1 FROM memory_links ml
                  JOIN memory_events e2 ON e2.id = ml.target_id
                  WHERE ml.source_id = e.id
                    AND e2.event_type = 'validation_result'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM memory_links ml
                  JOIN memory_events e2 ON e2.id = ml.source_id
                  WHERE ml.target_id = e.id
                    AND e2.event_type = 'validation_result'
              )
            ORDER BY e.id ASC
            """,
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        issues.append(GovernanceIssue(
            issue_type='adaptation_lineage_gap',
            severity='warning',
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Adaptation event [{row['id']}] '{row['title']}' is active/accepted "
                f"but has no linked validation_result event. "
                f"Adaptations should be backed by documented validation."
            ),
            recommended_action=(
                'Link this adaptation to a validation_result event that documents '
                'the evidence supporting the adaptation decision.'
            ),
        ))
    return issues


def detect_unreviewed_confidence_candidates(
    db_path: str,
    warning_days: int = 7,
    critical_days: int = 30,
) -> List[GovernanceIssue]:
    """Candidate confidence revisions in 'proposed' status older than warning_days."""
    warning_cutoff = _cutoff(warning_days)
    critical_cutoff = _cutoff(critical_days)
    issues: List[GovernanceIssue] = []

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT cr.id AS revision_id, cr.memory_event_id, cr.confidence_before,
                   cr.confidence_after, cr.revised_by, cr.reason,
                   cr.contradiction_link_ids_json, cr.created_at,
                   me.title
            FROM confidence_revisions cr
            JOIN memory_events me ON me.id = cr.memory_event_id
            WHERE cr.revision_type = 'candidate'
              AND cr.status = 'proposed'
              AND cr.created_at < ?
            ORDER BY cr.memory_event_id ASC, cr.id ASC
            """,
            (warning_cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if row['created_at'] < critical_cutoff:
            severity = 'critical'
            days_label = f'more than {critical_days} days'
        else:
            severity = 'warning'
            days_label = f'more than {warning_days} days'

        metadata: Dict = {
            'revision_id': row['revision_id'],
            'memory_event_id': row['memory_event_id'],
            'confidence_before': row['confidence_before'],
            'confidence_after': row['confidence_after'],
            'revised_by': row['revised_by'],
            'reason': row['reason'],
            'contradiction_link_ids_json': row['contradiction_link_ids_json'],
        }

        issues.append(GovernanceIssue(
            issue_type='unreviewed_confidence_candidate',
            severity=severity,
            memory_id=row['memory_event_id'],
            title=row['title'],
            rationale=(
                f"Candidate confidence revision (id={row['revision_id']}) for event "
                f"[{row['memory_event_id']}] '{row['title']}' "
                f"({row['confidence_before']} → {row['confidence_after']}) "
                f"has been unreviewed for {days_label}. "
                f"Created: {row['created_at']}."
            ),
            recommended_action=(
                'Review the candidate revision and either promote it by creating an operator '
                'revision, or reject it via reject_candidate_revision().'
            ),
            metadata=metadata,
        ))
    return issues


# ---------------------------------------------------------------------------
# Cognition session governance
# ---------------------------------------------------------------------------

SESSION_STALE_WARNING_DAYS = 30
SESSION_STALE_CRITICAL_DAYS = 90
SESSION_ABANDONED_THRESHOLD_DAYS = 7


def _cognition_session_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognition_session'"
    ).fetchone() is not None


def detect_stale_sessions(
    db_path: str,
    warning_days: int = SESSION_STALE_WARNING_DAYS,
    critical_days: int = SESSION_STALE_CRITICAL_DAYS,
) -> List[GovernanceIssue]:
    """Active cognition sessions open longer than warning_days."""
    warning_cutoff = _cutoff(warning_days)
    critical_cutoff = _cutoff(critical_days)
    issues: List[GovernanceIssue] = []

    conn = _connect(db_path)
    try:
        if not _cognition_session_table_exists(conn):
            return issues
        rows = conn.execute(
            "SELECT id, session_key, started_at, assembly_count FROM cognition_session"
            " WHERE status = 'active' AND started_at < ?"
            " ORDER BY id ASC",
            (warning_cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if row['started_at'] < critical_cutoff:
            severity = 'critical'
            days_label = f'more than {critical_days} days'
        else:
            severity = 'warning'
            days_label = f'more than {warning_days} days'
        issues.append(GovernanceIssue(
            issue_type='stale_cognition_session',
            severity=severity,
            memory_id=0,
            title=f"Stale session: {row['session_key']}",
            rationale=(
                f"Cognition session [{row['id']}] (key: {row['session_key']}) "
                f"has been active for {days_label}. "
                f"Started: {row['started_at']}. Assembly count: {row['assembly_count']}."
            ),
            recommended_action=(
                'Close the session if cognition is complete, or investigate why it remains open.'
            ),
            metadata={
                'session_id': row['id'],
                'session_key': row['session_key'],
                'started_at': row['started_at'],
                'assembly_count': row['assembly_count'],
            },
        ))
    return issues


def detect_abandoned_sessions(
    db_path: str,
    threshold_days: int = SESSION_ABANDONED_THRESHOLD_DAYS,
) -> List[GovernanceIssue]:
    """Active cognition sessions with no transition activity for threshold_days."""
    threshold_cutoff = _cutoff(threshold_days)
    issues: List[GovernanceIssue] = []

    conn = _connect(db_path)
    try:
        if not _cognition_session_table_exists(conn):
            return issues
        rows = conn.execute(
            """
            SELECT cs.id, cs.session_key, cs.started_at, cs.assembly_count,
                   COALESCE(MAX(atl.transitioned_at), cs.started_at) AS last_activity_at
            FROM cognition_session cs
            LEFT JOIN assembly_transition_log atl ON atl.cognition_session_id = cs.id
            WHERE cs.status = 'active'
            GROUP BY cs.id
            HAVING last_activity_at < ?
            ORDER BY cs.id ASC
            """,
            (threshold_cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        issues.append(GovernanceIssue(
            issue_type='abandoned_cognition_session',
            severity='warning',
            memory_id=0,
            title=f"Abandoned session: {row['session_key']}",
            rationale=(
                f"Cognition session [{row['id']}] (key: {row['session_key']}) "
                f"is active but has had no assembly transitions for more than "
                f"{threshold_days} days. "
                f"Last activity: {row['last_activity_at']}. "
                f"Assembly count: {row['assembly_count']}."
            ),
            recommended_action=(
                'Close or explicitly abandon this session if cognition is no longer in progress.'
            ),
            metadata={
                'session_id': row['id'],
                'session_key': row['session_key'],
                'started_at': row['started_at'],
                'last_activity_at': row['last_activity_at'],
                'assembly_count': row['assembly_count'],
            },
        ))
    return issues


def detect_duplicate_active_sessions(db_path: str) -> List[GovernanceIssue]:
    """Session keys with more than one active cognition_session row."""
    issues: List[GovernanceIssue] = []

    conn = _connect(db_path)
    try:
        if not _cognition_session_table_exists(conn):
            return issues

        dup_keys = conn.execute(
            "SELECT session_key FROM cognition_session"
            " WHERE status = 'active'"
            " GROUP BY session_key HAVING COUNT(*) > 1"
            " ORDER BY session_key ASC",
        ).fetchall()

        for key_row in dup_keys:
            session_key = key_row['session_key']
            id_rows = conn.execute(
                "SELECT id FROM cognition_session"
                " WHERE session_key = ? AND status = 'active'"
                " ORDER BY id ASC",
                (session_key,),
            ).fetchall()
            session_ids = [r['id'] for r in id_rows]
            issues.append(GovernanceIssue(
                issue_type='duplicate_active_cognition_session',
                severity='warning',
                memory_id=0,
                title=f"Duplicate active sessions: {session_key}",
                rationale=(
                    f"Session key '{session_key}' has {len(session_ids)} active "
                    f"cognition_session rows (ids: {session_ids}). "
                    f"Only one session should be active per key at a time."
                ),
                recommended_action=(
                    'Close all but the intended active session for this session key.'
                ),
                metadata={
                    'session_key': session_key,
                    'session_ids': session_ids,
                    'active_count': len(session_ids),
                },
            ))
    finally:
        conn.close()

    return issues


# ---------------------------------------------------------------------------
# Governance report
# ---------------------------------------------------------------------------

CANDIDATE_WARNING_DAYS = 7
CANDIDATE_CRITICAL_DAYS = 30


def build_governance_report(
    db_path: str,
    stale_warning_days: int = STALE_WARNING_DAYS,
    stale_critical_days: int = STALE_CRITICAL_DAYS,
    unresolved_warning_days: int = UNRESOLVED_WARNING_DAYS,
    unresolved_critical_days: int = UNRESOLVED_CRITICAL_DAYS,
    low_confidence_threshold: int = LOW_CONFIDENCE_ACTIVE_THRESHOLD,
    max_fanout: int = MAX_RELATED_FANOUT,
    candidate_warning_days: int = CANDIDATE_WARNING_DAYS,
    candidate_critical_days: int = CANDIDATE_CRITICAL_DAYS,
    session_stale_warning_days: int = SESSION_STALE_WARNING_DAYS,
    session_stale_critical_days: int = SESSION_STALE_CRITICAL_DAYS,
    session_abandoned_threshold_days: int = SESSION_ABANDONED_THRESHOLD_DAYS,
) -> GovernanceReport:
    """Run all governance checks and return a deterministically sorted report.

    Sort order: (severity_rank, issue_type, memory_id) — critical before warning before info,
    then alphabetically by issue_type, then by memory_id ascending.
    """
    conn = _connect(db_path)
    try:
        total = conn.execute('SELECT COUNT(*) FROM memory_events').fetchone()[0]
    finally:
        conn.close()

    issues: List[GovernanceIssue] = []
    issues.extend(detect_stale_memory(db_path, stale_warning_days, stale_critical_days))
    issues.extend(detect_conflicts(db_path))
    issues.extend(detect_orphans(db_path))
    issues.extend(detect_missing_evidence(db_path))
    issues.extend(detect_low_confidence_active(db_path, low_confidence_threshold))
    issues.extend(detect_unresolved_aging(db_path, unresolved_warning_days, unresolved_critical_days))
    issues.extend(detect_deprecated_linked(db_path))
    issues.extend(detect_duplicate_title(db_path))
    issues.extend(detect_excessive_fanout(db_path, max_fanout))
    issues.extend(detect_adaptation_lineage_gap(db_path))
    issues.extend(detect_unreviewed_confidence_candidates(db_path, candidate_warning_days, candidate_critical_days))
    issues.extend(detect_stale_sessions(db_path, session_stale_warning_days, session_stale_critical_days))
    issues.extend(detect_abandoned_sessions(db_path, session_abandoned_threshold_days))
    issues.extend(detect_duplicate_active_sessions(db_path))

    issues.sort(key=lambda i: (_SEVERITY_ORDER[i.severity], i.issue_type, i.memory_id))

    return GovernanceReport(
        generated_at=_now_utc(),
        total_events=total,
        issues=issues,
    )
