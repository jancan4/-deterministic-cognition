import json
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

VALID_RUNTIME_STATES = (
    'initialized',
    'idle',
    'polling',
    'executing',
    'checkpointing',
    'paused',
    'interrupted',
    'recovering',
    'failed',
    'stopped',
)

# stopped is the only terminal state; paused allows resumption.
TERMINAL_RUNTIME_STATES: FrozenSet[str] = frozenset({'stopped'})

VALID_RUNTIME_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    # initialized → interrupted removed: a runtime that has never run cannot be interrupted.
    'initialized':   frozenset({'idle', 'stopped'}),
    'idle':          frozenset({'polling', 'paused', 'stopped', 'interrupted'}),
    'polling':       frozenset({'executing', 'idle', 'checkpointing', 'interrupted', 'paused', 'stopped'}),
    'executing':     frozenset({'polling', 'checkpointing', 'idle', 'interrupted', 'paused'}),
    'checkpointing': frozenset({'idle', 'polling', 'interrupted', 'paused'}),
    'paused':        frozenset({'idle', 'recovering', 'stopped'}),
    'interrupted':   frozenset({'recovering', 'stopped'}),
    'recovering':    frozenset({'idle', 'failed'}),
    'failed':        frozenset({'recovering', 'stopped'}),
    'stopped':       frozenset(),
}


class TransitionError(ValueError):
    pass


def can_transition(old_state: str, new_state: str) -> bool:
    return new_state in VALID_RUNTIME_TRANSITIONS.get(old_state, frozenset())


def validate_runtime_transition(old_state: str, new_state: str) -> None:
    if old_state not in VALID_RUNTIME_TRANSITIONS:
        raise TransitionError(f"Unknown source state: '{old_state}'")
    if new_state not in VALID_RUNTIME_STATES:
        raise TransitionError(f"Unknown target state: '{new_state}'")
    if old_state == new_state:
        raise TransitionError(f"Self-transition on '{old_state}' is not permitted")
    if not can_transition(old_state, new_state):
        valid = sorted(VALID_RUNTIME_TRANSITIONS[old_state])
        label = valid if valid else ['(none — terminal state)']
        raise TransitionError(
            f"Invalid transition '{old_state}' → '{new_state}'. Valid: {label}"
        )


@dataclass
class RuntimeConfig:
    actor: str
    max_iterations: Optional[int] = None
    poll_interval_s: float = 0.0
    max_retries: int = 3
    checkpoint_every: int = 1
    # Explicit opt-in required to run without a max_iterations bound or
    # should_stop callback. False by default so accidental unbounded loops
    # are caught at call time rather than running indefinitely.
    allow_unbounded: bool = False

    def __post_init__(self) -> None:
        if self.checkpoint_every < 1:
            raise ValueError(
                f"checkpoint_every must be >= 1, got {self.checkpoint_every}"
            )

    def to_dict(self) -> dict:
        return {
            'actor': self.actor,
            'max_iterations': self.max_iterations,
            'poll_interval_s': self.poll_interval_s,
            'max_retries': self.max_retries,
            'checkpoint_every': self.checkpoint_every,
            'allow_unbounded': self.allow_unbounded,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'RuntimeConfig':
        return cls(
            actor=d['actor'],
            max_iterations=d.get('max_iterations'),
            poll_interval_s=d.get('poll_interval_s', 0.0),
            max_retries=d.get('max_retries', 3),
            checkpoint_every=d.get('checkpoint_every', 1),
            allow_unbounded=d.get('allow_unbounded', False),
        )


@dataclass
class Runtime:
    id: int
    name: str
    state: str
    orchestration_db: str
    config: RuntimeConfig
    current_iteration: int
    created_at: str
    updated_at: str
    version: int

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'name': self.name,
            'state': self.state,
            'orchestration_db': self.orchestration_db,
            'config': self.config.to_dict(),
            'current_iteration': self.current_iteration,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'version': self.version,
        }

    @classmethod
    def from_row(cls, row) -> 'Runtime':
        return cls(
            id=row['id'],
            name=row['name'],
            state=row['state'],
            orchestration_db=row['orchestration_db'],
            config=RuntimeConfig.from_dict(json.loads(row['config_json'] or '{}')),
            current_iteration=row['current_iteration'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            version=row['version'],
        )


@dataclass
class RuntimeLineageEvent:
    id: int
    runtime_id: int
    old_state: Optional[str]
    new_state: str
    reason: str
    iteration: int
    metadata: Dict[str, Any]
    created_at: str

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'runtime_id': self.runtime_id,
            'old_state': self.old_state,
            'new_state': self.new_state,
            'reason': self.reason,
            'iteration': self.iteration,
            'metadata': self.metadata,
            'created_at': self.created_at,
        }

    @classmethod
    def from_row(cls, row) -> 'RuntimeLineageEvent':
        return cls(
            id=row['id'],
            runtime_id=row['runtime_id'],
            old_state=row['old_state'],
            new_state=row['new_state'],
            reason=row['reason'],
            iteration=row['iteration'],
            metadata=json.loads(row['metadata_json'] or '{}'),
            created_at=row['created_at'],
        )


@dataclass
class Checkpoint:
    id: int
    runtime_id: int
    iteration: int
    state: Dict[str, Any]
    reason: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'runtime_id': self.runtime_id,
            'iteration': self.iteration,
            'state': self.state,
            'reason': self.reason,
            'created_at': self.created_at,
        }

    @classmethod
    def from_row(cls, row) -> 'Checkpoint':
        return cls(
            id=row['id'],
            runtime_id=row['runtime_id'],
            iteration=row['iteration'],
            state=json.loads(row['state_json'] or '{}'),
            reason=row['reason'],
            created_at=row['created_at'],
        )


@dataclass
class RunResult:
    runtime_id: int
    iterations_completed: int
    tasks_executed: int
    stopped_reason: str
    final_state: str

    def to_dict(self) -> dict:
        return {
            'runtime_id': self.runtime_id,
            'iterations_completed': self.iterations_completed,
            'tasks_executed': self.tasks_executed,
            'stopped_reason': self.stopped_reason,
            'final_state': self.final_state,
        }
