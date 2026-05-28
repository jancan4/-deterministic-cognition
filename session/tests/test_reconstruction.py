"""Tests for session/reconstruction.py."""
import json

import pytest

from memory import service as mem_service
from session.models import (
    ActivatedMemory,
    ContextActivationPolicy,
    SessionContext,
    SessionReconstruction,
)
from session.reconstruction import reconstruct, reconstruct_from_dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db(tmp_path) -> str:
    path = str(tmp_path / 'mem.db')
    mem_service.init_db(path)
    return path


def _add(db, **kw):
    defaults = dict(
        event_type='hypothesis',
        title='Test',
        summary='Test summary',
        source='test',
        confidence=3,
        status='proposed',
        created_by='tester',
    )
    defaults.update(kw)
    return mem_service.add_memory_event(db, **defaults)


def _policy(**kw) -> ContextActivationPolicy:
    return ContextActivationPolicy(**kw)


# ---------------------------------------------------------------------------
# reconstruct — basic structure
# ---------------------------------------------------------------------------

def test_reconstruct_returns_session_reconstruction(tmp_path):
    db = _mem_db(tmp_path)
    result = reconstruct(db)
    assert isinstance(result, SessionReconstruction)
    assert isinstance(result.context, SessionContext)


def test_reconstruct_session_id_non_empty(tmp_path):
    db = _mem_db(tmp_path)
    result = reconstruct(db)
    assert result.context.session_id
    assert len(result.context.session_id) == 32


def test_reconstruct_created_at_utc(tmp_path):
    db = _mem_db(tmp_path)
    result = reconstruct(db)
    assert result.context.created_at.endswith('Z')
    assert 'T' in result.context.created_at


def test_reconstruct_empty_db_no_entries(tmp_path):
    db = _mem_db(tmp_path)
    result = reconstruct(db)
    ctx = result.context
    assert ctx.governance_context == []
    assert ctx.unresolved_items == []
    assert ctx.relevant_memory == []
    assert ctx.active_investigations == []
    assert ctx.included_entries == 0


def test_reconstruct_default_policy(tmp_path):
    db = _mem_db(tmp_path)
    result = reconstruct(db, policy=None)
    assert isinstance(result.context.policy, ContextActivationPolicy)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

