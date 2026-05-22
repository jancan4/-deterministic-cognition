"""Tests for workflow models: construction, serialization, and topology hashing."""
import json

import pytest

from workflow.models import (
    ExecutionStage,
    RetryPolicy,
    WorkflowEdge,
    WorkflowExecutionPlan,
    WorkflowLineageEvent,
    WorkflowNode,
    compute_topology_hash,
)


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------

def test_retry_policy_defaults():
    rp = RetryPolicy()
    assert rp.max_attempts == 1
    assert rp.backoff_seconds == 0.0


def test_retry_policy_to_dict_contains_both_fields():
    rp = RetryPolicy(max_attempts=3, backoff_seconds=2.5)
    d = rp.to_dict()
    assert d['max_attempts'] == 3
    assert d['backoff_seconds'] == 2.5


def test_retry_policy_roundtrip():
    rp = RetryPolicy(max_attempts=5, backoff_seconds=1.5)
    assert RetryPolicy.from_dict(rp.to_dict()) == rp


def test_retry_policy_from_dict_defaults():
    rp = RetryPolicy.from_dict({})
    assert rp.max_attempts == 1
    assert rp.backoff_seconds == 0.0


# ---------------------------------------------------------------------------
# WorkflowNode
# ---------------------------------------------------------------------------

def test_workflow_node_defaults():
    n = WorkflowNode(node_id='a', task_type='research')
    assert n.task_payload_json == '{}'
    assert n.dependency_ids == []
    assert n.priority == 0
    assert n.tags == []
    assert n.governance_requirements == []
    assert n.retry_policy == RetryPolicy()


def test_workflow_node_to_dict_sorts_dependency_ids():
    n = WorkflowNode(node_id='a', task_type='t', dependency_ids=['c', 'b', 'a'])
    assert n.to_dict()['dependency_ids'] == ['a', 'b', 'c']


def test_workflow_node_to_dict_sorts_tags():
    n = WorkflowNode(node_id='a', task_type='t', tags=['z', 'a', 'm'])
    assert n.to_dict()['tags'] == ['a', 'm', 'z']


def test_workflow_node_to_dict_sorts_governance_requirements():
    n = WorkflowNode(node_id='a', task_type='t', governance_requirements=['human', 'quant'])
    assert n.to_dict()['governance_requirements'] == ['human', 'quant']


def test_workflow_node_roundtrip():
    n = WorkflowNode(
        node_id='n1',
        task_type='analysis',
        task_payload_json='{"key": "val"}',
        dependency_ids=['n0'],
        priority=2,
        tags=['fx', 'macro'],
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=1.0),
        governance_requirements=['quant_validation'],
    )
    assert WorkflowNode.from_dict(n.to_dict()) == n


def test_workflow_node_from_dict_defaults():
    n = WorkflowNode.from_dict({'node_id': 'a', 'task_type': 't'})
    assert n.task_payload_json == '{}'
    assert n.dependency_ids == []
    assert n.priority == 0


# ---------------------------------------------------------------------------
# WorkflowEdge
# ---------------------------------------------------------------------------

def test_workflow_edge_to_dict():
    e = WorkflowEdge(from_node_id='a', to_node_id='b')
    assert e.to_dict() == {'from_node_id': 'a', 'to_node_id': 'b'}


def test_workflow_edge_roundtrip():
    e = WorkflowEdge(from_node_id='x', to_node_id='y')
    assert WorkflowEdge.from_dict(e.to_dict()) == e


# ---------------------------------------------------------------------------
# compute_topology_hash
# ---------------------------------------------------------------------------

def test_topology_hash_is_deterministic():
    nodes = [WorkflowNode('a', 'research'), WorkflowNode('b', 'analysis', dependency_ids=['a'])]
    edges = [WorkflowEdge('a', 'b')]
    assert compute_topology_hash(nodes, edges) == compute_topology_hash(nodes, edges)


def test_topology_hash_is_64_char_hex():
    nodes = [WorkflowNode('a', 'research')]
    h = compute_topology_hash(nodes, [])
    assert len(h) == 64
    int(h, 16)  # raises if not valid hex


