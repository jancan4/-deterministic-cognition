"""
Deterministic execution planner.

Converts a validated WorkflowDefinition into a WorkflowExecutionPlan.

Algorithm:
  1. Kahn's topological sort with a sorted queue — deterministic traversal
     regardless of dict/set ordering.
  2. Stage grouping: all nodes that become zero-in-degree in the same round
     form one ExecutionStage.
  3. Within each stage: sorted by (priority ascending, node_id lexicographic)
     so execution order is fully determined by the graph structure and node
     metadata, never by insertion order or Python runtime state.

plan_id is a SHA-256 hash of (workflow_id, version, planner_version) — same
workflow identity and planner version always produce the same plan_id, making
the plan replayable and audit-comparable across runs.
"""
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from .models import (
    ExecutionStage,
    WorkflowDefinition,
    WorkflowExecutionPlan,
    WorkflowLineageEvent,
    WorkflowNode,
)
from .validation import ValidationResult

PLANNER_VERSION = '1.0.0'


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _plan_id(workflow_id: str, version: int, planner_version: str) -> str:
    raw = f'{workflow_id}:{version}:{planner_version}'
    return hashlib.sha256(raw.encode()).hexdigest()


def build_execution_plan(
    definition: WorkflowDefinition,
    validation_result: ValidationResult,
) -> WorkflowExecutionPlan:
    """
    Build a deterministic execution plan from a validated WorkflowDefinition.

    Raises ValueError if validation_result.valid is False — an invalid
    workflow cannot produce a meaningful execution plan.
    """
    if not validation_result.valid:
        raise ValueError(
            f"Cannot build execution plan for invalid workflow "
            f"'{definition.workflow_id}': {validation_result.errors}"
        )

    node_map: Dict[str, WorkflowNode] = {n.node_id: n for n in definition.nodes}

    # Build adjacency list and in-degree map for Kahn's algorithm.
    adj: Dict[str, List[str]] = {nid: [] for nid in node_map}
    in_degree: Dict[str, int] = {nid: 0 for nid in node_map}

    for node in definition.nodes:
        for dep_id in node.dependency_ids:
            adj[dep_id].append(node.node_id)
            in_degree[node.node_id] += 1

    stages: List[ExecutionStage] = []
    stage_index = 0
    remaining = dict(in_degree)

    while True:
        # Collect all nodes whose in-degree is now 0 — these form the next stage.
        ready_ids = sorted(nid for nid, deg in remaining.items() if deg == 0)
        if not ready_ids:
            break

        # Within the stage: sort by (priority asc, node_id lex) for full determinism.
        stage_node_ids = sorted(
            ready_ids,
            key=lambda nid: (node_map[nid].priority, nid),
        )
        stages.append(ExecutionStage(stage_index=stage_index, node_ids=stage_node_ids))
        stage_index += 1

        # Remove processed nodes and update in-degrees of their successors.
        for nid in ready_ids:
            del remaining[nid]
            for neighbor in adj[nid]:
                remaining[neighbor] -= 1

    # Dependency snapshot: frozen record of each node's deps at plan time.
    dependency_snapshot: Dict[str, List[str]] = {
        n.node_id: sorted(n.dependency_ids) for n in definition.nodes
    }

    return WorkflowExecutionPlan(
        workflow_id=definition.workflow_id,
        plan_id=_plan_id(definition.workflow_id, definition.version, PLANNER_VERSION),
        stages=stages,
        dependency_snapshot=dependency_snapshot,
        generated_at=_now(),
        planner_version=PLANNER_VERSION,
    )


def get_ready_nodes(
    plan: WorkflowExecutionPlan,
    completed_node_ids: Set[str],
) -> List[str]:
    """
    Return node_ids whose dependencies are all satisfied.

    Traverses stages in order so results respect the plan's execution
    sequence — nodes earlier in the plan appear first in the output.
    """
    ready: List[str] = []
    for stage in plan.stages:
        for nid in stage.node_ids:
            if nid in completed_node_ids:
                continue
            deps = plan.dependency_snapshot.get(nid, [])
            if all(dep in completed_node_ids for dep in deps):
                ready.append(nid)
    return ready


def get_blocked_nodes(
    plan: WorkflowExecutionPlan,
    completed_node_ids: Set[str],
) -> List[str]:
    """
    Return node_ids that have at least one unresolved dependency.

    Traverses stages in order; does not include already-completed nodes.
    """
    blocked: List[str] = []
    for stage in plan.stages:
        for nid in stage.node_ids:
            if nid in completed_node_ids:
                continue
            deps = plan.dependency_snapshot.get(nid, [])
            if not all(dep in completed_node_ids for dep in deps):
                blocked.append(nid)
    return blocked


def build_workflow_lineage(
    definition: WorkflowDefinition,
    validation_result: ValidationResult,
) -> WorkflowLineageEvent:
    """
    Produce a lineage event capturing the workflow generation context.

    Called regardless of validation outcome so every planning attempt —
    successful or not — has an immutable lineage record.
    """
    if validation_result.valid:
        validation_summary = 'valid'
    else:
        validation_summary = '; '.join(validation_result.errors)

    return WorkflowLineageEvent(
        workflow_id=definition.workflow_id,
        version=definition.version,
        planner_version=PLANNER_VERSION,
        validation_result=validation_summary,
        topology_hash=definition.topology_hash,
        generated_at=_now(),
    )
