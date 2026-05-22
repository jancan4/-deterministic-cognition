"""Tests for workflow service layer: define_workflow and plan_workflow."""
import json

import pytest

from workflow.models import WorkflowEdge, WorkflowNode
from workflow.service import define_workflow, plan_workflow


def _node(node_id, task_type='research', dep_ids=None):
    return WorkflowNode(
        node_id=node_id,
        task_type=task_type,
        dependency_ids=dep_ids or [],
    )


# ---------------------------------------------------------------------------
# define_workflow — basic construction
# ---------------------------------------------------------------------------

def test_define_workflow_sets_workflow_id():
    wf = define_workflow('my-wf', 'My Workflow', [_node('a')])
    assert wf.workflow_id == 'my-wf'


def test_define_workflow_sets_name():
    wf = define_workflow('wf', 'Pipeline Alpha', [_node('a')])
    assert wf.name == 'Pipeline Alpha'


def test_define_workflow_default_version_is_1():
    wf = define_workflow('wf', 'W', [_node('a')])
    assert wf.version == 1


def test_define_workflow_custom_version():
    wf = define_workflow('wf', 'W', [_node('a')], version=7)
    assert wf.version == 7


def test_define_workflow_preserves_all_nodes():
    nodes = [_node('a'), _node('b', dep_ids=['a']), _node('c', dep_ids=['b'])]
    wf = define_workflow('wf', 'W', nodes)
    assert len(wf.nodes) == 3


# ---------------------------------------------------------------------------
# define_workflow — edge derivation
# ---------------------------------------------------------------------------

def test_define_workflow_no_deps_no_edges():
    wf = define_workflow('wf', 'W', [_node('a')])
    assert wf.edges == []


def test_define_workflow_derives_edges_from_dep_ids():
    wf = define_workflow('wf', 'W', [_node('a'), _node('b', dep_ids=['a'])])
    assert len(wf.edges) == 1
    assert wf.edges[0].from_node_id == 'a'
    assert wf.edges[0].to_node_id == 'b'


def test_define_workflow_star_topology_produces_correct_edges():
    wf = define_workflow('wf', 'W', [
        _node('root'),
        _node('a', dep_ids=['root']),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['root']),
    ])
    assert len(wf.edges) == 3
    targets = sorted(e.to_node_id for e in wf.edges)
    assert targets == ['a', 'b', 'c']


def test_define_workflow_edges_sorted_by_from_then_to():
    wf = define_workflow('wf', 'W', [
        _node('root'),
        _node('z', dep_ids=['root']),
        _node('a', dep_ids=['root']),
    ])
    to_ids = [e.to_node_id for e in wf.edges]
    assert to_ids == sorted(to_ids)


def test_define_workflow_deduplicates_edges():
    """Two nodes declaring the same dep should not produce duplicate edges."""
    wf = define_workflow('wf', 'W', [
        _node('root'),
        _node('a', dep_ids=['root', 'root']),  # same dep listed twice
    ])
    edges_to_a = [e for e in wf.edges if e.to_node_id == 'a']
    assert len(edges_to_a) == 1


# ---------------------------------------------------------------------------
# define_workflow — topology hash
# ---------------------------------------------------------------------------

def test_topology_hash_is_deterministic():
    nodes = [_node('a'), _node('b', dep_ids=['a'])]
    wf1 = define_workflow('wf', 'W', nodes)
    wf2 = define_workflow('wf', 'W', nodes)
    assert wf1.topology_hash == wf2.topology_hash


def test_topology_hash_changes_when_topology_changes():
    wf1 = define_workflow('wf', 'W', [_node('a')])
    wf2 = define_workflow('wf', 'W', [_node('a'), _node('b', dep_ids=['a'])])
    assert wf1.topology_hash != wf2.topology_hash


