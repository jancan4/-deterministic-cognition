"""
Governed activation policy substrate (Phase 7A-core).

ActivationPolicy defines the conditions under which cognition state should be
refreshed. ActivationDecision records whether a policy fired for a given trigger
event, preserving full provenance regardless of outcome.

Core invariants
---------------
- evaluate_trigger() is a pure function: no DB writes, no DB reads, deterministic.
  The caller pre-fetches relevant artifact IDs and passes them in trigger_event.
- log_activation_decision() writes exactly one activation_decision_log row per
  explicit call. Decision logging is caller-controlled; it is never automatic.
- replay_activation_decision() restores a historical decision using
  policy_snapshot_json only. It does not re-read or re-evaluate the current
  activation_policies row.
- candidate, superseded, and invalidated policies return fired=False from
  evaluate_trigger(). Only active policies may fire.
- No function in this module writes to memory_events, context_assembly_log,
  retrieval_log, confidence_revisions, or any canonical memory table.

Trigger class implementation status
-------------------------------------
Phase 7A-core implements four trigger classes:
  operator_request     — always fires on explicit operator request (active policy)
  governance_escalation — fires when governance issue severity meets threshold
  contradiction_change  — fires when new contradicts links meet count threshold
  confidence_revision   — fires when new confidence revisions meet count threshold

Six trigger classes are reserved for future phases. evaluate_trigger() returns
fired=False with detection_reason='trigger_class_reserved' for reserved classes.

Policy lifecycle
-----------------
  candidate → active        (activate_activation_policy — requires operator + reason)
  active    → superseded    (supersede_activation_policy — requires operator + reason)
  active    → invalidated   (Phase 7A-beta; not implemented in core)

Supersession columns and invalidation columns are mutually exclusive per row.
Status is authoritative. Timestamp columns are informational lineage metadata only.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional

from .models import VALID_TRIGGER_CLASSES

# ---------------------------------------------------------------------------
# Severity ordering for governance_escalation trigger evaluation
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: Dict[str, int] = {'critical': 0, 'warning': 1, 'info': 2}

# ---------------------------------------------------------------------------
# Per-trigger-class allowlist of valid condition keys
# ---------------------------------------------------------------------------

VALID_CONDITION_KEYS: Dict[str, FrozenSet[str]] = {
    # Implemented in Phase 7A-core
    'operator_request':           frozenset(),
    'governance_escalation':      frozenset({'min_severity', 'detector_names', 'require_all'}),
    'contradiction_change':       frozenset({'min_new_links'}),
    'confidence_revision':        frozenset({'min_new_revisions', 'require_revision_type'}),
    # Reserved — condition keys defined for forward compatibility; evaluation not yet implemented
    'retrieval_refresh':          frozenset({'min_new_retrievals', 'min_hours_since_assembly'}),
    'continuity_refresh':         frozenset({'artifact_statuses'}),
    'workflow_checkpoint':        frozenset({'workflow_ids', 'stage_indices'}),
    'stale_session_recovery':     frozenset({'min_days_stale'}),
    'embedding_invalidation':     frozenset({'min_invalidated_count'}),
    'semantic_candidate_arrival': frozenset({'min_candidate_count', 'candidate_statuses'}),
}

# Trigger classes with full evaluation logic in Phase 7A-core
_IMPLEMENTED_TRIGGER_CLASSES: FrozenSet[str] = frozenset({
    'operator_request',
    'governance_escalation',
    'contradiction_change',
    'confidence_revision',
})

# Reserved trigger classes — evaluate_trigger() returns fired=False
_RESERVED_TRIGGER_CLASSES: FrozenSet[str] = VALID_TRIGGER_CLASSES - _IMPLEMENTED_TRIGGER_CLASSES

# Valid lifecycle statuses for activation_policies
VALID_POLICY_STATUSES: FrozenSet[str] = frozenset({
    'candidate', 'active', 'superseded', 'invalidated',
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ActivationPolicyValidationError(ValueError):
    """Raised when an ActivationPolicy fails structural validation."""


class ActivationPolicyLifecycleError(ValueError):
    """Raised when an activation policy lifecycle transition is invalid."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ActivationPolicy:
    """
    A named, versioned, operator-governed definition of when cognition state
    should be refreshed.

    Starts in status='candidate'. Must be activated via
    activate_activation_policy() before evaluate_trigger() will return fired=True.

    trigger_conditions_json is validated against VALID_CONDITION_KEYS[trigger_class]
    at construction time. Unknown condition keys raise ActivationPolicyValidationError.
    """
    id: Optional[int]
    name: str
    trigger_class: str
    trigger_conditions_json: str
    status: str
    priority: int
    policy_version: str
    created_by: str
    reason: str
    created_at: str
    activated_at: Optional[str]
    activated_by: Optional[str]
    superseded_at: Optional[str]
    superseded_by_policy_id: Optional[int]
    superseded_by_operator: Optional[str]
    superseded_reason: Optional[str]
    invalidated_at: Optional[str]
    invalidated_reason: Optional[str]
    provenance_json: str

    def __post_init__(self) -> None:
        _validate_activation_policy(self)

    @property
    def conditions(self) -> dict:
        return json.loads(self.trigger_conditions_json)

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'name': self.name,
            'trigger_class': self.trigger_class,
            'trigger_conditions_json': self.trigger_conditions_json,
            'status': self.status,
            'priority': self.priority,
            'policy_version': self.policy_version,
            'created_by': self.created_by,
            'reason': self.reason,
            'created_at': self.created_at,
            'activated_at': self.activated_at,
            'activated_by': self.activated_by,
            'superseded_at': self.superseded_at,
            'superseded_by_policy_id': self.superseded_by_policy_id,
            'superseded_by_operator': self.superseded_by_operator,
            'superseded_reason': self.superseded_reason,
            'invalidated_at': self.invalidated_at,
            'invalidated_reason': self.invalidated_reason,
            'provenance_json': self.provenance_json,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ActivationPolicy':
        return cls(
            id=d.get('id'),
            name=d['name'],
            trigger_class=d['trigger_class'],
            trigger_conditions_json=d.get('trigger_conditions_json', '{}'),
            status=d.get('status', 'candidate'),
            priority=d.get('priority', 100),
            policy_version=d.get('policy_version', '1.0.0'),
            created_by=d['created_by'],
            reason=d.get('reason', ''),
            created_at=d.get('created_at', ''),
            activated_at=d.get('activated_at'),
            activated_by=d.get('activated_by'),
            superseded_at=d.get('superseded_at'),
            superseded_by_policy_id=d.get('superseded_by_policy_id'),
            superseded_by_operator=d.get('superseded_by_operator'),
            superseded_reason=d.get('superseded_reason'),
            invalidated_at=d.get('invalidated_at'),
            invalidated_reason=d.get('invalidated_reason'),
            provenance_json=d.get('provenance_json', '{}'),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'ActivationPolicy':
        def _g(k: str, default=None):
            try:
                return row[k]
            except (IndexError, KeyError):
                return default
        return cls(
            id=row['id'],
            name=row['name'],
            trigger_class=row['trigger_class'],
            trigger_conditions_json=row['trigger_conditions_json'],
            status=row['status'],
            priority=row['priority'],
            policy_version=row['policy_version'],
            created_by=row['created_by'],
            reason=row['reason'],
            created_at=row['created_at'],
            activated_at=_g('activated_at'),
            activated_by=_g('activated_by'),
            superseded_at=_g('superseded_at'),
            superseded_by_policy_id=_g('superseded_by_policy_id'),
            superseded_by_operator=_g('superseded_by_operator'),
            superseded_reason=_g('superseded_reason'),
            invalidated_at=_g('invalidated_at'),
            invalidated_reason=_g('invalidated_reason'),
            provenance_json=row['provenance_json'],
        )


@dataclass
class ActivationTriggerResult:
    """
    The result of one evaluate_trigger() call.

    fired=True means the policy conditions were satisfied and a cognition refresh
    is warranted. fired=False means conditions were not met, the policy is not
    active, or the trigger class is reserved.

    triggering_artifact_ids contains the sorted IDs of artifacts that contributed
    to the firing decision (e.g. contradiction link IDs, confidence revision IDs).
    It is empty when fired=False or when no artifact IDs are applicable.
    """
    trigger_class: str
    fired: bool
    detection_reason: str
    triggering_artifact_ids: List[int] = field(default_factory=list)
    triggering_workflow_execution_id: Optional[str] = None
    triggering_session_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            'trigger_class': self.trigger_class,
            'fired': self.fired,
            'detection_reason': self.detection_reason,
            'triggering_artifact_ids': list(self.triggering_artifact_ids),
            'triggering_workflow_execution_id': self.triggering_workflow_execution_id,
            'triggering_session_id': self.triggering_session_id,
        }


