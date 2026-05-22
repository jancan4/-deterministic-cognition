"""Tests for deterministic execution planner."""
import pytest

from workflow.models import RetryPolicy, WorkflowNode
from workflow.planner import (
    PLANNER_VERSION,
    build_execution_plan,
    build_workflow_lineage,
    get_blocked_nodes,
    get_ready_nodes,
)
from workflow.service import define_workflow, plan_workflow
from workflow.validation import validate_workflow


def _node(node_id, task_type='research', dep_ids=None, priority=0):
    return WorkflowNode(
        node_id=node_id,
        task_type=task_type,
        dependency_ids=dep_ids or [],
        priority=priority,
    )


def _wf(*nodes, wf_id='wf'):
    return define_workflow(wf_id, 'Test', list(nodes))


def _plan(wf):
    vr = validate_workflow(wf)
    return build_execution_plan(wf, vr)


# ---------------------------------------------------------------------------
# Stage structure
# ---------------------------------------------------------------------------

def test_single_node_plan_has_one_stage():
    plan = _plan(_wf(_node('a')))
    assert len(plan.stages) == 1
    assert plan.stages[0].stage_index == 0
    assert plan.stages[0].node_ids == ['a']


def test_linear_chain_produces_sequential_stages():
    plan = _plan(_wf(
        _node('a'),
        _node('b', dep_ids=['a']),
        _node('c', dep_ids=['b']),
    ))
    assert len(plan.stages) == 3
    assert plan.stages[0].node_ids == ['a']
    assert plan.stages[1].node_ids == ['b']
    assert plan.stages[2].node_ids == ['c']


def test_diamond_dag_three_stages():
    plan = _plan(_wf(
        _node('root'),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['root']),
        _node('sink', dep_ids=['b', 'c']),
    ))
    assert len(plan.stages) == 3
    assert plan.stages[0].node_ids == ['root']
    assert sorted(plan.stages[1].node_ids) == ['b', 'c']
    assert plan.stages[2].node_ids == ['sink']


def test_fan_in_two_stages():
    plan = _plan(_wf(
        _node('a'), _node('b'), _node('c'),
        _node('sink', dep_ids=['a', 'b', 'c']),
    ))
    assert len(plan.stages) == 2
    assert sorted(plan.stages[0].node_ids) == ['a', 'b', 'c']
    assert plan.stages[1].node_ids == ['sink']


def test_stage_index_is_zero_based_and_sequential():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a']), _node('c', dep_ids=['b'])))
    for i, stage in enumerate(plan.stages):
        assert stage.stage_index == i


# ---------------------------------------------------------------------------
# Within-stage ordering: priority then node_id
# ---------------------------------------------------------------------------

def test_within_stage_lower_priority_runs_first():
    """Lower priority integer = higher execution priority within the stage."""
    plan = _plan(_wf(
        _node('root'),
        _node('hi', dep_ids=['root'], priority=1),
        _node('lo', dep_ids=['root'], priority=5),
    ))
    stage = plan.stages[1]
    assert stage.node_ids[0] == 'hi'
    assert stage.node_ids[1] == 'lo'


def test_within_stage_node_id_tiebreaker():
    """Equal priority → lexicographic node_id ordering."""
    plan = _plan(_wf(
        _node('root'),
        _node('z_node', dep_ids=['root'], priority=0),
        _node('a_node', dep_ids=['root'], priority=0),
    ))
    assert plan.stages[1].node_ids == ['a_node', 'z_node']


def test_within_stage_mixed_priority_and_node_id():
    plan = _plan(_wf(
        _node('root'),
        _node('z_hi', dep_ids=['root'], priority=1),
        _node('a_lo', dep_ids=['root'], priority=2),
        _node('a_hi', dep_ids=['root'], priority=1),
    ))
    # priority=1: a_hi, z_hi (a < z); priority=2: a_lo
    assert plan.stages[1].node_ids == ['a_hi', 'z_hi', 'a_lo']