def test_reconstruct_deterministic_same_db(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', status='proposed')
    _add(db, event_type='governance_rule', title='G1', status='active')
    _add(db, event_type='hypothesis', title='H2', status='unresolved')

    policy = _policy(include_unresolved=True)
    r1 = reconstruct(db, policy)
    r2 = reconstruct(db, policy)

    # Section membership must be identical
    assert [m.memory_id for m in r1.context.governance_context] == \
           [m.memory_id for m in r2.context.governance_context]
    assert [m.memory_id for m in r1.context.unresolved_items] == \
           [m.memory_id for m in r2.context.unresolved_items]
    assert [m.memory_id for m in r1.context.relevant_memory] == \
           [m.memory_id for m in r2.context.relevant_memory]


# ---------------------------------------------------------------------------
# governance context
# ---------------------------------------------------------------------------

def test_reconstruct_includes_governance(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    result = reconstruct(db, _policy())
    ids = [m.memory_id for m in result.context.governance_context]
    assert len(ids) >= 1
    types = [m.event_type for m in result.context.governance_context]
    assert 'governance_rule' in types


def test_reconstruct_governance_before_relevant_memory(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='implementation_note', title='N1', confidence=5, status='accepted')
    _add(db, event_type='governance_rule', title='G1', confidence=3, status='active')

    result = reconstruct(db, _policy())
    ctx = result.context
    assert ctx.governance_context  # governance present
    # Rendered output should mention governance before relevant memory
    rendered = result.render()
    gov_pos = rendered.find('ACTIVE GOVERNANCE CONTEXT')
    mem_pos = rendered.find('RELEVANT MEMORY')
    if gov_pos != -1 and mem_pos != -1:
        assert gov_pos < mem_pos


# ---------------------------------------------------------------------------
# unresolved items
# ---------------------------------------------------------------------------

def test_reconstruct_includes_unresolved(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='open_question', title='Q1', status='unresolved')
    result = reconstruct(db, _policy(include_unresolved=True))
    statuses = [m.status for m in result.context.unresolved_items]
    assert 'unresolved' in statuses


def test_reconstruct_unresolved_open_question_in_unresolved_not_investigations(tmp_path):
    """Unresolved open_questions must appear in unresolved_items only, not also
    in active_investigations. The two sections are mutually exclusive for
    investigation-type events to prevent budget double-counting."""
    db = _mem_db(tmp_path)
    _add(db, event_type='open_question', title='Q1', status='unresolved')
    result = reconstruct(db, _policy(include_unresolved=True))
    unres_types = [m.event_type for m in result.context.unresolved_items]
    inv_types = [m.event_type for m in result.context.active_investigations]
    assert 'open_question' in unres_types
    assert 'open_question' not in inv_types, (
        "Overlap: unresolved open_question double-counted in active_investigations"
    )


# ---------------------------------------------------------------------------
# active investigations
# ---------------------------------------------------------------------------

def test_reconstruct_hypothesis_in_investigations(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', status='active')
    result = reconstruct(db, _policy())
    types = [m.event_type for m in result.context.active_investigations]
    assert 'hypothesis' in types


# ---------------------------------------------------------------------------
# context window budgeting
# ---------------------------------------------------------------------------

def test_reconstruct_budget_limits_entries(tmp_path):
    db = _mem_db(tmp_path)
    for i in range(20):
        _add(db, event_type='hypothesis', title=f'H{i}', status='proposed', confidence=3)

    policy = _policy(max_entries=5, max_chars=999999)
    result = reconstruct(db, policy)
    assert result.context.included_entries <= 5


def test_reconstruct_budget_limits_chars(tmp_path):
    db = _mem_db(tmp_path)
    for i in range(10):
        _add(db, event_type='hypothesis', title=f'H{i}',
             summary='x' * 300, status='proposed')

    policy = _policy(max_chars=100, max_entries=999)
    result = reconstruct(db, policy)
    assert result.context.chars_used <= 100 + 4  # minor overhead tolerance


def test_reconstruct_truncated_flag(tmp_path):
    db = _mem_db(tmp_path)
    for i in range(20):
        _add(db, event_type='hypothesis', title=f'H{i}', status='proposed')

    policy = _policy(max_entries=2, max_chars=999999)
    result = reconstruct(db, policy)
    assert result.context.truncated


def test_reconstruct_governance_preserved_under_budget(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active', confidence=5)
    for i in range(10):
        _add(db, event_type='hypothesis', title=f'H{i}',
             summary='x' * 500, status='proposed', confidence=1)

    policy = _policy(max_chars=300, max_entries=999)
    result = reconstruct(db, policy)
    gov_ids = [m.memory_id for m in result.context.governance_context]
    assert len(gov_ids) >= 1


# ---------------------------------------------------------------------------
# render output
# ---------------------------------------------------------------------------

def test_reconstruct_render_is_string(tmp_path):
    db = _mem_db(tmp_path)
    result = reconstruct(db)
    rendered = result.render()
    assert isinstance(rendered, str)
    assert len(rendered) > 0


def test_reconstruct_render_includes_session_id(tmp_path):
    db = _mem_db(tmp_path)
    result = reconstruct(db)
    rendered = result.render()
    assert result.context.session_id in rendered


def test_reconstruct_render_includes_governance_section(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='GovernanceRule1', status='active')
    result = reconstruct(db, _policy())
    rendered = result.render()
    assert 'ACTIVE GOVERNANCE CONTEXT' in rendered


def test_reconstruct_render_includes_unresolved_section(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='open_question', title='Q1', status='unresolved')
    result = reconstruct(db, _policy(include_unresolved=True))
    rendered = result.render()
    assert 'UNRESOLVED ITEMS' in rendered


# ---------------------------------------------------------------------------
# to_dict and reconstruct_from_dict (replay)
# ---------------------------------------------------------------------------

def test_context_to_dict_round_trips(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    _add(db, event_type='hypothesis', title='H1', status='unresolved')

    result = reconstruct(db, _policy())
    ctx_dict = result.context.to_dict()

    assert isinstance(ctx_dict, dict)
    assert 'session_id' in ctx_dict
    assert 'governance_context' in ctx_dict
    assert 'unresolved_items' in ctx_dict
    assert 'total_candidates' in ctx_dict
    assert 'truncated' in ctx_dict


def test_reconstruct_from_dict_restores_context(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')

    result = reconstruct(db, _policy())
    ctx_dict = result.context.to_dict()

    restored = reconstruct_from_dict(ctx_dict)
    assert isinstance(restored, SessionContext)
    assert restored.session_id == result.context.session_id
    assert restored.created_at == result.context.created_at
    assert restored.included_entries == result.context.included_entries
    assert len(restored.governance_context) == len(result.context.governance_context)


def test_reconstruct_from_dict_memory_ids_preserved(tmp_path):
    db = _mem_db(tmp_path)
    ev = _add(db, event_type='governance_rule', title='G1', status='active')

    result = reconstruct(db, _policy())
    ctx_dict = result.context.to_dict()
    restored = reconstruct_from_dict(ctx_dict)

    original_ids = {m.memory_id for m in result.context.governance_context}
    restored_ids = {m.memory_id for m in restored.governance_context}
    assert original_ids == restored_ids


def test_reconstruct_from_dict_serializes_to_json(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', status='proposed')

    result = reconstruct(db, _policy())
    ctx_dict = result.context.to_dict()
    # Must be JSON-serializable
    serialized = json.dumps(ctx_dict)
    assert serialized


# ---------------------------------------------------------------------------
# workflow and runtime integration (no actual DBs — just policy flags)
# ---------------------------------------------------------------------------

def test_reconstruct_workflow_db_none_skips_workflows(tmp_path):
    db = _mem_db(tmp_path)
    policy = _policy(workflow_db_path=None, include_active_workflows=True)
    result = reconstruct(db, policy)
    assert result.context.active_workflows == []


def test_reconstruct_runtime_db_none_skips_runtime(tmp_path):
    db = _mem_db(tmp_path)
    policy = _policy(runtime_db_path=None, include_runtime_state=True)
    result = reconstruct(db, policy)
    assert result.context.runtime_snapshots == []


def test_reconstruct_with_workflow_db(tmp_path):
    """Workflow section is populated when a workflow DB is provided with live executions."""
    from workflow.storage import init_db as wf_init_db
    from workflow.executor import initialize_execution, start_execution
    from workflow.persistence import persist_execution
    from workflow.service import define_workflow, plan_workflow
    from workflow.models import WorkflowNode, RetryPolicy

    mem_db = _mem_db(tmp_path)
    wf_db = str(tmp_path / 'wf.db')
    wf_init_db(wf_db)

    wf = define_workflow('wf-session', 'Test', [
        WorkflowNode(node_id='a', task_type='research', dependency_ids=[],
                     retry_policy=RetryPolicy(max_attempts=1)),
    ])
    vr, plan, _ = plan_workflow(wf)
    assert vr.valid

    execution, init_event = initialize_execution(plan)
    persist_execution(wf_db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(wf_db, execution, start_events)

    policy = _policy(
        include_active_workflows=True,
        workflow_db_path=wf_db,
        max_workflows=5,
    )
    result = reconstruct(mem_db, policy)
    assert len(result.context.active_workflows) == 1
    wf_entry = result.context.active_workflows[0]
    assert wf_entry.execution_id == execution.execution_id
    assert wf_entry.state == 'executing'
    assert 'ACTIVE WORKFLOWS' in result.render()
