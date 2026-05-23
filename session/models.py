"""
Session reconstruction data models.

A session is a deterministic reconstruction of relevant cognition state from
persisted memory, workflow lineage, and runtime snapshots. These models
capture the structured output of reconstruction without embedding any DB or
I/O logic.

Canonical truth remains the lineage and persisted memory events. Session
models are ephemeral: same inputs always produce the same session.
"""
from dataclasses import dataclass, field, fields as _dataclass_fields
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Assembly constants
# ---------------------------------------------------------------------------

CONTEXT_ASSEMBLY_VERSION = '1.1.0'
CHAR_BUDGET_DEFAULT = 12000
ENTRY_BUDGET_DEFAULT = 60

# ---------------------------------------------------------------------------
# Cognition session constants
# ---------------------------------------------------------------------------

VALID_TRANSITION_TYPES = frozenset({
    'session_start', 'memory_drift', 'confidence_revision',
    'contradiction_change', 'operator_rebuild', 'policy_update', 'session_close',
})

VALID_SESSION_STATUSES = frozenset({'active', 'closed', 'abandoned'})


# ---------------------------------------------------------------------------
# Contradiction pair
# ---------------------------------------------------------------------------

@dataclass
class ConflictingPair:
    """
    A snapshot of one active contradicts link where both sides are present
    in the current assembly. Immutable provenance — captured at assembly time
    so replay shows contradictions as they were known, even after retraction.
    """
    link_id: int
    source_id: int
    target_id: int
    created_by: Optional[str]
    reason: Optional[str]
    link_confidence: Optional[int]
    link_created_at: str

    def to_dict(self) -> dict:
        return {
            'link_id': self.link_id,
            'source_id': self.source_id,
            'target_id': self.target_id,
            'created_by': self.created_by,
            'reason': self.reason,
            'link_confidence': self.link_confidence,
            'link_created_at': self.link_created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ConflictingPair':
        return cls(
            link_id=d['link_id'],
            source_id=d['source_id'],
            target_id=d['target_id'],
            created_by=d.get('created_by'),
            reason=d.get('reason'),
            link_confidence=d.get('link_confidence'),
            link_created_at=d['link_created_at'],
        )


# ---------------------------------------------------------------------------
# Activation policy
# ---------------------------------------------------------------------------

@dataclass
class ContextActivationPolicy:
    """
    Governs what gets activated and in what priority order for a session.

    All fields have safe defaults so callers can override only what they need.

    Activation priorities (highest to lowest):
      1. Governance context (governance_rule, architecture_decision)
      2. Unresolved items (status = 'unresolved' or 'proposed')
      3. Workflow-linked memory (tags overlap with active workflow IDs/types)
      4. High-confidence active memory sorted by doctrine priority
      5. Expanded related items

    No embeddings. No semantic search. All scoring is deterministic.
    """
    # Memory retrieval
    tags: List[str] = field(default_factory=list)
    min_confidence: int = 1
    include_unresolved: bool = True
    include_adaptations: bool = True
    expand_related: bool = True

    # Compression mode — only 'none' is valid in Phase 3B; hook for future summarization
    compression_mode: str = 'none'

    # Workflow retrieval
    include_active_workflows: bool = True
    workflow_db_path: Optional[str] = None   # if None, workflow section is skipped
    max_workflows: int = 10

    # Runtime retrieval
    include_runtime_state: bool = True
    runtime_db_path: Optional[str] = None    # if None, runtime section is skipped
    max_runtime_events: int = 5

    # Memory limits before budgeting
    max_memory_candidates: int = 50

    # Context window
    max_chars: int = CHAR_BUDGET_DEFAULT
    max_entries: int = ENTRY_BUDGET_DEFAULT

    def to_dict(self) -> dict:
        return {
            'tags': list(self.tags),
            'min_confidence': self.min_confidence,
            'include_unresolved': self.include_unresolved,
            'include_adaptations': self.include_adaptations,
            'expand_related': self.expand_related,
            'compression_mode': self.compression_mode,
            'include_active_workflows': self.include_active_workflows,
            'workflow_db_path': self.workflow_db_path,
            'max_workflows': self.max_workflows,
            'include_runtime_state': self.include_runtime_state,
            'runtime_db_path': self.runtime_db_path,
            'max_runtime_events': self.max_runtime_events,
            'max_memory_candidates': self.max_memory_candidates,
            'max_chars': self.max_chars,
            'max_entries': self.max_entries,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ContextActivationPolicy':
        """Deserialize from a dict; unknown keys (e.g. include_governance from old logs) are ignored."""
        known = {f.name for f in _dataclass_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Activated memory
# ---------------------------------------------------------------------------

@dataclass
class ActivatedMemory:
    """
    A memory event selected for inclusion in the session context.

    activation_rank is the composite sort key used to determine inclusion
    order during context budgeting. Lower = higher priority.
    """
    memory_id: int
    event_type: str
    title: str
    summary: str
    evidence: Optional[str]
    confidence: int
    status: str
    tags: List[str]
    source: str
    related_ids: List[int]
    created_at: str
    updated_at: str
    is_expanded: bool                                  # True if included via related-expansion
    tag_overlap: int                                   # tags in common with query tags
    activation_rank: Tuple                             # composite sort key; lower = higher priority
    contradiction_ids: List[int] = field(default_factory=list)  # memory_ids of contradicting events in this assembly

    def to_dict(self) -> dict:
        return {
            'memory_id': self.memory_id,
            'event_type': self.event_type,
            'title': self.title,
            'summary': self.summary,
            'evidence': self.evidence,
            'confidence': self.confidence,
            'status': self.status,
            'tags': list(self.tags),
            'source': self.source,
            'related_ids': list(self.related_ids),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'is_expanded': self.is_expanded,
            'tag_overlap': self.tag_overlap,
            'contradiction_ids': list(self.contradiction_ids),
        }

    def render(self) -> str:
        parts = [
            f"[mem:{self.memory_id}] {self.event_type.upper()} "
            f"| confidence={self.confidence} | status={self.status}",
            f"  Title   : {self.title}",
            f"  Summary : {self.summary}",
        ]
        if self.evidence:
            parts.append(f"  Evidence: {self.evidence}")
        if self.tags:
            parts.append(f"  Tags    : {', '.join(self.tags)}")
        if self.related_ids:
            parts.append(f"  Related : {self.related_ids}")
        if self.contradiction_ids:
            refs = ', '.join(f'[mem:{mid}]' for mid in self.contradiction_ids)
            parts.append(f"  Conflicts: {refs}")
        if self.is_expanded:
            parts.append("  [related-expanded]")
        return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Active workflow
# ---------------------------------------------------------------------------

@dataclass
class ActiveWorkflow:
    """
    A non-terminal workflow execution surfaced in the session context.

    Derived from WorkflowExecution via workflow.storage. Not independently
    persisted — reconstructed on each session activation.
    """
    execution_id: str
    workflow_id: str
    plan_id: str
    state: str
    active_stage_index: int
    completed_node_ids: List[str]
    failed_node_ids: List[str]
    node_attempts: Dict[str, int]
    total_lineage_events: int
    updated_at: str

    def to_dict(self) -> dict:
        return {
            'execution_id': self.execution_id,
            'workflow_id': self.workflow_id,
            'plan_id': self.plan_id,
            'state': self.state,
            'active_stage_index': self.active_stage_index,
            'completed_node_ids': list(self.completed_node_ids),
            'failed_node_ids': list(self.failed_node_ids),
            'node_attempts': dict(self.node_attempts),
            'total_lineage_events': self.total_lineage_events,
            'updated_at': self.updated_at,
        }

    def render(self) -> str:
        parts = [
            f"[wf:{self.execution_id[:16]}] {self.workflow_id} | state={self.state}",
            f"  plan_id         : {self.plan_id[:16]}",
            f"  active_stage    : {self.active_stage_index}",
            f"  completed_nodes : {self.completed_node_ids}",
            f"  failed_nodes    : {self.failed_node_ids}",
            f"  lineage_events  : {self.total_lineage_events}",
            f"  updated_at      : {self.updated_at}",
        ]
        if self.node_attempts:
            parts.append(f"  node_attempts   : {self.node_attempts}")
        return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Runtime snapshot
# ---------------------------------------------------------------------------

@dataclass
class RuntimeSnapshot:
    """
    A point-in-time view of a runtime process surfaced in the session context.
    """
    runtime_id: int
    name: str
    state: str
    current_iteration: int
    updated_at: str
    recent_transitions: List[Dict]   # last N lineage events as dicts

    def to_dict(self) -> dict:
        return {
            'runtime_id': self.runtime_id,
            'name': self.name,
            'state': self.state,
            'current_iteration': self.current_iteration,
            'updated_at': self.updated_at,
            'recent_transitions': list(self.recent_transitions),
        }

    def render(self) -> str:
        parts = [
            f"[rt:{self.runtime_id}] {self.name} | state={self.state}",
            f"  iteration  : {self.current_iteration}",
            f"  updated_at : {self.updated_at}",
        ]
        for t in self.recent_transitions:
            parts.append(
                f"  → {t.get('old_state', '?')} → {t['new_state']}  "
                f"({t.get('reason', '')})"
            )
        return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Session context and reconstruction
# ---------------------------------------------------------------------------

@dataclass
class SessionContext:
    """
    The assembled, budget-constrained context for one session.

    Carries the raw structured data for inspection and replay. Use
    SessionReconstruction.render() for the human-readable text form.
    """
    session_id: str
    created_at: str
    policy: ContextActivationPolicy

    # Structured sections — each may be empty if the source was unavailable
    governance_context: List[ActivatedMemory]
    unresolved_items: List[ActivatedMemory]
    active_workflows: List[ActiveWorkflow]
    execution_lineage: List[ActiveWorkflow]  # terminal or recently-updated
    relevant_memory: List[ActivatedMemory]
    active_investigations: List[ActivatedMemory]  # open_question + hypothesis
    runtime_snapshots: List[RuntimeSnapshot]

    # Budget accounting
    total_candidates: int    # all items evaluated before budgeting
    included_entries: int    # items included after budgeting
    char_budget: int
    chars_used: int
    truncated: bool          # True if budget was exhausted

    # Contradiction state — pairs of events in this assembly connected by an active contradicts link.
    # Populated after budgeting; empty list means no contradictions within this window.
    # Default empty for backward compat with pre-v1.1.0 replay paths.
    contradiction_pairs: List[ConflictingPair] = field(default_factory=list)

    # Assembly provenance — default 'unknown' for backward compat with pre-v7 snapshots
    assembly_version: str = 'unknown'

    def to_dict(self) -> dict:
        return {
            'session_id': self.session_id,
            'created_at': self.created_at,
            'assembly_version': self.assembly_version,
            'policy': self.policy.to_dict(),
            'governance_context': [m.to_dict() for m in self.governance_context],
            'unresolved_items': [m.to_dict() for m in self.unresolved_items],
            'active_workflows': [w.to_dict() for w in self.active_workflows],
            'execution_lineage': [w.to_dict() for w in self.execution_lineage],
            'relevant_memory': [m.to_dict() for m in self.relevant_memory],
            'active_investigations': [m.to_dict() for m in self.active_investigations],
            'runtime_snapshots': [r.to_dict() for r in self.runtime_snapshots],
            'total_candidates': self.total_candidates,
            'included_entries': self.included_entries,
            'char_budget': self.char_budget,
            'chars_used': self.chars_used,
            'truncated': self.truncated,
            'contradiction_pairs': [p.to_dict() for p in self.contradiction_pairs],
        }


@dataclass
class AssemblyDivergenceReport:
    """
    Result of verify_assembly_against_current_db().

    Describes how the current memory DB state differs from what was captured
    at assembly time. Diagnostic only — never mutates any database.
    """
    assembly_id: int
    assembly_hash: str
    diverged: bool
    events_added_since_assembly: List[int]             # memory_ids present now but not at assembly
    events_removed_since_assembly: List[int]           # memory_ids at assembly but not present now
    events_rescored_since_assembly: List[int]          # memory_ids in both but with changed confidence
    contradictions_added_since_assembly: List[int]     # link_ids of new active contradicts links between assembled events
    contradictions_retracted_since_assembly: List[int] # link_ids in stored snapshot no longer active


@dataclass
class SessionReconstruction:
    """
    The complete reconstructed session: structured context + rendered text.

    Deterministic: given the same memory_db state, workflow_db state, and
    activation policy, reconstruct() always produces the same session.

    Replayable: SessionContext.to_dict() captures everything needed to
    reproduce or audit the reconstruction without re-querying the databases.
    """
    context: SessionContext
    replayed: bool = False   # True when restored from context_assembly_log via replay_assembly()

    def render(self) -> str:
        """Render the session as human-readable text with labelled sections."""
        sections = []
        ctx = self.context

        header = (
            f"SESSION RECONSTRUCTION\n"
            f"session_id : {ctx.session_id}\n"
            f"created_at : {ctx.created_at}\n"
            f"budget     : {ctx.chars_used}/{ctx.char_budget} chars  "
            f"({ctx.included_entries} entries)"
            + ("  [TRUNCATED]" if ctx.truncated else "")
        )
        sections.append(header)

        if ctx.contradiction_pairs:
            pair_texts = []
            for pair in ctx.contradiction_pairs:
                pair_texts.append(
                    f"[link:{pair.link_id}] [mem:{pair.source_id}] ↔ [mem:{pair.target_id}]"
                    f" | confidence={pair.link_confidence}"
                    f" | by={pair.created_by}"
                    f" | {pair.reason}"
                )
            sections.append(_render_section('CONFLICTING MEMORIES', pair_texts))

        if ctx.governance_context:
            sections.append(_render_section(
                'ACTIVE GOVERNANCE CONTEXT',
                [m.render() for m in ctx.governance_context],
            ))

        if ctx.active_workflows:
            sections.append(_render_section(
                'ACTIVE WORKFLOWS',
                [w.render() for w in ctx.active_workflows],
            ))

        if ctx.execution_lineage:
            sections.append(_render_section(
                'RECENT EXECUTION LINEAGE',
                [w.render() for w in ctx.execution_lineage],
            ))

        if ctx.unresolved_items:
            sections.append(_render_section(
                'UNRESOLVED ITEMS',
                [m.render() for m in ctx.unresolved_items],
            ))

        if ctx.relevant_memory:
            sections.append(_render_section(
                'RELEVANT MEMORY',
                [m.render() for m in ctx.relevant_memory],
            ))

        if ctx.active_investigations:
            sections.append(_render_section(
                'ACTIVE INVESTIGATIONS',
                [m.render() for m in ctx.active_investigations],
            ))

        if ctx.runtime_snapshots:
            sections.append(_render_section(
                'RUNTIME STATE',
                [r.render() for r in ctx.runtime_snapshots],
            ))

        return '\n\n'.join(sections)


def _render_section(title: str, items: List[str]) -> str:
    header = f"## {title}"
    body = '\n\n'.join(items)
    return f"{header}\n\n{body}"


# ---------------------------------------------------------------------------
# Cognition session
# ---------------------------------------------------------------------------

@dataclass
class CognitionSession:
    """
    A durable lifecycle container for a sequence of context assemblies.

    session_key is the policy fingerprint (same value as context_assembly_log.session_id)
    and is used to join back to the assembly log. Multiple active sessions with the
    same session_key are permitted at the schema level; governance detects duplicates.

    Lifecycle: active → closed (explicit) | active → abandoned (governance-detected).
    Transitions are append-only in assembly_transition_log; no backward state changes.
    """
    id: int
    session_key: str
    status: str
    started_at: str
    closed_at: Optional[str]
    closed_reason: Optional[str]
    initial_assembly_id: Optional[int]
    latest_assembly_id: Optional[int]
    assembly_count: int
    db_path: str
    policy_fingerprint_json: str
    metadata_json: Optional[str]

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'session_key': self.session_key,
            'status': self.status,
            'started_at': self.started_at,
            'closed_at': self.closed_at,
            'closed_reason': self.closed_reason,
            'initial_assembly_id': self.initial_assembly_id,
            'latest_assembly_id': self.latest_assembly_id,
            'assembly_count': self.assembly_count,
            'db_path': self.db_path,
            'policy_fingerprint_json': self.policy_fingerprint_json,
            'metadata_json': self.metadata_json,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'CognitionSession':
        return cls(
            id=d['id'],
            session_key=d['session_key'],
            status=d['status'],
            started_at=d['started_at'],
            closed_at=d.get('closed_at'),
            closed_reason=d.get('closed_reason'),
            initial_assembly_id=d.get('initial_assembly_id'),
            latest_assembly_id=d.get('latest_assembly_id'),
            assembly_count=d['assembly_count'],
            db_path=d['db_path'],
            policy_fingerprint_json=d['policy_fingerprint_json'],
            metadata_json=d.get('metadata_json'),
        )

    @classmethod
    def from_row(cls, row) -> 'CognitionSession':
        def _get(key, default=None):
            try:
                return row[key]
            except (IndexError, KeyError):
                return default
        return cls(
            id=row['id'],
            session_key=row['session_key'],
            status=row['status'],
            started_at=row['started_at'],
            closed_at=_get('closed_at'),
            closed_reason=_get('closed_reason'),
            initial_assembly_id=_get('initial_assembly_id'),
            latest_assembly_id=_get('latest_assembly_id'),
            assembly_count=row['assembly_count'],
            db_path=row['db_path'],
            policy_fingerprint_json=row['policy_fingerprint_json'],
            metadata_json=_get('metadata_json'),
        )


# ---------------------------------------------------------------------------
# Assembly transition
# ---------------------------------------------------------------------------

@dataclass
class AssemblyTransition:
    """
    One step in a cognition session's assembly chronology.

    Append-only: rows are never updated or deleted.
    sequence_index is strictly monotonic within a cognition_session_id.
    from_assembly_id is NULL for the first transition (session_start).
    """
    id: int
    cognition_session_id: int
    sequence_index: int
    from_assembly_id: Optional[int]
    to_assembly_id: int
    transition_type: str
    transition_reason: str
    triggered_by: str
    transitioned_at: str
    triggering_retrieval_ids_json: Optional[str]
    triggering_confidence_revision_ids_json: Optional[str]
    triggering_contradiction_link_ids_json: Optional[str]
    provenance_json: Optional[str]

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'cognition_session_id': self.cognition_session_id,
            'sequence_index': self.sequence_index,
            'from_assembly_id': self.from_assembly_id,
            'to_assembly_id': self.to_assembly_id,
            'transition_type': self.transition_type,
            'transition_reason': self.transition_reason,
            'triggered_by': self.triggered_by,
            'transitioned_at': self.transitioned_at,
            'triggering_retrieval_ids_json': self.triggering_retrieval_ids_json,
            'triggering_confidence_revision_ids_json': self.triggering_confidence_revision_ids_json,
            'triggering_contradiction_link_ids_json': self.triggering_contradiction_link_ids_json,
            'provenance_json': self.provenance_json,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'AssemblyTransition':
        return cls(
            id=d['id'],
            cognition_session_id=d['cognition_session_id'],
            sequence_index=d['sequence_index'],
            from_assembly_id=d.get('from_assembly_id'),
            to_assembly_id=d['to_assembly_id'],
            transition_type=d['transition_type'],
            transition_reason=d['transition_reason'],
            triggered_by=d['triggered_by'],
            transitioned_at=d['transitioned_at'],
            triggering_retrieval_ids_json=d.get('triggering_retrieval_ids_json'),
            triggering_confidence_revision_ids_json=d.get(
                'triggering_confidence_revision_ids_json'
            ),
            triggering_contradiction_link_ids_json=d.get(
                'triggering_contradiction_link_ids_json'
            ),
            provenance_json=d.get('provenance_json'),
        )

    @classmethod
    def from_row(cls, row) -> 'AssemblyTransition':
        def _get(key, default=None):
            try:
                return row[key]
            except (IndexError, KeyError):
                return default
        return cls(
            id=row['id'],
            cognition_session_id=row['cognition_session_id'],
            sequence_index=row['sequence_index'],
            from_assembly_id=_get('from_assembly_id'),
            to_assembly_id=row['to_assembly_id'],
            transition_type=row['transition_type'],
            transition_reason=row['transition_reason'],
            triggered_by=row['triggered_by'],
            transitioned_at=row['transitioned_at'],
            triggering_retrieval_ids_json=_get('triggering_retrieval_ids_json'),
            triggering_confidence_revision_ids_json=_get(
                'triggering_confidence_revision_ids_json'
            ),
            triggering_contradiction_link_ids_json=_get(
                'triggering_contradiction_link_ids_json'
            ),
            provenance_json=_get('provenance_json'),
        )


# ---------------------------------------------------------------------------
# Session timeline divergence report
# ---------------------------------------------------------------------------

@dataclass
class SessionTimelineDivergenceReport:
    """
    Aggregated divergence report for a full cognition session timeline.

    assembly_reports is ordered by sequence_index ascending (same order as
    the transitions that produced them). diverged=True if any assembly report
    has diverged=True.
    """
    cognition_session_id: int
    diverged: bool
    assembly_reports: List['AssemblyDivergenceReport']

    def to_dict(self) -> dict:
        return {
            'cognition_session_id': self.cognition_session_id,
            'diverged': self.diverged,
            'assembly_reports': [
                {
                    'assembly_id': r.assembly_id,
                    'assembly_hash': r.assembly_hash,
                    'diverged': r.diverged,
                    'events_added_since_assembly': r.events_added_since_assembly,
                    'events_removed_since_assembly': r.events_removed_since_assembly,
                    'events_rescored_since_assembly': r.events_rescored_since_assembly,
                    'contradictions_added_since_assembly': r.contradictions_added_since_assembly,
                    'contradictions_retracted_since_assembly': (
                        r.contradictions_retracted_since_assembly
                    ),
                }
                for r in self.assembly_reports
            ],
        }
