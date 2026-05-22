"""Tests for workflow DAG validation."""
import pytest

from workflow.models import RetryPolicy, WorkflowNode
from workflow.service import define_workflow
from workflow.validation import validate_workflow


def _node(node_id, task_type='research', dep_ids=None, retry_policy=None,
          payload='{}'):
    return WorkflowNode(
        node_id=node_id,
        task_type=task_type,
        dependency_ids=dep_ids or [],
        retry_policy=retry_policy or RetryPolicy(),
        task_payload_json=payload,
    )


def _wf(*nodes, wf_id='wf'):
    return define_workflow(wf_id, 'Test', list(nodes))


# ---------------------------------------------------------------------------
# Valid workflows
# ---------------------------------------------------------------------------

def test_single_node_is_valid():
    result = validate_workflow(_wf(_node('a')))
    assert result.valid
    assert result.errors == []


def test_linear_chain_is_valid():
    result = validate_workflow(_wf(
        _node('a'),
        _node('b', dep_ids=['a']),
        _node('c', dep_ids=['b']),
    ))
    assert result.valid


def test_diamond_dag_is_valid():
    result = validate_workflow(_wf(
        _node('root'),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['root']),
        _node('sink', dep_ids=['b', 'c']),
    ))
    assert result.valid


def test_fan_in_topology_is_valid():
    result = validate_workflow(_wf(
        _node('a'), _node('b'), _node('c'),
        _node('sink', dep_ids=['a', 'b', 'c']),
    ))
    assert result.valid


# ---------------------------------------------------------------------------
# Duplicate node_ids
# ---------------------------------------------------------------------------

def test_duplicate_node_id_detected():
    wf = _wf(_node('a'), _node('a'))
    result = validate_workflow(wf)
    assert not result.valid
    assert any('Duplicate' in e and "'a'" in e for e in result.errors)


