"""
Workflow service layer.

High-level API for defining, validating, and planning workflow definitions.
All functions are deterministic: same structural inputs → same outputs.
No database, no network, no hidden side effects.
"""
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .models import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowExecutionPlan,
    WorkflowLineageEvent,
    WorkflowNode,
    compute_topology_hash,
)
from .planner import build_execution_plan, build_workflow_lineage
from .validation import ValidationResult, validate_workflow


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _derive_edges(nodes: List[WorkflowNode]) -> List[WorkflowEdge]:
    """
    Derive an explicit, deduplicated, sorted edge list from node dependency_ids.

    Edges are sorted by (from_node_id, to_node_id) for deterministic output.
    """
    seen = set()
    edges: List[WorkflowEdge] = []
    for node in nodes:
        for dep_id in node.dependency_ids:
            key = (dep_id, node.node_id)
            if key not in seen:
                seen.add(key)
                edges.append(WorkflowEdge(from_node_id=dep_id, to_node_id=node.node_id))
    return sorted(edges, key=lambda e: (e.from_node_id, e.to_node_id))


def define_workflow(
    workflow_id: str,
    name: str,
    nodes: List[WorkflowNode],
    version: int = 1,
    metadata: Optional[Dict] = None,
) -> WorkflowDefinition:
    """
    Construct a WorkflowDefinition from a flat node list.

    Edges are derived automatically from each node's dependency_ids.
    topology_hash is computed deterministically over the structural graph
    (node_ids, task_types, and dependency relationships only — payload and
    metadata do not affect the hash).
    """
    edges = _derive_edges(nodes)
    topology_hash = compute_topology_hash(nodes, edges)
    metadata_json = json.dumps(metadata or {}, sort_keys=True)

    return WorkflowDefinition(
        workflow_id=workflow_id,
        name=name,
        version=version,
        nodes=list(nodes),
        edges=edges,
        metadata_json=metadata_json,
        created_at=_now(),
        topology_hash=topology_hash,
    )


def plan_workflow(
    definition: WorkflowDefinition,
) -> Tuple[ValidationResult, Optional[WorkflowExecutionPlan], WorkflowLineageEvent]:
    """
    Validate and plan a workflow definition.

    Returns (validation_result, execution_plan, lineage_event).
    execution_plan is None when validation fails; lineage_event is always
    produced so every planning attempt has an immutable audit record.
    """
    validation_result = validate_workflow(definition)
    lineage = build_workflow_lineage(definition, validation_result)

    if not validation_result.valid:
        return validation_result, None, lineage

    plan = build_execution_plan(definition, validation_result)
    return validation_result, plan, lineage
