"""
Phase 8A: ontology and governed vocabulary substrate.

Governed vocabulary registry for four semantic vocabularies:
  event_type         — memory event classifications
  relationship       — memory link types
  trigger_class      — activation policy trigger classes
  compression_method — compression artifact producer methods

Design invariants:
- Terms are append-only governed artifacts; status changes, never deletes.
- Python validation tuples (VALID_EVENT_TYPES, etc.) remain the runtime write gate.
  This registry supplements them with lineage and deprecation tracking.
- Replay paths MUST NOT query ontology tables. This module is observational governance
  infrastructure, not a runtime dependency for historical replay.
- Alias chains are explicitly forbidden. resolve_alias() performs exactly one step.
- Aliases may not target another alias, may not self-alias, and may not target
  forbidden terms.
- Supersession replacement must be active, same-vocabulary, non-self.
- All list/query functions return deterministically ordered results.
- Governance detectors degrade gracefully when ontology tables are absent (pre-v16 DBs).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .governance import GovernanceIssue


VALID_TERM_STATUSES = ('active', 'deprecated', 'superseded', 'forbidden')
VALID_VOCABULARY_NAMES = ('event_type', 'relationship', 'trigger_class', 'compression_method')


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _ontology_tables_exist(conn: sqlite3.Connection) -> bool:
    """Return True if the ontology_terms table exists (schema v16+)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ontology_terms'"
    ).fetchone()
    return row is not None


def _activation_policies_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activation_policies'"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class OntologyTerm:
    id: int
    vocabulary_name: str
    term: str
    label: str
    description: Optional[str]
    status: str
    superseded_by: Optional[str]
    introduced_at: str
    introduced_by: str
    deprecated_at: Optional[str]
    deprecated_by: Optional[str]
    deprecation_reason: Optional[str]
    provenance: Optional[dict]

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'vocabulary_name': self.vocabulary_name,
            'term': self.term,
            'label': self.label,
            'description': self.description,
            'status': self.status,
            'superseded_by': self.superseded_by,
            'introduced_at': self.introduced_at,
            'introduced_by': self.introduced_by,
            'deprecated_at': self.deprecated_at,
            'deprecated_by': self.deprecated_by,
            'deprecation_reason': self.deprecation_reason,
            'provenance': self.provenance,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'OntologyTerm':
        provenance = None
        raw = row['provenance_json']
        if raw:
            try:
                provenance = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                provenance = None
        return cls(
            id=row['id'],
            vocabulary_name=row['vocabulary_name'],
            term=row['term'],
            label=row['label'],
            description=row['description'],
            status=row['status'],
            superseded_by=row['superseded_by'],
            introduced_at=row['introduced_at'],
            introduced_by=row['introduced_by'],
            deprecated_at=row['deprecated_at'],
            deprecated_by=row['deprecated_by'],
            deprecation_reason=row['deprecation_reason'],
            provenance=provenance,
        )


@dataclass
class OntologyAlias:
    id: int
    vocabulary_name: str
    term: str
    alias: str
    created_at: str
    created_by: str
    reason: str

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'vocabulary_name': self.vocabulary_name,
            'term': self.term,
            'alias': self.alias,
            'created_at': self.created_at,
            'created_by': self.created_by,
            'reason': self.reason,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'OntologyAlias':
        return cls(
            id=row['id'],
            vocabulary_name=row['vocabulary_name'],
            term=row['term'],
            alias=row['alias'],
            created_at=row['created_at'],
            created_by=row['created_by'],
            reason=row['reason'],
        )


# ---------------------------------------------------------------------------
# Core CRUD functions
# ---------------------------------------------------------------------------

