"""
Deterministic workflow DAG validation.

All errors are collected in a single pass before returning — the caller
sees every problem at once, not just the first. Validation is a pure
function: same WorkflowDefinition always produces the same ValidationResult.

Validators run in this order:
  1. Duplicate node_ids
  2. Empty node_id / task_type
  3. Missing dependency references
  4. Invalid retry policies
  5. Invalid task_payload_json
  6. Circular dependencies (Kahn's, only when no missing-dep errors)
  7. Disconnected graph (only for multi-node workflows)

Errors in the returned list are sorted alphabetically so test assertions
are independent of the order validators run internally.
"""
import json
from dataclasses import dataclass, field
from typing import Dict, List, Set

from .models import WorkflowDefinition, WorkflowNode


@dataclass
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {'valid': self.valid, 'errors': list(self.errors)}


def validate_workflow(definition: WorkflowDefinition) -> ValidationResult:
    """Run all validators and return a consolidated ValidationResult."""
    errors: List[str] = []

    node_ids = [n.node_id for n in definition.nodes]
    node_id_set: Set[str] = set(node_ids)

    # 1. Duplicate node_ids.
    seen: Set[str] = set()
    for nid in node_ids:
        if nid in seen:
            errors.append(f"Duplicate node_id: '{nid}'")
        seen.add(nid)

    # 2. Empty node_id or task_type.
    for node in definition.nodes:
        if not node.node_id or not node.node_id.strip():
            errors.append("Node has empty node_id")
        if not node.task_type or not node.task_type.strip():
            errors.append(f"Node '{node.node_id}' has empty task_type")

    # 3. Missing dependency references.
    for node in definition.nodes:
        for dep_id in node.dependency_ids:
            if dep_id not in node_id_set:
                errors.append(
                    f"Node '{node.node_id}' depends on unknown node '{dep_id}'"
                )

    # 4. Invalid retry policies.
    for node in definition.nodes:
        if node.retry_policy.max_attempts < 1:
            errors.append(
                f"Node '{node.node_id}' retry_policy.max_attempts must be >= 1,"
                f" got {node.retry_policy.max_attempts}"
            )
        if node.retry_policy.backoff_seconds < 0:
            errors.append(
                f"Node '{node.node_id}' retry_policy.backoff_seconds must be >= 0,"
                f" got {node.retry_policy.backoff_seconds}"
            )

    # 5. Invalid task_payload_json.
    for node in definition.nodes:
        try:
            json.loads(node.task_payload_json)
        except (json.JSONDecodeError, ValueError):
            errors.append(
                f"Node '{node.node_id}' has invalid task_payload_json"
            )

    # 6. Circular dependencies — only meaningful if no missing-dep errors
    # (a missing dep would cause false cycle reports).
    has_missing_dep_errors = any('unknown node' in e for e in errors)
    if not has_missing_dep_errors:
        errors.extend(_detect_cycles(definition.nodes))

    # 7. Disconnected graph (skip for single-node workflows).
    if len(definition.nodes) > 1:
        errors.extend(_detect_disconnected(definition.nodes))

    errors_sorted = sorted(errors)
    return ValidationResult(valid=len(errors_sorted) == 0, errors=errors_sorted)


def _detect_cycles(nodes: List[WorkflowNode]) -> List[str]:
    """
    Kahn's algorithm — returns a single error string if any cycle is found.

    Queue is kept sorted at every step to ensure deterministic traversal
    regardless of dict insertion order.
    """
    adj: Dict[str, List[str]] = {n.node_id: [] for n in nodes}
    in_degree: Dict[str, int] = {n.node_id: 0 for n in nodes}

    for node in nodes:
        for dep_id in node.dependency_ids:
            if dep_id in adj:  # guard against already-reported missing deps
                adj[dep_id].append(node.node_id)
                in_degree[node.node_id] += 1

    queue = sorted(nid for nid, deg in in_degree.items() if deg == 0)
    processed = 0

    while queue:
        nid = queue.pop(0)
        processed += 1
        for neighbor in sorted(adj[nid]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
                queue.sort()

    if processed < len(nodes):
        cycle_nodes = sorted(nid for nid, deg in in_degree.items() if deg > 0)
        return [f"Circular dependency detected involving nodes: {cycle_nodes}"]
    return []


def _detect_disconnected(nodes: List[WorkflowNode]) -> List[str]:
    """
    Union-find on the undirected version of the dependency graph.

    Returns an error listing all components if the graph has more than one
    weakly connected component.
    """
    parent = {n.node_id: n.node_id for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for node in nodes:
        for dep_id in node.dependency_ids:
            if dep_id in parent:
                union(node.node_id, dep_id)

    roots = {find(n.node_id) for n in nodes}
    if len(roots) > 1:
        components: Dict[str, List[str]] = {}
        for node in nodes:
            root = find(node.node_id)
            components.setdefault(root, []).append(node.node_id)
        sorted_components = sorted(sorted(v) for v in components.values())
        return [f"Workflow graph is disconnected: components {sorted_components}"]
    return []