@dataclass
class ReplayedActivationDecision:
    """
    A historical activation decision restored from activation_decision_log.

    policy_snapshot is reconstructed from policy_snapshot_json captured at
    decision time. It does NOT reflect the current state of the
    activation_policies table. replayed=True always.

    This is a read-only audit artifact. It does not trigger retrieval, assembly,
    or any other cognition action.
    """
    decision_id: int
    policy_snapshot: ActivationPolicy
    trigger_class: str
    trigger_event: dict
    fired: bool
    detection_reason: str
    triggering_artifact_ids: List[int]
    triggering_workflow_execution_id: Optional[str]
    triggering_session_id: Optional[int]
    resulting_retrieval_id: Optional[int]
    resulting_assembly_id: Optional[int]
    resulting_transition_id: Optional[int]
    detected_at: str
    replayed: bool = True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _mem_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _validate_activation_policy(policy: 'ActivationPolicy') -> None:
    if policy.trigger_class not in VALID_TRIGGER_CLASSES:
        raise ActivationPolicyValidationError(
            f"Unknown trigger_class {policy.trigger_class!r}. "
            f"Valid: {sorted(VALID_TRIGGER_CLASSES)}"
        )
    try:
        conditions = json.loads(policy.trigger_conditions_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ActivationPolicyValidationError(
            f"trigger_conditions_json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(conditions, dict):
        raise ActivationPolicyValidationError(
            "trigger_conditions_json must be a JSON object"
        )
    allowed = VALID_CONDITION_KEYS[policy.trigger_class]
    unknown = set(conditions.keys()) - allowed
    if unknown:
        raise ActivationPolicyValidationError(
            f"Unknown condition keys for trigger_class {policy.trigger_class!r}: "
            f"{sorted(unknown)}. Valid keys: {sorted(allowed)}"
        )


# ---------------------------------------------------------------------------
# Pure trigger evaluation — no DB writes, no DB reads
# ---------------------------------------------------------------------------

def evaluate_trigger(
    policy: ActivationPolicy,
    trigger_event: dict,
) -> ActivationTriggerResult:
    """
    Pure trigger evaluation. No DB writes. No DB reads. Deterministic.

    The caller is responsible for pre-fetching artifact IDs from relevant
    tables and providing them in trigger_event. This function performs no
    I/O — it evaluates policy conditions against the provided event context.

    Returns ActivationTriggerResult with fired=True only when:
    - policy.status == 'active'
    - trigger_class is fully implemented (not reserved)
    - trigger_event satisfies policy conditions

    Non-active policies (candidate, superseded, invalidated) return fired=False
    with detection_reason='policy_disabled: status=<status>'.

    Reserved trigger classes return fired=False with
    detection_reason='trigger_class_reserved: not implemented in Phase 7A-core'.
    """
    tc = policy.trigger_class

    # Non-active policies never fire
    if policy.status != 'active':
        return ActivationTriggerResult(
            trigger_class=tc,
            fired=False,
            detection_reason=f'policy_disabled: status={policy.status!r}',
        )

    # Reserved trigger classes — not yet implemented
    if tc in _RESERVED_TRIGGER_CLASSES:
        return ActivationTriggerResult(
            trigger_class=tc,
            fired=False,
            detection_reason='trigger_class_reserved: not implemented in Phase 7A-core',
        )

    conditions = json.loads(policy.trigger_conditions_json)

    if tc == 'operator_request':
        return _eval_operator_request(conditions, trigger_event)
    if tc == 'governance_escalation':
        return _eval_governance_escalation(conditions, trigger_event)
    if tc == 'contradiction_change':
        return _eval_contradiction_change(conditions, trigger_event)
    if tc == 'confidence_revision':
        return _eval_confidence_revision(conditions, trigger_event)

    # Should not reach here; covered by reserved check above
    return ActivationTriggerResult(
        trigger_class=tc,
        fired=False,
        detection_reason='trigger_class_unhandled',
    )


def _eval_operator_request(conditions: dict, trigger_event: dict) -> ActivationTriggerResult:
    """operator_request always fires when policy is active and operator_id is provided."""
    operator_id = trigger_event.get('operator_id', '')
    if not operator_id:
        return ActivationTriggerResult(
            trigger_class='operator_request',
            fired=False,
            detection_reason='missing operator_id in trigger_event',
        )
    return ActivationTriggerResult(
        trigger_class='operator_request',
        fired=True,
        detection_reason=f'explicit operator request from {operator_id!r}',
    )


def _eval_governance_escalation(conditions: dict, trigger_event: dict) -> ActivationTriggerResult:
    """
    Fires when governance issues meet the severity threshold.

    trigger_event keys:
      issue_types  List[str] — issue_type values from governance detectors
      severities   List[str] — corresponding severity values ('critical'/'warning'/'info')

    condition keys:
      min_severity   str       — minimum severity to qualify ('critical'/'warning'/'info')
      detector_names List[str] — if set, issue_type must be in this list
      require_all    bool      — if True, all named detectors must have qualifying issues
    """
    issue_types = trigger_event.get('issue_types', [])
    severities = trigger_event.get('severities', [])

    min_severity = conditions.get('min_severity', 'warning')
    detector_names = conditions.get('detector_names', [])
    require_all = conditions.get('require_all', False)

    min_order = _SEVERITY_ORDER.get(min_severity, 1)

    # Filter to issues that meet the severity threshold
    qualifying = [
        (itype, sev)
        for itype, sev in zip(issue_types, severities)
        if _SEVERITY_ORDER.get(sev, 999) <= min_order
    ]

    if not qualifying:
        return ActivationTriggerResult(
            trigger_class='governance_escalation',
            fired=False,
            detection_reason=f'no issues with severity>={min_severity!r}',
        )

    # Further filter by detector names if specified
    if detector_names:
        matching = [(itype, sev) for itype, sev in qualifying if itype in detector_names]
        if not matching:
            return ActivationTriggerResult(
                trigger_class='governance_escalation',
                fired=False,
                detection_reason=(
                    f'no qualifying issues match detector_names={detector_names}'
                ),
            )
        if require_all:
            required_set = set(detector_names)
            found_set = {itype for itype, _ in matching}
            if not required_set.issubset(found_set):
                missing = sorted(required_set - found_set)
                return ActivationTriggerResult(
                    trigger_class='governance_escalation',
                    fired=False,
                    detection_reason=(
                        f'require_all=True but missing detector_names: {missing}'
                    ),
                )
        qualifying = matching

    return ActivationTriggerResult(
        trigger_class='governance_escalation',
        fired=True,
        detection_reason=(
            f'{len(qualifying)} qualifying issue(s) at severity>={min_severity!r}: '
            f'{[itype for itype, _ in qualifying[:3]]}'
        ),
    )


def _eval_contradiction_change(conditions: dict, trigger_event: dict) -> ActivationTriggerResult:
    """
    Fires when the count of new contradiction links meets the threshold.

    trigger_event keys:
      new_link_ids  List[int] — memory_links IDs of newly-added contradicts links

    condition keys:
      min_new_links  int  — minimum count required to fire (default 1)
    """
    new_link_ids: List[int] = trigger_event.get('new_link_ids', [])
    min_new_links: int = conditions.get('min_new_links', 1)

    count = len(new_link_ids)
    if count < min_new_links:
        return ActivationTriggerResult(
            trigger_class='contradiction_change',
            fired=False,
            detection_reason=f'{count} new contradiction link(s); threshold={min_new_links}',
        )

    return ActivationTriggerResult(
        trigger_class='contradiction_change',
        fired=True,
        detection_reason=f'{count} new contradiction link(s) >= threshold={min_new_links}',
        triggering_artifact_ids=sorted(new_link_ids),
    )


def _eval_confidence_revision(conditions: dict, trigger_event: dict) -> ActivationTriggerResult:
    """
    Fires when qualifying confidence revisions meet the threshold.

    trigger_event keys:
      revision_ids    List[int] — confidence_revisions IDs
      revision_types  List[str] — corresponding revision_type values

    condition keys:
      min_new_revisions    int  — minimum count required to fire (default 1)
      require_revision_type str  — if set, only revisions of this type qualify
    """
    revision_ids: List[int] = trigger_event.get('revision_ids', [])
    revision_types: List[str] = trigger_event.get('revision_types', [])
    min_new_revisions: int = conditions.get('min_new_revisions', 1)
    require_revision_type: Optional[str] = conditions.get('require_revision_type')

    if require_revision_type:
        qualified_ids = [
            rid
            for rid, rtype in zip(revision_ids, revision_types)
            if rtype == require_revision_type
        ]
    else:
        qualified_ids = list(revision_ids)

    count = len(qualified_ids)
    type_detail = f' (type={require_revision_type!r})' if require_revision_type else ''

    if count < min_new_revisions:
        return ActivationTriggerResult(
            trigger_class='confidence_revision',
            fired=False,
            detection_reason=(
                f'{count} qualifying revision(s){type_detail}; '
                f'threshold={min_new_revisions}'
            ),
        )

    return ActivationTriggerResult(
        trigger_class='confidence_revision',
        fired=True,
        detection_reason=(
            f'{count} qualifying revision(s){type_detail} >= threshold={min_new_revisions}'
        ),
        triggering_artifact_ids=sorted(qualified_ids),
    )


# ---------------------------------------------------------------------------
# Policy persistence
# ---------------------------------------------------------------------------

def create_activation_policy(
    db_path: str,
    name: str,
    trigger_class: str,
    trigger_conditions: dict,
    created_by: str,
    reason: str,
    *,
    priority: int = 100,
    policy_version: str = '1.0.0',
    provenance: Optional[dict] = None,
) -> ActivationPolicy:
    """
    Create a new candidate activation policy and persist it.

    The policy starts in status='candidate'. It must be explicitly activated via
    activate_activation_policy() before evaluate_trigger() will return fired=True.

    Raises ActivationPolicyValidationError if trigger_class or condition keys are invalid.
    Raises ValueError if name, created_by, or reason is empty.
    """
    if not name or not name.strip():
        raise ValueError("'name' must not be empty")
    if not created_by or not created_by.strip():
        raise ValueError("'created_by' must not be empty")
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")

    conditions_json = json.dumps(trigger_conditions, sort_keys=True)
    provenance_json = json.dumps(provenance or {}, sort_keys=True)
    now = _now_utc()

    # Validate before writing — __post_init__ raises ActivationPolicyValidationError
    ActivationPolicy(
        id=None,
        name=name,
        trigger_class=trigger_class,
        trigger_conditions_json=conditions_json,
        status='candidate',
        priority=priority,
        policy_version=policy_version,
        created_by=created_by,
        reason=reason,
        created_at=now,
        activated_at=None,
        activated_by=None,
        superseded_at=None,
        superseded_by_policy_id=None,
        superseded_by_operator=None,
        superseded_reason=None,
        invalidated_at=None,
        invalidated_reason=None,
        provenance_json=provenance_json,
    )

    conn = _mem_connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO activation_policies
                   (name, trigger_class, trigger_conditions_json, status,
                    priority, policy_version, created_by, reason, created_at,
                    provenance_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (name, trigger_class, conditions_json, 'candidate',
                 priority, policy_version, created_by, reason, now,
                 provenance_json),
            )
            row = conn.execute(
                'SELECT * FROM activation_policies WHERE id = ?', (cur.lastrowid,)
            ).fetchone()
            return ActivationPolicy.from_row(row)
    finally:
        conn.close()


def get_activation_policy(db_path: str, policy_id: int) -> ActivationPolicy:
    """Fetch one activation policy by id. Raises ValueError if not found."""
    conn = _mem_connect(db_path)
    try:
        row = conn.execute(
            'SELECT * FROM activation_policies WHERE id = ?', (policy_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Activation policy {policy_id} not found")
        return ActivationPolicy.from_row(row)
    finally:
        conn.close()


def list_activation_policies(
    db_path: str,
    *,
    status: Optional[str] = None,
    trigger_class: Optional[str] = None,
    limit: int = 100,
) -> List[ActivationPolicy]:
    """List activation policies ordered by priority ASC, id ASC."""
    clauses: List[str] = []
    params: list = []
    if status is not None:
        clauses.append('status = ?')
        params.append(status)
    if trigger_class is not None:
        clauses.append('trigger_class = ?')
        params.append(trigger_class)
    where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
    params.append(limit)

    conn = _mem_connect(db_path)
    try:
        rows = conn.execute(
            f'SELECT * FROM activation_policies {where} '
            f'ORDER BY priority ASC, id ASC LIMIT ?',
            params,
        ).fetchall()
        return [ActivationPolicy.from_row(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Policy lifecycle transitions
# ---------------------------------------------------------------------------

def activate_activation_policy(
    db_path: str,
    policy_id: int,
    activated_by: str,
    reason: str,
) -> ActivationPolicy:
    """
    Transition an activation policy from candidate → active.

    Sets activated_at and activated_by. Only candidate policies may be activated.
    Once active, the policy is eligible to fire in evaluate_trigger().

    Raises ActivationPolicyLifecycleError if policy is not 'candidate'.
    Raises ValueError if activated_by or reason is empty.
    """
    if not activated_by or not activated_by.strip():
        raise ValueError("'activated_by' must not be empty")
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")

    now = _now_utc()
    conn = _mem_connect(db_path)
    try:
        with conn:
            row = conn.execute(
                'SELECT * FROM activation_policies WHERE id = ?', (policy_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Activation policy {policy_id} not found")
            if row['status'] != 'candidate':
                raise ActivationPolicyLifecycleError(
                    f"Activation policy {policy_id} has status={row['status']!r}; "
                    "only 'candidate' policies may be activated"
                )

            conn.execute(
                """UPDATE activation_policies
                   SET status = 'active', activated_at = ?, activated_by = ?
                   WHERE id = ?""",
                (now, activated_by, policy_id),
            )
            row = conn.execute(
                'SELECT * FROM activation_policies WHERE id = ?', (policy_id,)
            ).fetchone()
            return ActivationPolicy.from_row(row)
    finally:
        conn.close()


def supersede_activation_policy(
    db_path: str,
    policy_id: int,
    superseded_by_operator: str,
    reason: str,
    *,
    superseded_by_policy_id: Optional[int] = None,
) -> ActivationPolicy:
    """
    Transition an activation policy from active → superseded.

    Writes superseded_at, superseded_reason, superseded_by_operator, and
    optionally superseded_by_policy_id. Does NOT write invalidated_at or
    invalidated_reason — supersession and invalidation columns are mutually
    exclusive per row.

    Only active policies may be superseded.

    Raises ActivationPolicyLifecycleError if policy is not 'active'.
    Raises ValueError if superseded_by_operator or reason is empty.
    """
    if not superseded_by_operator or not superseded_by_operator.strip():
        raise ValueError("'superseded_by_operator' must not be empty")
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")

    now = _now_utc()
    conn = _mem_connect(db_path)
    try:
        with conn:
            row = conn.execute(
                'SELECT * FROM activation_policies WHERE id = ?', (policy_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Activation policy {policy_id} not found")
            if row['status'] != 'active':
                raise ActivationPolicyLifecycleError(
                    f"Activation policy {policy_id} has status={row['status']!r}; "
                    "only 'active' policies may be superseded"
                )

            conn.execute(
                """UPDATE activation_policies
                   SET status = 'superseded',
                       superseded_at = ?,
                       superseded_reason = ?,
                       superseded_by_operator = ?,
                       superseded_by_policy_id = ?
                   WHERE id = ?""",
                (now, reason, superseded_by_operator, superseded_by_policy_id, policy_id),
            )
            row = conn.execute(
                'SELECT * FROM activation_policies WHERE id = ?', (policy_id,)
            ).fetchone()
            return ActivationPolicy.from_row(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Decision logging and replay
# ---------------------------------------------------------------------------

def log_activation_decision(
    db_path: str,
    policy: ActivationPolicy,
    result: ActivationTriggerResult,
    trigger_event: dict,
    *,
    resulting_retrieval_id: Optional[int] = None,
    resulting_assembly_id: Optional[int] = None,
    resulting_transition_id: Optional[int] = None,
) -> int:
    """
    Write one activation_decision_log row. Caller-controlled; never called automatically.

    Logs the full policy snapshot at decision time so that replay never depends
    on the current state of activation_policies. Both firing (fired=True) and
    non-firing (fired=False) decisions are logged when the caller chooses to do so.

    Returns the new decision row id.
    Raises ValueError if policy.id is None (policy must be persisted first).
    """
    if policy.id is None:
        raise ValueError("ActivationPolicy must be persisted (id != None) before logging a decision")

    now = _now_utc()
    artifact_ids_json = (
        json.dumps(sorted(result.triggering_artifact_ids))
        if result.triggering_artifact_ids else None
    )

    conn = _mem_connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO activation_decision_log
                   (policy_id, policy_snapshot_json, trigger_class, trigger_event_json,
                    fired, detection_reason, triggering_artifact_ids_json,
                    triggering_workflow_execution_id, triggering_session_id,
                    resulting_retrieval_id, resulting_assembly_id, resulting_transition_id,
                    detected_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    policy.id,
                    json.dumps(policy.to_dict(), sort_keys=True),
                    result.trigger_class,
                    json.dumps(trigger_event, sort_keys=True),
                    1 if result.fired else 0,
                    result.detection_reason,
                    artifact_ids_json,
                    result.triggering_workflow_execution_id,
                    result.triggering_session_id,
                    resulting_retrieval_id,
                    resulting_assembly_id,
                    resulting_transition_id,
                    now,
                ),
            )
            return cur.lastrowid
    finally:
        conn.close()


def replay_activation_decision(db_path: str, decision_id: int) -> ReplayedActivationDecision:
    """
    Restore a historical activation decision from activation_decision_log.

    Uses policy_snapshot_json captured at decision time. Does NOT re-read or
    re-evaluate the current activation_policies row. Policy supersession after
    the decision does not alter the replayed result.

    Does not trigger retrieval, assembly, or any other cognition action.

    Raises ValueError if decision_id not found.
    """
    conn = _mem_connect(db_path)
    try:
        row = conn.execute(
            'SELECT * FROM activation_decision_log WHERE id = ?', (decision_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise ValueError(f"Activation decision {decision_id} not found in activation_decision_log")

    policy_snapshot = ActivationPolicy.from_dict(json.loads(row['policy_snapshot_json']))
    trigger_event = json.loads(row['trigger_event_json'])

    art_ids_raw = row['triggering_artifact_ids_json']
    triggering_artifact_ids: List[int] = json.loads(art_ids_raw) if art_ids_raw else []

    return ReplayedActivationDecision(
        decision_id=decision_id,
        policy_snapshot=policy_snapshot,
        trigger_class=row['trigger_class'],
        trigger_event=trigger_event,
        fired=bool(row['fired']),
        detection_reason=row['detection_reason'],
        triggering_artifact_ids=triggering_artifact_ids,
        triggering_workflow_execution_id=row['triggering_workflow_execution_id'],
        triggering_session_id=row['triggering_session_id'],
        resulting_retrieval_id=row['resulting_retrieval_id'],
        resulting_assembly_id=row['resulting_assembly_id'],
        resulting_transition_id=row['resulting_transition_id'],
        detected_at=row['detected_at'],
    )


def get_activation_decision(db_path: str, decision_id: int) -> dict:
    """Return a raw activation_decision_log row as a dict. Raises ValueError if not found."""
    conn = _mem_connect(db_path)
    try:
        row = conn.execute(
            'SELECT * FROM activation_decision_log WHERE id = ?', (decision_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Activation decision {decision_id} not found")
        return dict(row)
    finally:
        conn.close()


def list_activation_decisions(
    db_path: str,
    *,
    policy_id: Optional[int] = None,
    fired_only: bool = False,
    limit: int = 100,
) -> List[dict]:
    """Return activation_decision_log rows as dicts, ordered by id DESC."""
    clauses: List[str] = []
    params: list = []
    if policy_id is not None:
        clauses.append('policy_id = ?')
        params.append(policy_id)
    if fired_only:
        clauses.append('fired = 1')
    where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
    params.append(limit)

    conn = _mem_connect(db_path)
    try:
        rows = conn.execute(
            f'SELECT * FROM activation_decision_log {where} ORDER BY id DESC LIMIT ?',
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