def register_term(
    db_path: str,
    vocabulary_name: str,
    term: str,
    label: str,
    *,
    introduced_by: str,
    description: Optional[str] = None,
    provenance: Optional[dict] = None,
) -> OntologyTerm:
    """Register a new ontology term with status='active'.

    Raises ValueError if:
    - vocabulary_name, term, label, or introduced_by are empty
    - vocabulary_name is not a recognized vocabulary
    - term already exists in this vocabulary (idempotency guard)
    - term string is already an alias in this vocabulary (integrity guard)
    """
    if not vocabulary_name or not vocabulary_name.strip():
        raise ValueError("'vocabulary_name' must not be empty")
    if vocabulary_name not in VALID_VOCABULARY_NAMES:
        raise ValueError(
            f"Unknown vocabulary_name {vocabulary_name!r}. "
            f"Valid: {sorted(VALID_VOCABULARY_NAMES)}"
        )
    if not term or not term.strip():
        raise ValueError("'term' must not be empty")
    if not label or not label.strip():
        raise ValueError("'label' must not be empty")
    if not introduced_by or not introduced_by.strip():
        raise ValueError("'introduced_by' must not be empty")

    provenance_json = json.dumps(provenance, sort_keys=True) if provenance is not None else None
    now = _now()

    conn = _connect(db_path)
    try:
        # Integrity guard: term must not already be an alias in this vocabulary
        existing_alias = conn.execute(
            "SELECT alias FROM ontology_aliases WHERE vocabulary_name = ? AND alias = ?",
            (vocabulary_name, term),
        ).fetchone()
        if existing_alias is not None:
            raise ValueError(
                f"Cannot register {term!r} as a canonical term in {vocabulary_name!r}: "
                f"it already exists as an alias in this vocabulary."
            )

        try:
            cur = conn.execute(
                """INSERT INTO ontology_terms
                   (vocabulary_name, term, label, description, status,
                    introduced_at, introduced_by, provenance_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (vocabulary_name, term, label, description, 'active',
                 now, introduced_by, provenance_json),
            )
        except sqlite3.IntegrityError:
            raise ValueError(
                f"Term {term!r} already exists in vocabulary {vocabulary_name!r}. "
                "Use deprecate_term() or supersede_term() to change its status."
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return OntologyTerm.from_row(row)
    finally:
        conn.close()


def deprecate_term(
    db_path: str,
    vocabulary_name: str,
    term: str,
    *,
    deprecated_by: str,
    deprecation_reason: str,
) -> OntologyTerm:
    """Transition a term from status='active' to status='deprecated'.

    The term remains accepted in historical records. New usage emits a warning.
    Raises ValueError if term is not found or not currently active.
    """
    if not deprecated_by or not deprecated_by.strip():
        raise ValueError("'deprecated_by' must not be empty")
    if not deprecation_reason or not deprecation_reason.strip():
        raise ValueError("'deprecation_reason' must not be empty")

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Term {term!r} not found in vocabulary {vocabulary_name!r}."
            )
        if row['status'] != 'active':
            raise ValueError(
                f"Cannot deprecate term {term!r} in {vocabulary_name!r}: "
                f"current status is {row['status']!r}. Only 'active' terms may be deprecated."
            )
        now = _now()
        conn.execute(
            """UPDATE ontology_terms
               SET status = 'deprecated', deprecated_at = ?, deprecated_by = ?,
                   deprecation_reason = ?
               WHERE vocabulary_name = ? AND term = ?""",
            (now, deprecated_by, deprecation_reason, vocabulary_name, term),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        return OntologyTerm.from_row(row)
    finally:
        conn.close()


def supersede_term(
    db_path: str,
    vocabulary_name: str,
    term: str,
    *,
    superseded_by: str,
    deprecated_by: str,
    deprecation_reason: str,
) -> OntologyTerm:
    """Transition a term from status='active' to status='superseded'.

    superseded_by must:
    - exist in the same vocabulary_name
    - have status='active'
    - differ from term (no self-supersession)

    Raises ValueError on any violation.
    """
    if not deprecated_by or not deprecated_by.strip():
        raise ValueError("'deprecated_by' must not be empty")
    if not deprecation_reason or not deprecation_reason.strip():
        raise ValueError("'deprecation_reason' must not be empty")
    if not superseded_by or not superseded_by.strip():
        raise ValueError("'superseded_by' must not be empty")
    if superseded_by == term:
        raise ValueError(
            f"Self-supersession is forbidden: term and superseded_by are both {term!r}."
        )

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Term {term!r} not found in vocabulary {vocabulary_name!r}."
            )
        if row['status'] != 'active':
            raise ValueError(
                f"Cannot supersede term {term!r} in {vocabulary_name!r}: "
                f"current status is {row['status']!r}. Only 'active' terms may be superseded."
            )

        # Replacement must exist in the same vocabulary and be active
        replacement = conn.execute(
            "SELECT status FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, superseded_by),
        ).fetchone()
        if replacement is None:
            raise ValueError(
                f"Replacement term {superseded_by!r} does not exist in vocabulary "
                f"{vocabulary_name!r}. Both terms must be in the same vocabulary."
            )
        if replacement['status'] != 'active':
            raise ValueError(
                f"Replacement term {superseded_by!r} has status={replacement['status']!r}. "
                f"superseded_by must be an active term in the same vocabulary."
            )

        now = _now()
        conn.execute(
            """UPDATE ontology_terms
               SET status = 'superseded', superseded_by = ?,
                   deprecated_at = ?, deprecated_by = ?, deprecation_reason = ?
               WHERE vocabulary_name = ? AND term = ?""",
            (superseded_by, now, deprecated_by, deprecation_reason, vocabulary_name, term),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        return OntologyTerm.from_row(row)
    finally:
        conn.close()


def forbid_term(
    db_path: str,
    vocabulary_name: str,
    term: str,
    *,
    forbidden_by: str,
    reason: str,
) -> OntologyTerm:
    """Transition a term to status='forbidden'. Hard-rejected for new usage.

    Can be applied from any status. Forbidden terms also invalidate aliases
    pointing to them — add_alias() will reject targets with status='forbidden'.
    """
    if not forbidden_by or not forbidden_by.strip():
        raise ValueError("'forbidden_by' must not be empty")
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Term {term!r} not found in vocabulary {vocabulary_name!r}."
            )
        now = _now()
        conn.execute(
            """UPDATE ontology_terms
               SET status = 'forbidden', deprecated_at = ?, deprecated_by = ?,
                   deprecation_reason = ?
               WHERE vocabulary_name = ? AND term = ?""",
            (now, forbidden_by, reason, vocabulary_name, term),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        return OntologyTerm.from_row(row)
    finally:
        conn.close()


def add_alias(
    db_path: str,
    vocabulary_name: str,
    term: str,
    alias: str,
    *,
    created_by: str,
    reason: str,
) -> OntologyAlias:
    """Add an alias for a canonical ontology term.

    Alias chain invariants (all hard-enforced):
    - alias != term (no self-alias)
    - term must be a canonical entry in ontology_terms for this vocabulary
    - alias must not itself be a canonical term in ontology_terms (prevents shadowing)
    - alias must not already exist in ontology_aliases for this vocabulary
    - target term must not be status='forbidden'

    resolve_alias() performs exactly one lookup step. Alias chains are structurally
    impossible because the alias field in ontology_aliases can only point to a
    canonical term (validated here), never to another alias entry.
    """
    if not created_by or not created_by.strip():
        raise ValueError("'created_by' must not be empty")
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")
    if not alias or not alias.strip():
        raise ValueError("'alias' must not be empty")
    if alias == term:
        raise ValueError(
            f"Self-alias is forbidden: alias and term are both {term!r}."
        )

    conn = _connect(db_path)
    try:
        # target term must be a canonical term in ontology_terms
        target = conn.execute(
            "SELECT status FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        if target is None:
            raise ValueError(
                f"Canonical term {term!r} not found in vocabulary {vocabulary_name!r}. "
                "An alias must target an existing canonical term, not another alias."
            )
        if target['status'] == 'forbidden':
            raise ValueError(
                f"Cannot alias {term!r} in {vocabulary_name!r}: the target term is forbidden."
            )

        # alias must not shadow an existing canonical term
        shadows = conn.execute(
            "SELECT 1 FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, alias),
        ).fetchone()
        if shadows is not None:
            raise ValueError(
                f"Alias {alias!r} conflicts with an existing canonical term in "
                f"vocabulary {vocabulary_name!r}. Aliases may not shadow canonical terms."
            )

        now = _now()
        try:
            cur = conn.execute(
                """INSERT INTO ontology_aliases
                   (vocabulary_name, term, alias, created_at, created_by, reason)
                   VALUES (?,?,?,?,?,?)""",
                (vocabulary_name, term, alias, now, created_by, reason),
            )
        except sqlite3.IntegrityError:
            raise ValueError(
                f"Alias {alias!r} already exists in vocabulary {vocabulary_name!r}."
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM ontology_aliases WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return OntologyAlias.from_row(row)
    finally:
        conn.close()


def get_term(
    db_path: str,
    vocabulary_name: str,
    term: str,
) -> Optional[OntologyTerm]:
    """Return an OntologyTerm by vocabulary and term, or None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM ontology_terms WHERE vocabulary_name = ? AND term = ?",
            (vocabulary_name, term),
        ).fetchone()
        return OntologyTerm.from_row(row) if row else None
    finally:
        conn.close()


