import json
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional

VALID_STATES = (
    'pending',
    'ready',
    'running',
    'blocked',
    'completed',
    'failed',
    'cancelled',
    'superseded',
)

# completed is NOT terminal — it permits the completed → superseded transition.
TERMINAL_STATES: FrozenSet[str] = frozenset({'cancelled', 'superseded'})

VALID_TASK_TYPES = (
    'research',
    'analysis',
    'validation',
    'calibration',
    'review',
    'governance',
    'implementation',
    'report',
)

VALID_DEPENDENCY_TYPES = (
    'task_completion',
    'governance_approval',
    'validation_outcome',
)

# Directed adjacency map: state → frozenset of valid next states.
# Terminal states have empty sets — no outgoing transitions permitted.
VALID_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    'pending':    frozenset({'ready', 'blocked', 'cancelled'}),
    'ready':      frozenset({'running', 'blocked', 'cancelled'}),
    'running':    frozenset({'completed', 'failed', 'blocked'}),
    'blocked':    frozenset({'ready', 'cancelled'}),
    'failed':     frozenset({'ready', 'cancelled'}),
    'completed':  frozenset({'superseded'}),
    'cancelled':  frozenset(),
    'superseded': frozenset(),
}

PRIORITY_MIN = 1
PRIORITY_MAX = 5


@dataclass
class Task:
    id: int
    title: str
    description: Optional[str]
    task_type: str
    state: str
    priority: int
    actor: str
    tags: List[str]
    metadata: Dict[str, Any]
    created_at: str
    updated_at: str
    version: int

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'task_type': self.task_type,
            'state': self.state,
            'priority': self.priority,
            'actor': self.actor,
            'tags': self.tags,
            'metadata': self.metadata,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'version': self.version,
        }

    @classmethod
    def from_row(cls, row) -> 'Task':
        return cls(
            id=row['id'],
            title=row['title'],
            description=row['description'],
            task_type=row['task_type'],
            state=row['state'],
            priority=row['priority'],
            actor=row['actor'],
            tags=json.loads(row['tags_json'] or '[]'),
            metadata=json.loads(row['metadata_json'] or '{}'),
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            version=row['version'],
        )


@dataclass
class TaskLineageEvent:
    id: int
    task_id: int
    old_state: Optional[str]
    new_state: str
    reason: str
    actor: str
    dependency_snapshot: List[int]
    metadata: Dict[str, Any]
    created_at: str

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'task_id': self.task_id,
            'old_state': self.old_state,
            'new_state': self.new_state,
            'reason': self.reason,
            'actor': self.actor,
            'dependency_snapshot': self.dependency_snapshot,
            'metadata': self.metadata,
            'created_at': self.created_at,
        }

    @classmethod
    def from_row(cls, row) -> 'TaskLineageEvent':
        return cls(
            id=row['id'],
            task_id=row['task_id'],
            old_state=row['old_state'],
            new_state=row['new_state'],
            reason=row['reason'],
            actor=row['actor'],
            dependency_snapshot=json.loads(row['dependency_snapshot'] or '[]'),
            metadata=json.loads(row['metadata_json'] or '{}'),
            created_at=row['created_at'],
        )


@dataclass
class TaskDependency:
    id: int
    task_id: int
    depends_on_id: int
    dependency_type: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'task_id': self.task_id,
            'depends_on_id': self.depends_on_id,
            'dependency_type': self.dependency_type,
            'created_at': self.created_at,
        }

    @classmethod
    def from_row(cls, row) -> 'TaskDependency':
        return cls(
            id=row['id'],
            task_id=row['task_id'],
            depends_on_id=row['depends_on_id'],
            dependency_type=row['dependency_type'],
            created_at=row['created_at'],
        )
