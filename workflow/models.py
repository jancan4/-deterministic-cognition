"""
Workflow definition models.

All models are deterministic: same input → same JSON representation.
sort_keys=True on all serialization. No random IDs, no network calls.
"""
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            'backoff_seconds': self.backoff_seconds,
            'max_attempts': self.max_attempts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'RetryPolicy':
        return cls(
            max_attempts=d.get('max_attempts', 1),
            backoff_seconds=d.get('backoff_seconds', 0.0),
        )


@dataclass
class WorkflowNode:
    node_id: str
    task_type: str
    task_payload_json: str = '{}'
    dependency_ids: List[str] = field(default_factory=list)
    priority: int = 0
    tags: List[str] = field(default_factory=list)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    governance_requirements: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'node_id': self.node_id,
            'task_type': self.task_type,
            'task_payload_json': self.task_payload_json,
            'dependency_ids': sorted(self.dependency_ids),
            'priority': self.priority,
            'tags': sorted(self.tags),
            'retry_policy': self.retry_policy.to_dict(),
            'governance_requirements': sorted(self.governance_requirements),
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'WorkflowNode':
        return cls(
            node_id=d['node_id'],
            task_type=d['task_type'],
            task_payload_json=d.get('task_payload_json', '{}'),
            dependency_ids=list(d.get('dependency_ids', [])),
            priority=d.get('priority', 0),
            tags=list(d.get('tags', [])),
            retry_policy=RetryPolicy.from_dict(d.get('retry_policy', {})),
            governance_requirements=list(d.get('governance_requirements', [])),
        )


@dataclass
class WorkflowEdge:
    from_node_id: str
    to_node_id: str

    def to_dict(self) -> dict:
        return {
            'from_node_id': self.from_node_id,
            'to_node_id': self.to_node_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'WorkflowEdge':
        return cls(from_node_id=d['from_node_id'], to_node_id=d['to_node_id'])


@dataclass
class WorkflowDefinition:
    workflow_id: str
    name: str
    version: int
    nodes: List[WorkflowNode]
    edges: List[WorkflowEdge]
    metadata_json: str
    created_at: str
    topology_hash: str

    def to_dict(self) -> dict:
        return {
            'workflow_id': self.workflow_id,
            'name': self.name,
            'version': self.version,
            'nodes': [
                n.to_dict()
                for n in sorted(self.nodes, key=lambda n: n.node_id)
            ],
            'edges': [
                e.to_dict()
                for e in sorted(self.edges, key=lambda e: (e.from_node_id, e.to_node_id))
            ],
            'metadata_json': self.metadata_json,
            'created_at': self.created_at,
            'topology_hash': self.topology_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'WorkflowDefinition':
        return cls(
            workflow_id=d['workflow_id'],
            name=d['name'],
            version=d['version'],
            nodes=[WorkflowNode.from_dict(n) for n in d.get('nodes', [])],
            edges=[WorkflowEdge.from_dict(e) for e in d.get('edges', [])],
            metadata_json=d.get('metadata_json', '{}'),
            created_at=d['created_at'],
            topology_hash=d['topology_hash'],
        )


def compute_topology_hash(nodes: List[WorkflowNode], edges: List[WorkflowEdge]) -> str:
    """
    Deterministic SHA-256 hash of the workflow topology.

    Only node_id, task_type, and dependency_ids participate — payload and
    metadata do not affect the structural hash. Nodes and edges are sorted
    before hashing so insertion order never changes the result.
    """
    topology = {
        'edges': [
            e.to_dict()
            for e in sorted(edges, key=lambda e: (e.from_node_id, e.to_node_id))
        ],
        'nodes': [
            {
                'dependency_ids': sorted(n.dependency_ids),
                'node_id': n.node_id,
                'task_type': n.task_type,
            }
            for n in sorted(nodes, key=lambda n: n.node_id)
        ],
    }
    return hashlib.sha256(json.dumps(topology, sort_keys=True).encode()).hexdigest()


@dataclass
class ExecutionStage:
    stage_index: int
    node_ids: List[str]

    def to_dict(self) -> dict:
        return {
            'stage_index': self.stage_index,
            'node_ids': list(self.node_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ExecutionStage':
        return cls(stage_index=d['stage_index'], node_ids=list(d['node_ids']))


@dataclass
class WorkflowExecutionPlan:
    workflow_id: str
    plan_id: str
    version: int
    stages: List[ExecutionStage]
    dependency_snapshot: Dict[str, List[str]]
    generated_at: str
    planner_version: str

    def to_dict(self) -> dict:
        return {
            'workflow_id': self.workflow_id,
            'plan_id': self.plan_id,
            'version': self.version,
            'stages': [s.to_dict() for s in self.stages],
            'dependency_snapshot': {
                k: sorted(v)
                for k, v in sorted(self.dependency_snapshot.items())
            },
            'generated_at': self.generated_at,
            'planner_version': self.planner_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'WorkflowExecutionPlan':
        return cls(
            workflow_id=d['workflow_id'],
            plan_id=d['plan_id'],
            version=d.get('version', 0),
            stages=[ExecutionStage.from_dict(s) for s in d.get('stages', [])],
            dependency_snapshot=dict(d.get('dependency_snapshot', {})),
            generated_at=d['generated_at'],
            planner_version=d['planner_version'],
        )


@dataclass
class WorkflowLineageEvent:
    workflow_id: str
    version: int
    planner_version: str
    validation_result: str
    topology_hash: str
    generated_at: str

    def to_dict(self) -> dict:
        return {
            'workflow_id': self.workflow_id,
            'version': self.version,
            'planner_version': self.planner_version,
            'validation_result': self.validation_result,
            'topology_hash': self.topology_hash,
            'generated_at': self.generated_at,
        }