def list_terms(
    db_path: str,
    *,
    vocabulary_name: Optional[str] = None,
    status: Optional[str] = None,
) -> List[OntologyTerm]:
    """List ontology terms, deterministically ordered by vocabulary_name ASC, term ASC."""
    query = "SELECT * FROM ontology_terms"
    params: list = []
    clauses: list = []
    if vocabulary_name is not None:
        clauses.append("vocabulary_name = ?")
        params.append(vocabulary_name)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY vocabulary_name ASC, term ASC"

    conn = _connect(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
        return [OntologyTerm.from_row(r) for r in rows]
    finally:
        conn.close()


def resolve_alias(
    db_path: str,
    vocabulary_name: str,
    alias: str,
) -> Optional[str]:
    """Resolve an alias to its canonical term string.

    Performs exactly one lookup step. Returns None if the alias is not found.
    Alias chains are structurally impossible — the alias column in ontology_aliases
    always points to a canonical term, never to another alias entry.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT term FROM ontology_aliases WHERE vocabulary_name = ? AND alias = ?",
            (vocabulary_name, alias),
        ).fetchone()
        return row['term'] if row else None
    finally:
        conn.close()


def list_aliases(
    db_path: str,
    *,
    vocabulary_name: Optional[str] = None,
) -> List[OntologyAlias]:
    """List aliases, deterministically ordered by vocabulary_name ASC, alias ASC."""
    query = "SELECT * FROM ontology_aliases"
    params: list = []
    if vocabulary_name is not None:
        query += " WHERE vocabulary_name = ?"
        params.append(vocabulary_name)
    query += " ORDER BY vocabulary_name ASC, alias ASC"

    conn = _connect(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
        return [OntologyAlias.from_row(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Governance detectors
# All detectors degrade gracefully when ontology tables are absent (pre-v16 DBs).
# All detectors are read-only and observational.
# ---------------------------------------------------------------------------

def detect_unregistered_compression_methods(
    db_path: str,
) -> List[GovernanceIssue]:
    """Flag compression artifacts whose compression_method is not in the ontology registry.

    Returns empty list if ontology tables are absent (pre-v16 DB) or if
    compression_artifacts table is absent.
    """
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        if not _ontology_tables_exist(conn):
            return issues

        # Check that compression_artifacts table exists
        has_ca = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='compression_artifacts'"
        ).fetchone()
        if has_ca is None:
            return issues

        rows = conn.execute(
            "SELECT DISTINCT compression_method FROM compression_artifacts ORDER BY compression_method ASC"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        method = row['compression_method']
        registered = get_term(db_path, 'compression_method', method)
        if registered is None or registered.status != 'active':
            issues.append(GovernanceIssue(
                issue_type='unregistered_compression_method',
                severity='warning',
                memory_id=0,
                title=f"Unregistered compression method: {method!r}",
                rationale=(
                    f"compression_artifacts rows use method={method!r} which has no "
                    "active entry in the ontology registry vocabulary 'compression_method'. "
                    "Unregistered methods cannot be governed, deprecated, or routed."
                ),
                recommended_action=(
                    f"Register the method via: ontology-register --vocabulary compression_method "
                    f"--term {method!r} --label '...' --introduced-by <operator>"
                ),
                metadata={'compression_method': method},
            ))

    return issues


def detect_deprecated_event_type_usage(
    db_path: str,
) -> List[GovernanceIssue]:
    """Flag memory_events using deprecated/superseded event_type values, written after deprecation.

    Pre-deprecation records are never flagged. Returns empty list if ontology tables absent.
    """
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        if not _ontology_tables_exist(conn):
            return issues

        rows = conn.execute(
            """
            SELECT me.id, me.title, me.event_type, me.created_at,
                   ot.status AS term_status, ot.deprecated_at, ot.superseded_by
            FROM memory_events me
            JOIN ontology_terms ot
              ON ot.vocabulary_name = 'event_type' AND ot.term = me.event_type
            WHERE ot.status IN ('deprecated', 'superseded')
              AND ot.deprecated_at IS NOT NULL
              AND me.created_at > ot.deprecated_at
            ORDER BY me.id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        replacement = f" Consider using {row['superseded_by']!r} instead." if row['superseded_by'] else ""
        issues.append(GovernanceIssue(
            issue_type='deprecated_event_type_usage',
            severity='warning',
            memory_id=row['id'],
            title=row['title'],
            rationale=(
                f"Memory event [{row['id']}] '{row['title']}' uses event_type={row['event_type']!r} "
                f"which was {row['term_status']} on {row['deprecated_at']}. "
                f"This event was created after the deprecation date."
                f"{replacement}"
            ),
            recommended_action=(
                "Review and update the event_type if a canonical replacement exists, "
                "or register a supersession in the ontology registry."
            ),
            metadata={
                'memory_event_id': row['id'],
                'event_type': row['event_type'],
                'term_status': row['term_status'],
                'deprecated_at': row['deprecated_at'],
                'superseded_by': row['superseded_by'],
            },
        ))

    return issues


def detect_deprecated_relationship_usage(
    db_path: str,
) -> List[GovernanceIssue]:
    """Flag memory_links using deprecated/superseded relationship values, written after deprecation.

    Pre-deprecation records are never flagged. Returns empty list if ontology tables absent.
    """
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        if not _ontology_tables_exist(conn):
            return issues

        rows = conn.execute(
            """
            SELECT ml.id, ml.source_id, ml.target_id, ml.relationship, ml.created_at,
                   ot.status AS term_status, ot.deprecated_at, ot.superseded_by
            FROM memory_links ml
            JOIN ontology_terms ot
              ON ot.vocabulary_name = 'relationship' AND ot.term = ml.relationship
            WHERE ot.status IN ('deprecated', 'superseded')
              AND ot.deprecated_at IS NOT NULL
              AND ml.created_at > ot.deprecated_at
            ORDER BY ml.id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        replacement = f" Consider using {row['superseded_by']!r} instead." if row['superseded_by'] else ""
        issues.append(GovernanceIssue(
            issue_type='deprecated_relationship_usage',
            severity='warning',
            memory_id=row['source_id'],
            title=f"Link {row['source_id']} → {row['target_id']}",
            rationale=(
                f"Memory link [{row['id']}] ({row['source_id']} → {row['target_id']}) uses "
                f"relationship={row['relationship']!r} which was {row['term_status']} "
                f"on {row['deprecated_at']}. This link was created after the deprecation date."
                f"{replacement}"
            ),
            recommended_action=(
                "Review the link relationship. If a replacement exists, "
                "create a new link with the canonical relationship and retract this one."
            ),
            metadata={
                'link_id': row['id'],
                'source_id': row['source_id'],
                'target_id': row['target_id'],
                'relationship': row['relationship'],
                'term_status': row['term_status'],
                'deprecated_at': row['deprecated_at'],
                'superseded_by': row['superseded_by'],
            },
        ))

    return issues


def detect_deprecated_trigger_class_usage(
    db_path: str,
) -> List[GovernanceIssue]:
    """Flag activation_policies using deprecated/superseded trigger_class values.

    Only flags policies created after the deprecation date (replay-safe).
    Returns empty list if ontology tables or activation_policies table is absent.
    """
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        if not _ontology_tables_exist(conn):
            return issues
        if not _activation_policies_table_exists(conn):
            return issues

        rows = conn.execute(
            """
            SELECT ap.id, ap.name, ap.trigger_class, ap.created_at,
                   ot.status AS term_status, ot.deprecated_at, ot.superseded_by
            FROM activation_policies ap
            JOIN ontology_terms ot
              ON ot.vocabulary_name = 'trigger_class' AND ot.term = ap.trigger_class
            WHERE ot.status IN ('deprecated', 'superseded')
              AND ot.deprecated_at IS NOT NULL
              AND ap.created_at > ot.deprecated_at
            ORDER BY ap.id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        replacement = f" Consider using {row['superseded_by']!r} instead." if row['superseded_by'] else ""
        issues.append(GovernanceIssue(
            issue_type='deprecated_trigger_class_usage',
            severity='warning',
            memory_id=0,
            title=f"Policy {row['id']}: {row['name']}",
            rationale=(
                f"Activation policy [{row['id']}] '{row['name']}' uses "
                f"trigger_class={row['trigger_class']!r} which was {row['term_status']} "
                f"on {row['deprecated_at']}. This policy was created after the deprecation date."
                f"{replacement}"
            ),
            recommended_action=(
                "Review the activation policy trigger class. Create a new policy with the "
                "canonical trigger class and supersede this one."
            ),
            metadata={
                'policy_id': row['id'],
                'policy_name': row['name'],
                'trigger_class': row['trigger_class'],
                'term_status': row['term_status'],
                'deprecated_at': row['deprecated_at'],
                'superseded_by': row['superseded_by'],
            },
        ))

    return issues


def detect_alias_conflicts(
    db_path: str,
) -> List[GovernanceIssue]:
    """Flag aliases that shadow canonical terms in the same vocabulary.

    An alias conflict occurs when an alias string matches an existing canonical
    term string in the same vocabulary_name. This creates ambiguous resolution:
    resolve_alias() returns the aliased target, but get_term() returns the
    canonical entry — two different objects for the same lookup string.

    add_alias() prevents this at write time. This detector is a defensive audit
    for any integrity violations that bypassed the write guard.

    Returns empty list if ontology tables are absent.
    """
    issues: List[GovernanceIssue] = []
    conn = _connect(db_path)
    try:
        if not _ontology_tables_exist(conn):
            return issues

        rows = conn.execute(
            """
            SELECT oa.id, oa.vocabulary_name, oa.alias, oa.term AS alias_target,
                   ot.term AS shadowed_term, ot.status AS shadowed_status
            FROM ontology_aliases oa
            JOIN ontology_terms ot
              ON oa.vocabulary_name = ot.vocabulary_name AND oa.alias = ot.term
            ORDER BY oa.vocabulary_name ASC, oa.alias ASC
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        issues.append(GovernanceIssue(
            issue_type='alias_shadows_canonical_term',
            severity='critical',
            memory_id=0,
            title=f"Alias conflict in {row['vocabulary_name']!r}: {row['alias']!r}",
            rationale=(
                f"Alias {row['alias']!r} in vocabulary {row['vocabulary_name']!r} points to "
                f"{row['alias_target']!r} but also shadows canonical term {row['shadowed_term']!r} "
                f"(status={row['shadowed_status']!r}). resolve_alias() and get_term() return "
                "different objects for this lookup string."
            ),
            recommended_action=(
                "Remove the alias or rename the conflicting canonical term to eliminate the ambiguity."
            ),
            metadata={
                'alias_id': row['id'],
                'vocabulary_name': row['vocabulary_name'],
                'alias': row['alias'],
                'alias_target': row['alias_target'],
                'shadowed_canonical_term': row['shadowed_term'],
            },
        ))

    return issues