def test_topology_hash_unchanged_by_metadata():
    nodes = [_node('a')]
    wf1 = define_workflow('wf', 'W', nodes, metadata={'env': 'dev'})
    wf2 = define_workflow('wf', 'W', nodes, metadata={'env': 'prod'})
    assert wf1.topology_hash == wf2.topology_hash


# ---------------------------------------------------------------------------
# define_workflow — metadata
# ---------------------------------------------------------------------------

def test_metadata_serialized_as_json():
    wf = define_workflow('wf', 'W', [_node('a')], metadata={'env': 'prod', 'team': 'fx'})
    d = json.loads(wf.metadata_json)
    assert d['env'] == 'prod'
    assert d['team'] == 'fx'


def test_metadata_uses_sort_keys():
    wf = define_workflow('wf', 'W', [_node('a')], metadata={'z': 1, 'a': 2})
    raw = wf.metadata_json
    assert raw.index('"a"') < raw.index('"z"')


def test_none_metadata_produces_empty_object():
    wf = define_workflow('wf', 'W', [_node('a')], metadata=None)
    assert wf.metadata_json == '{}'


# ---------------------------------------------------------------------------
# plan_workflow — full integration
# ---------------------------------------------------------------------------

def test_plan_workflow_linear_four_stage():
    wf = define_workflow('wf', 'W', [
        _node('fetch'),
        _node('parse', dep_ids=['fetch']),
        _node('analyze', dep_ids=['parse']),
        _node('report', dep_ids=['analyze']),
    ])
    vr, plan, lineage = plan_workflow(wf)
    assert vr.valid
    assert plan is not None
    assert len(plan.stages) == 4
    assert plan.stages[0].node_ids == ['fetch']
    assert plan.stages[3].node_ids == ['report']


def test_plan_workflow_invalid_returns_none_plan():
    wf = define_workflow('wf', 'W', [_node('a', dep_ids=['nonexistent'])])
    vr, plan, lineage = plan_workflow(wf)
    assert not vr.valid
    assert plan is None


def test_plan_workflow_always_returns_lineage_on_success():
    wf = define_workflow('wf', 'W', [_node('a')])
    vr, plan, lineage = plan_workflow(wf)
    assert lineage is not None
    assert lineage.workflow_id == 'wf'


def test_plan_workflow_always_returns_lineage_on_failure():
    wf = define_workflow('wf', 'W', [_node('a', dep_ids=['gone'])])
    _, _, lineage = plan_workflow(wf)
    assert lineage is not None


def test_plan_workflow_plan_id_is_hex_sha256():
    wf = define_workflow('wf', 'W', [_node('a')])
    _, plan, _ = plan_workflow(wf)
    assert plan is not None
    assert len(plan.plan_id) == 64
    int(plan.plan_id, 16)


def test_plan_workflow_captures_version_in_lineage():
    wf = define_workflow('wf-99', 'W', [_node('a')], version=4)
    _, _, lineage = plan_workflow(wf)
    assert lineage.workflow_id == 'wf-99'
    assert lineage.version == 4


def test_plan_workflow_lineage_topology_hash_matches_definition():
    wf = define_workflow('wf', 'W', [_node('a'), _node('b', dep_ids=['a'])])
    _, _, lineage = plan_workflow(wf)
    assert lineage.topology_hash == wf.topology_hash


def test_plan_workflow_dependency_snapshot_matches_definition():
    wf = define_workflow('wf', 'W', [_node('a'), _node('b', dep_ids=['a'])])
    _, plan, _ = plan_workflow(wf)
    assert plan is not None
    assert plan.dependency_snapshot['a'] == []
    assert plan.dependency_snapshot['b'] == ['a']


def test_plan_workflow_planner_version_in_plan_and_lineage():
    from workflow.planner import PLANNER_VERSION
    wf = define_workflow('wf', 'W', [_node('a')])
    _, plan, lineage = plan_workflow(wf)
    assert plan.planner_version == PLANNER_VERSION
    assert lineage.planner_version == PLANNER_VERSION