def test_topology_hash_independent_of_node_list_order():
    n_a = WorkflowNode('a', 'research')
    n_b = WorkflowNode('b', 'analysis', dependency_ids=['a'])
    e = WorkflowEdge('a', 'b')
    assert compute_topology_hash([n_a, n_b], [e]) == compute_topology_hash([n_b, n_a], [e])


def test_topology_hash_changes_when_edge_added():
    nodes = [WorkflowNode('a', 'research'), WorkflowNode('b', 'analysis')]
    h1 = compute_topology_hash(nodes, [])
    h2 = compute_topology_hash(
        [WorkflowNode('a', 'research'), WorkflowNode('b', 'analysis', dependency_ids=['a'])],
        [WorkflowEdge('a', 'b')],
    )
    assert h1 != h2


def test_topology_hash_changes_when_task_type_changes():
    n1 = [WorkflowNode('a', 'research')]
    n2 = [WorkflowNode('a', 'analysis')]
    assert compute_topology_hash(n1, []) != compute_topology_hash(n2, [])


def test_topology_hash_unchanged_by_payload_change():
    """Payload is not structural — changing it must not alter the topology hash."""
    n1 = [WorkflowNode('a', 'research', task_payload_json='{"x": 1}')]
    n2 = [WorkflowNode('a', 'research', task_payload_json='{"x": 999}')]
    assert compute_topology_hash(n1, []) == compute_topology_hash(n2, [])


# ---------------------------------------------------------------------------
# ExecutionStage
# ---------------------------------------------------------------------------

def test_execution_stage_to_dict():
    s = ExecutionStage(stage_index=2, node_ids=['b', 'a'])
    d = s.to_dict()
    assert d['stage_index'] == 2
    assert d['node_ids'] == ['b', 'a']  # insertion order preserved


def test_execution_stage_roundtrip():
    s = ExecutionStage(stage_index=0, node_ids=['a', 'b'])
    assert ExecutionStage.from_dict(s.to_dict()) == s


# ---------------------------------------------------------------------------
# WorkflowExecutionPlan
# ---------------------------------------------------------------------------

def test_execution_plan_to_dict_sorts_dependency_snapshot_keys():
    plan = WorkflowExecutionPlan(
        workflow_id='wf', plan_id='pid', version=1,
        stages=[ExecutionStage(0, ['a'])],
        dependency_snapshot={'z': [], 'a': []},
        generated_at='2026-01-01T00:00:00Z',
        planner_version='1.0.0',
    )
    keys = list(plan.to_dict()['dependency_snapshot'].keys())
    assert keys == ['a', 'z']


def test_execution_plan_to_dict_sorts_snapshot_dep_lists():
    plan = WorkflowExecutionPlan(
        workflow_id='wf', plan_id='pid', version=1,
        stages=[ExecutionStage(0, ['c'])],
        dependency_snapshot={'c': ['b', 'a']},
        generated_at='2026-01-01T00:00:00Z',
        planner_version='1.0.0',
    )
    assert plan.to_dict()['dependency_snapshot']['c'] == ['a', 'b']


def test_execution_plan_roundtrip():
    plan = WorkflowExecutionPlan(
        workflow_id='wf', plan_id='abc', version=1,
        stages=[ExecutionStage(0, ['a']), ExecutionStage(1, ['b'])],
        dependency_snapshot={'a': [], 'b': ['a']},
        generated_at='2026-01-01T00:00:00Z',
        planner_version='1.0.0',
    )
    assert WorkflowExecutionPlan.from_dict(plan.to_dict()) == plan


# ---------------------------------------------------------------------------
# WorkflowLineageEvent
# ---------------------------------------------------------------------------

def test_workflow_lineage_event_to_dict():
    evt = WorkflowLineageEvent(
        workflow_id='wf', version=2, planner_version='1.0.0',
        validation_result='valid', topology_hash='abc123',
        generated_at='2026-01-01T00:00:00Z',
    )
    d = evt.to_dict()
    assert d['workflow_id'] == 'wf'
    assert d['version'] == 2
    assert d['validation_result'] == 'valid'