def test_duplicate_node_id_error_mentions_id():
    wf = _wf(_node('x'), _node('x'))
    result = validate_workflow(wf)
    assert any("'x'" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Missing dependency references
# ---------------------------------------------------------------------------

def test_missing_dependency_detected():
    wf = _wf(_node('b', dep_ids=['a']))  # 'a' not in workflow
    result = validate_workflow(wf)
    assert not result.valid
    assert any("unknown node 'a'" in e for e in result.errors)


def test_multiple_missing_dependencies_all_reported():
    wf = _wf(_node('c', dep_ids=['a', 'b']))  # both missing
    result = validate_workflow(wf)
    assert not result.valid
    joined = ' '.join(result.errors)
    assert "'a'" in joined and "'b'" in joined


# ---------------------------------------------------------------------------
# Circular dependencies
# ---------------------------------------------------------------------------

def test_self_loop_detected():
    wf = _wf(_node('a', dep_ids=['a']))
    result = validate_workflow(wf)
    assert not result.valid
    assert any('Circular' in e for e in result.errors)


def test_two_node_cycle_detected():
    wf = _wf(_node('a', dep_ids=['b']), _node('b', dep_ids=['a']))
    result = validate_workflow(wf)
    assert not result.valid
    assert any('Circular' in e for e in result.errors)


def test_three_node_cycle_detected():
    wf = _wf(
        _node('a', dep_ids=['c']),
        _node('b', dep_ids=['a']),
        _node('c', dep_ids=['b']),
    )
    result = validate_workflow(wf)
    assert not result.valid
    assert any('Circular' in e for e in result.errors)


def test_cycle_error_lists_involved_nodes():
    wf = _wf(_node('x', dep_ids=['y']), _node('y', dep_ids=['x']))
    result = validate_workflow(wf)
    cycle_errors = [e for e in result.errors if 'Circular' in e]
    assert len(cycle_errors) == 1
    assert 'x' in cycle_errors[0] and 'y' in cycle_errors[0]


def test_partial_dag_with_separate_cycle_detected():
    """Acyclic nodes plus a separate cycle — cycle is still found."""
    wf = _wf(
        _node('ok_root'),
        _node('ok_child', dep_ids=['ok_root']),
        _node('cycle_a', dep_ids=['cycle_b']),
        _node('cycle_b', dep_ids=['cycle_a']),
        _node('bridge', dep_ids=['ok_child', 'cycle_b']),
    )
    result = validate_workflow(wf)
    assert not result.valid
    assert any('Circular' in e for e in result.errors)


# ---------------------------------------------------------------------------
# Disconnected graph
# ---------------------------------------------------------------------------

def test_two_isolated_nodes_flagged_as_disconnected():
    wf = _wf(_node('a'), _node('b'))
    result = validate_workflow(wf)
    assert not result.valid
    assert any('disconnected' in e.lower() for e in result.errors)


def test_disconnected_error_lists_all_components():
    wf = _wf(_node('a'), _node('b'))
    result = validate_workflow(wf)
    disc_errors = [e for e in result.errors if 'disconnected' in e.lower()]
    assert len(disc_errors) == 1
    assert 'a' in disc_errors[0] and 'b' in disc_errors[0]


def test_three_isolated_nodes_flagged():
    wf = _wf(_node('a'), _node('b'), _node('c'))
    result = validate_workflow(wf)
    assert not result.valid
    assert any('disconnected' in e.lower() for e in result.errors)


def test_connected_two_node_graph_not_flagged():
    wf = _wf(_node('a'), _node('b', dep_ids=['a']))
    result = validate_workflow(wf)
    assert result.valid


def test_fan_in_connects_previously_isolated_nodes():
    """a, b, and c are isolated roots but all connected via sink."""
    wf = _wf(
        _node('a'), _node('b'), _node('c'),
        _node('sink', dep_ids=['a', 'b', 'c']),
    )
    result = validate_workflow(wf)
    assert result.valid


# ---------------------------------------------------------------------------
# Invalid retry policies
# ---------------------------------------------------------------------------

def test_retry_max_attempts_zero_rejected():
    wf = _wf(_node('a', retry_policy=RetryPolicy(max_attempts=0)))
    result = validate_workflow(wf)
    assert not result.valid
    assert any('max_attempts' in e for e in result.errors)


def test_retry_max_attempts_negative_rejected():
    wf = _wf(_node('a', retry_policy=RetryPolicy(max_attempts=-3)))
    result = validate_workflow(wf)
    assert not result.valid


def test_retry_backoff_negative_rejected():
    wf = _wf(_node('a', retry_policy=RetryPolicy(backoff_seconds=-1.0)))
    result = validate_workflow(wf)
    assert not result.valid
    assert any('backoff_seconds' in e for e in result.errors)


def test_valid_retry_policy_accepted():
    wf = _wf(_node('a', retry_policy=RetryPolicy(max_attempts=5, backoff_seconds=2.0)))
    result = validate_workflow(wf)
    assert result.valid


# ---------------------------------------------------------------------------
# Empty / invalid task_type
# ---------------------------------------------------------------------------

def test_empty_task_type_rejected():
    n = WorkflowNode(node_id='a', task_type='')
    result = validate_workflow(define_workflow('wf', 'T', [n]))
    assert not result.valid
    assert any('task_type' in e for e in result.errors)


def test_whitespace_task_type_rejected():
    n = WorkflowNode(node_id='a', task_type='   ')
    result = validate_workflow(define_workflow('wf', 'T', [n]))
    assert not result.valid


# ---------------------------------------------------------------------------
# Invalid task_payload_json
# ---------------------------------------------------------------------------

def test_invalid_payload_json_rejected():
    n = WorkflowNode(node_id='a', task_type='research', task_payload_json='not json{')
    result = validate_workflow(define_workflow('wf', 'T', [n]))
    assert not result.valid
    assert any('task_payload_json' in e for e in result.errors)


def test_valid_json_payload_accepted():
    n = WorkflowNode(node_id='a', task_type='research', task_payload_json='{"key": 1}')
    result = validate_workflow(define_workflow('wf', 'T', [n]))
    assert result.valid


def test_empty_object_payload_accepted():
    n = WorkflowNode(node_id='a', task_type='research', task_payload_json='{}')
    result = validate_workflow(define_workflow('wf', 'T', [n]))
    assert result.valid


# ---------------------------------------------------------------------------
# Multiple errors collected in a single pass
# ---------------------------------------------------------------------------

def test_all_errors_collected_not_short_circuited():
    """Validator must accumulate all errors, not stop at first failure."""
    n1 = WorkflowNode(node_id='a', task_type='')  # invalid task_type
    n2 = WorkflowNode(node_id='b', task_type='t',
                      retry_policy=RetryPolicy(max_attempts=0))  # invalid retry
    wf = define_workflow('wf', 'T', [n1, n2])
    result = validate_workflow(wf)
    assert not result.valid
    assert len(result.errors) >= 2


def test_validation_result_errors_are_sorted():
    """Errors must be returned sorted for deterministic test assertions."""
    n1 = WorkflowNode(node_id='a', task_type='')
    n2 = WorkflowNode(node_id='b', task_type='t',
                      retry_policy=RetryPolicy(max_attempts=-1))
    wf = define_workflow('wf', 'T', [n1, n2])
    result = validate_workflow(wf)
    assert result.errors == sorted(result.errors)


# ---------------------------------------------------------------------------
# ValidationResult.to_dict
# ---------------------------------------------------------------------------

def test_validation_result_to_dict_valid():
    wf = _wf(_node('a'))
    result = validate_workflow(wf)
    d = result.to_dict()
    assert d['valid'] is True
    assert d['errors'] == []


def test_validation_result_to_dict_invalid():
    wf = _wf(_node('b', dep_ids=['missing']))
    result = validate_workflow(wf)
    d = result.to_dict()
    assert d['valid'] is False
    assert len(d['errors']) > 0