# ---------------------------------------------------------------------------
# Determinism across repeated calls
# ---------------------------------------------------------------------------

def test_plan_stage_structure_identical_on_repeated_calls():
    wf = _wf(_node('root'), _node('b', dep_ids=['root']), _node('c', dep_ids=['root']))
    plan1, plan2 = _plan(wf), _plan(wf)
    assert len(plan1.stages) == len(plan2.stages)
    for s1, s2 in zip(plan1.stages, plan2.stages):
        assert s1.stage_index == s2.stage_index
        assert s1.node_ids == s2.node_ids


def test_plan_id_is_deterministic_for_same_workflow():
    """Same workflow_id + version + planner_version → same plan_id."""
    wf = _wf(_node('a'))
    plan1, plan2 = _plan(wf), _plan(wf)
    assert plan1.plan_id == plan2.plan_id


def test_plan_id_changes_with_different_workflow_id():
    wf1 = define_workflow('wf-1', 'W', [_node('a')])
    wf2 = define_workflow('wf-2', 'W', [_node('a')])
    plan1, plan2 = _plan(wf1), _plan(wf2)
    assert plan1.plan_id != plan2.plan_id


def test_plan_id_is_64_char_hex():
    plan = _plan(_wf(_node('a')))
    assert len(plan.plan_id) == 64
    int(plan.plan_id, 16)  # valid hex


# ---------------------------------------------------------------------------
# Dependency snapshot
# ---------------------------------------------------------------------------

def test_dependency_snapshot_captures_all_nodes():
    wf = _wf(_node('a'), _node('b', dep_ids=['a']))
    plan = _plan(wf)
    assert set(plan.dependency_snapshot.keys()) == {'a', 'b'}


def test_dependency_snapshot_root_has_empty_list():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a'])))
    assert plan.dependency_snapshot['a'] == []


def test_dependency_snapshot_dep_list_is_sorted():
    plan = _plan(_wf(
        _node('root'),
        _node('a', dep_ids=['root']),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['b', 'a']),
    ))
    assert plan.dependency_snapshot['c'] == ['a', 'b']


# ---------------------------------------------------------------------------
# build_execution_plan rejects invalid workflows
# ---------------------------------------------------------------------------

def test_build_plan_raises_for_invalid_workflow():
    wf = _wf(_node('a', dep_ids=['missing']))
    vr = validate_workflow(wf)
    assert not vr.valid
    with pytest.raises(ValueError, match='invalid workflow'):
        build_execution_plan(wf, vr)


def test_build_plan_raises_error_mentions_workflow_id():
    wf = define_workflow('bad-wf', 'W', [_node('a', dep_ids=['x'])])
    vr = validate_workflow(wf)
    with pytest.raises(ValueError, match='bad-wf'):
        build_execution_plan(wf, vr)


# ---------------------------------------------------------------------------
# get_ready_nodes
# ---------------------------------------------------------------------------

def test_ready_nodes_at_start_are_roots():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a'])))
    assert get_ready_nodes(plan, set()) == ['a']


def test_ready_nodes_after_root_completes():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a'])))
    assert get_ready_nodes(plan, {'a'}) == ['b']


def test_ready_nodes_empty_when_all_complete():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a'])))
    assert get_ready_nodes(plan, {'a', 'b'}) == []


def test_ready_nodes_diamond_partial():
    plan = _plan(_wf(
        _node('root'),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['root']),
        _node('sink', dep_ids=['b', 'c']),
    ))
    # root and b done; c is ready, sink is blocked
    ready = get_ready_nodes(plan, {'root', 'b'})
    assert 'c' in ready
    assert 'sink' not in ready


def test_ready_nodes_sink_unblocked_when_all_predecessors_complete():
    plan = _plan(_wf(
        _node('root'),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['root']),
        _node('sink', dep_ids=['b', 'c']),
    ))
    ready = get_ready_nodes(plan, {'root', 'b', 'c'})
    assert ready == ['sink']


