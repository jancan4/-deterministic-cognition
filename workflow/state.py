"""
Workflow execution state models.

WorkflowExecution is the live state of a workflow plan's realization.
WorkflowExecutionLineageEvent records every state change and node outcome.
WorkflowStageExecution is a computed, read-only view of one stage's progress.
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional

VALID_WORKFLOW_EXECUTION_STATES = (
    'initialized', 'ready', 'executing', 'blocked',
    'paused', 'completed', 'failed', 'cancelled',
)

TERMINAL_WORKFLOW_EXECUTION_STATES: FrozenSet[str] = frozenset({'completed', 'cancelled'})

VALID_WORKFLOW_EXECUTION_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    'initialized': frozenset({'ready', 'cancelled'}),
    'ready':       frozenset({'executing', 'cancelled'}),
    'executing':   frozenset({'completed', 'failed', 'blocked', 'paused'}),
    'blocked':     frozenset({'executing', 'failed', 'cancelled'}),
    'paused':      frozenset({'executing', 'cancelled'}),
    'failed':      frozenset({'cancelled'}),
    'completed':   frozenset(),
    'cancelled':   frozenset(),
}

# Lineage event types
EVENT_STATE_TRANSITION = 'state_transition'
EVENT_NODE_COMPLETED = 'node_completed'
EVENT_NODE_FAILED = 'node_failed'
EVENT_NODE_RETRY = 'node_retry'
EVENT_NODE_SUBMITTED = 'node_submitted'
EVENT_STAGE_ADVANCED = 'stage_advanced'


class WorkflowExecutionTransitionError(ValueError):
    pass


def validate_execution_transition(old_state: str, new_state: str) -> None:
    """Raise WorkflowExecutionTransitionError if the transition is invalid."""
    if old_state not in VALID_WORKFLOW_EXECUTION_TRANSITIONS:
        raise WorkflowExecutionTransitionError(
            f"Unknown source state: '{old_state}'"
        )
    if new_state not in VALID_WORKFLOW_EXECUTION_STATES:
        raise WorkflowExecutionTransitionError(
            f"Unknown target state: '{new_state}'"
        )
    if old_state == new_state:
        raise WorkflowExecutionTransitionError(
            f"Self-transition on '{old_state}' is not permitted"
        )
    if new_state not in VALID_WORKFLOW_EXECUTION_TRANSITIONS[old_state]:
        raise WorkflowExecutionTransitionError(
            f"Transition '{old_state}' → '{new_state}' is not permitted. "
            f"Valid targets: {sorted(VALID_WORKFLOW_EXECUTION_TRANSITIONS[old_state])}"
        )


def make_execution_id(plan_id: str, created_at: str) -> str:
    """Deterministic execution_id: SHA-256 of plan_id + created_at."""
    raw = f'{plan_id}:{created_at}'
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class WorkflowExecution:
    execution_id: str
    workflow_id: str
    plan_id: str
    state: str
    active_stage_index: int
    completed_node_ids: List[str]  # sorted; all successfully completed nodes
    failed_node_ids: List[str]     # sorted; nodes whose retries are exhausted
    node_attempts: Dict[str, int]  # node_id → number of attempts made so far
    created_at: str
    updated_at: str
    version: int

    def to_dict(self) -> dict:
        return {
            'execution_id': self.execution_id,
            'workflow_id': self.workflow_id,
            'plan_id': self.plan_id,
            'state': self.state,
            'active_stage_index': self.active_stage_index,
            'completed_node_ids': list(self.completed_node_ids),
            'failed_node_ids': list(self.failed_node_ids),
            'node_attempts': dict(sorted(self.node_attempts.items())),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'version': self.version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'WorkflowExecution':
        return cls(
            execution_id=d['execution_id'],
            workflow_id=d['workflow_id'],
            plan_id=d['plan_id'],
            state=d['state'],
            active_stage_index=d['active_stage_index'],
            completed_node_ids=list(d.get('completed_node_ids', [])),
            failed_node_ids=list(d.get('failed_node_ids', [])),
            node_attempts=dict(d.get('node_attempts', {})),
            created_at=d['created_at'],
            updated_at=d['updated_at'],
            version=d['version'],
        )


@dataclass
class WorkflowExecutionLineageEvent:
    execution_id: str
    event_type: str           # one of the EVENT_* constants
    old_state: Optional[str]  # set for state_transition events
    new_state: Optional[str]  # set for state_transition events
    node_id: Optional[str]    # set for node_* events
    stage_index: int
    reason: str
    created_at: str
    # Optional structured payload. The init event carries workflow_id and plan_id
    # so that lineage is self-contained and identity is recoverable from events alone.
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'execution_id': self.execution_id,
            'event_type': self.event_type,
            'old_state': self.old_state,
            'new_state': self.new_state,
            'node_id': self.node_id,
            'stage_index': self.stage_index,
            'reason': self.reason,
            'created_at': self.created_at,
            'metadata': dict(self.metadata),
        }


@dataclass
class WorkflowStageExecution:
    stage_index: int
    node_ids: List[str]            # all nodes in this stage (from the plan)
    completed_node_ids: List[str]  # subset that completed successfully
    failed_node_ids: List[str]     # subset that failed with exhausted retries
    pending_node_ids: List[str]    # subset not yet completed or failed
    is_complete: bool              # True when all nodes are in completed_node_ids
    has_failures: bool             # True when any nodes are in failed_node_ids

    def to_dict(self) -> dict:
        return {
            'stage_index': self.stage_index,
            'node_ids': list(self.node_ids),
            'completed_node_ids': list(self.completed_node_ids),
            'failed_node_ids': list(self.failed_node_ids),
            'pending_node_ids': list(self.pending_node_ids),
            'is_complete': self.is_complete,
            'has_failures': self.has_failures,
        }