def test_ready_nodes_order_respects_stage_order():
    """Ready nodes must appear in the same relative order as in the plan stages."""
    plan = _plan(_wf(
        _node('root'),
        _node('a_node', dep_ids=['root'], priority=0),
        _node('z_node', dep_ids=['root'], priority=0),
    ))
    ready = get_ready_nodes(plan, {'root'})
    assert ready == ['a_node', 'z_node']  # lexicographic within stage


# ---------------------------------------------------------------------------
# get_blocked_nodes
# ---------------------------------------------------------------------------

def test_blocked_nodes_at_start():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a'])))
    assert get_blocked_nodes(plan, set()) == ['b']


def test_blocked_nodes_none_when_root_done():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a'])))
    assert get_blocked_nodes(plan, {'a'}) == []


def test_blocked_nodes_sink_blocked_until_both_preds_done():
    plan = _plan(_wf(
        _node('root'),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['root']),
        _node('sink', dep_ids=['b', 'c']),
    ))
    blocked = get_blocked_nodes(plan, {'root', 'b'})
    assert 'sink' in blocked
    assert 'c' not in blocked  # c is now ready, not blocked


def test_blocked_nodes_empty_when_all_complete():
    plan = _plan(_wf(_node('a'), _node('b', dep_ids=['a'])))
    assert get_blocked_nodes(plan, {'a', 'b'}) == []


# ---------------------------------------------------------------------------
# build_workflow_lineage
# ---------------------------------------------------------------------------

def test_lineage_planner_version():
    wf = _wf(_node('a'))
    vr = validate_workflow(wf)
    lineage = build_workflow_lineage(wf, vr)
    assert lineage.planner_version == PLANNER_VERSION


def test_lineage_valid_result_string():
    wf = _wf(_node('a'))
    vr = validate_workflow(wf)
    lineage = build_workflow_lineage(wf, vr)
    assert lineage.validation_result == 'valid'


def test_lineage_invalid_captures_errors():
    wf = _wf(_node('a', dep_ids=['missing']))
    vr = validate_workflow(wf)
    lineage = build_workflow_lineage(wf, vr)
    assert lineage.validation_result != 'valid'
    assert 'missing' in lineage.validation_result.lower() or \
           'unknown' in lineage.validation_result.lower()


def test_lineage_topology_hash_matches_definition():
    wf = _wf(_node('a'), _node('b', dep_ids=['a']))
    vr = validate_workflow(wf)
    lineage = build_workflow_lineage(wf, vr)
    assert lineage.topology_hash == wf.topology_hash


def test_lineage_produced_even_on_invalid_workflow():
    """Lineage must be written regardless of validation outcome."""
    wf = _wf(_node('a', dep_ids=['missing']))
    vr = validate_workflow(wf)
    lineage = build_workflow_lineage(wf, vr)
    assert lineage is not None
    assert lineage.workflow_id == 'wf'


# ---------------------------------------------------------------------------
# plan_workflow service integration
# ---------------------------------------------------------------------------

def test_plan_workflow_valid_returns_plan():
    wf = _wf(_node('a'), _node('b', dep_ids=['a']))
    vr, plan, lineage = plan_workflow(wf)
    assert vr.valid
    assert plan is not None
    assert len(plan.stages) == 2


def test_plan_workflow_invalid_returns_none_plan():
    wf = _wf(_node('a', dep_ids=['missing']))
    vr, plan, lineage = plan_workflow(wf)
    assert not vr.valid
    assert plan is None


def test_plan_workflow_always_returns_lineage():
    for wf in [
        _wf(_node('a')),
        _wf(_node('a', dep_ids=['gone'])),
    ]:
        _, _, lineage = plan_workflow(wf)
        assert lineage is not None


def test_plan_workflow_lineage_workflow_id():
    wf = define_workflow('my-wf', 'W', [_node('a')])
    _, _, lineage = plan_workflow(wf)
    assert lineage.workflow_id == 'my-wf'
