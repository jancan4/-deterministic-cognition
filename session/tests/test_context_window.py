"""Tests for session/context_window.py."""
import pytest

from session.context_window import BudgetedContext, apply_context_budget
from session.models import (
    ActivatedMemory,
    ActiveWorkflow,
    ContextActivationPolicy,
    RuntimeSnapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy(**kw) -> ContextActivationPolicy:
    return ContextActivationPolicy(**kw)


def _mem(id_, event_type='hypothesis', status='proposed', confidence=3,
         summary_len=50) -> ActivatedMemory:
    summary = 'x' * summary_len
    return ActivatedMemory(
        memory_id=id_, event_type=event_type, title=f'T{id_}',
        summary=summary, evidence=None, confidence=confidence,
        status=status, tags=[], source='test', related_ids=[],
        created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z',
        is_expanded=False, tag_overlap=0,
        activation_rank=(2, 5, -confidence, 0, 0, id_),
    )


def _governance(id_) -> ActivatedMemory:
    return _mem(id_, event_type='governance_rule', status='active', confidence=5)


def _unresolved(id_) -> ActivatedMemory:
    return _mem(id_, event_type='hypothesis', status='unresolved')


def _workflow(id_str) -> ActiveWorkflow:
    return ActiveWorkflow(
        execution_id=id_str, workflow_id='wf', plan_id='p',
        state='executing', active_stage_index=0,
        completed_node_ids=[], failed_node_ids={}, node_attempts={},
        total_lineage_events=3, updated_at='2025-01-01T00:00:00Z',
    )


def _runtime(id_) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        runtime_id=id_, name=f'rt{id_}', state='idle',
        current_iteration=0, updated_at='2025-01-01T00:00:00Z',
        recent_transitions=[],
    )


def _budget(**kw) -> BudgetedContext:
    defaults = dict(
        policy=_policy(),
        governance_context=[],
        unresolved_items=[],
        active_workflows=[],
        active_investigations=[],
        relevant_memory=[],
        execution_lineage=[],
        runtime_snapshots=[],
    )
    defaults.update(kw)
    return apply_context_budget(**defaults)


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------

def test_empty_all_sections():
    result = _budget()
    assert result.included_entries == 0
    assert result.chars_used == 0
    assert not result.truncated
    assert result.total_candidates == 0


# ---------------------------------------------------------------------------
# Basic inclusion
# ---------------------------------------------------------------------------

def test_single_memory_item_included():
    result = _budget(relevant_memory=[_mem(1)])
    assert result.included_entries == 1
    assert len(result.relevant_memory) == 1
    assert not result.truncated


def test_governance_item_included():
    result = _budget(governance_context=[_governance(1)])
    assert len(result.governance_context) == 1
    assert result.included_entries == 1


def test_multiple_sections_all_fit():
    result = _budget(
        governance_context=[_governance(1)],
        unresolved_items=[_unresolved(2)],
        active_workflows=[_workflow('abc')],
        relevant_memory=[_mem(3)],
    )
    assert result.included_entries == 4
    assert not result.truncated


# ---------------------------------------------------------------------------
# Char budget enforcement
# ---------------------------------------------------------------------------

def test_char_budget_zero_excludes_all():
    result = _budget(
        policy=_policy(max_chars=0),
        governance_context=[_governance(1)],
        relevant_memory=[_mem(2)],
    )
    assert result.included_entries == 0
    assert result.truncated


def test_char_budget_tight_drops_lower_tiers():
    gov = _governance(1)
    gov_len = len(gov.render()) + 4  # separator overhead

    result = _budget(
        policy=_policy(max_chars=gov_len + 5),  # fits governance but not memory
        governance_context=[gov],
        relevant_memory=[_mem(2, summary_len=200)],
    )
    assert len(result.governance_context) == 1
    assert len(result.relevant_memory) == 0
    assert result.truncated


def test_governance_preserved_when_memory_truncated():
    policy = _policy(max_chars=200)
    gov_items = [_governance(i) for i in range(3)]
    mem_items = [_mem(i + 10, summary_len=500) for i in range(5)]

    result = apply_context_budget(
        policy=policy,
        governance_context=gov_items,
        unresolved_items=[],
        active_workflows=[],
        active_investigations=[],
        relevant_memory=mem_items,
        execution_lineage=[],
        runtime_snapshots=[],
    )
    # governance items are tier 0; they fill budget before relevant_memory (tier 4)
    assert result.governance_context  # at least some governance included
    # relevant_memory may be empty if budget was exhausted by governance
    assert result.chars_used <= 200 + 4  # minor rounding tolerance


# ---------------------------------------------------------------------------
# Entry count budget
# ---------------------------------------------------------------------------

def test_max_entries_respected():
    policy = _policy(max_entries=2, max_chars=999999)
    items = [_mem(i) for i in range(10)]
    result = _budget(policy=policy, relevant_memory=items)
    assert result.included_entries == 2
    assert len(result.relevant_memory) == 2
    assert result.truncated


def test_max_entries_one():
    policy = _policy(max_entries=1, max_chars=999999)
    result = _budget(
        policy=policy,
        governance_context=[_governance(1)],
        relevant_memory=[_mem(2)],
    )
    assert result.included_entries == 1
    # tier 0 (governance) fills first
    assert len(result.governance_context) == 1
    assert len(result.relevant_memory) == 0


# ---------------------------------------------------------------------------
# Tier ordering: governance > unresolved > workflows > investigations > memory > lineage/runtime
# ---------------------------------------------------------------------------

def test_tier_ordering_governance_before_unresolved():
    policy = _policy(max_entries=1, max_chars=999999)
    result = _budget(
        policy=policy,
        governance_context=[_governance(1)],
        unresolved_items=[_unresolved(2)],
    )
    assert len(result.governance_context) == 1
    assert len(result.unresolved_items) == 0


def test_tier_ordering_unresolved_before_memory():
    policy = _policy(max_entries=1, max_chars=999999)
    result = _budget(
        policy=policy,
        unresolved_items=[_unresolved(1)],
        relevant_memory=[_mem(2)],
    )
    assert len(result.unresolved_items) == 1
    assert len(result.relevant_memory) == 0


def test_tier_ordering_memory_before_runtime():
    policy = _policy(max_entries=1, max_chars=999999)
    result = _budget(
        policy=policy,
        relevant_memory=[_mem(1)],
        runtime_snapshots=[_runtime(99)],
    )
    assert len(result.relevant_memory) == 1
    assert len(result.runtime_snapshots) == 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic_same_inputs():
    items = [_mem(i, summary_len=20) for i in range(10)]
    policy = _policy(max_entries=5, max_chars=500)

    r1 = _budget(policy=policy, relevant_memory=items)
    r2 = _budget(policy=policy, relevant_memory=items)

    assert [m.memory_id for m in r1.relevant_memory] == [m.memory_id for m in r2.relevant_memory]
    assert r1.chars_used == r2.chars_used
    assert r1.included_entries == r2.included_entries


# ---------------------------------------------------------------------------
# Truncation flag
# ---------------------------------------------------------------------------

def test_truncated_flag_false_when_all_fit():
    result = _budget(relevant_memory=[_mem(1)])
    assert not result.truncated


def test_truncated_flag_true_when_budget_exceeded():
    policy = _policy(max_entries=1, max_chars=999999)
    result = _budget(policy=policy, relevant_memory=[_mem(1), _mem(2)])
    assert result.truncated


# ---------------------------------------------------------------------------
# Budget accounting
# ---------------------------------------------------------------------------

def test_chars_used_non_negative():
    result = _budget()
    assert result.chars_used == 0


def test_chars_used_increases_with_items():
    r0 = _budget()
    r1 = _budget(relevant_memory=[_mem(1)])
    assert r1.chars_used > r0.chars_used


def test_total_candidates_counts_all_inputs():
    result = _budget(
        governance_context=[_governance(1)],
        unresolved_items=[_unresolved(2)],
        relevant_memory=[_mem(3), _mem(4)],
        runtime_snapshots=[_runtime(5)],
    )
    assert result.total_candidates == 5


# ---------------------------------------------------------------------------
# Fix 4 regression: max_governance_chars cap
# ---------------------------------------------------------------------------

def test_max_governance_chars_zero_is_uncapped():
    """max_governance_chars=0 must behave identically to no cap: all governance items included."""
    gov_items = [_governance(i) for i in range(5)]
    result = _budget(
        policy=_policy(max_governance_chars=0, max_chars=999999),
        governance_context=gov_items,
    )
    assert len(result.governance_context) == 5


def test_max_governance_chars_limits_governance_tier():
    """When max_governance_chars is set, governance entries stop at the cap."""
    gov1 = _governance(1)
    gov2 = _governance(2)
    gov3 = _governance(3)
    # Set the cap to fit gov1 but not gov1+gov2
    one_entry_chars = len(gov1.render()) + 4
    policy = _policy(max_governance_chars=one_entry_chars, max_chars=999999)

    result = _budget(policy=policy, governance_context=[gov1, gov2, gov3])
    assert len(result.governance_context) == 1
    assert result.governance_context[0].memory_id == 1


def test_max_governance_chars_leaves_budget_for_memory():
    """After capping governance, relevant_memory must fill the remaining budget."""
    gov_items = [_governance(i) for i in range(10)]
    mem_items = [_mem(i + 100) for i in range(5)]

    one_gov_chars = len(gov_items[0].render()) + 4
    policy = _policy(
        max_governance_chars=one_gov_chars,  # cap: only 1 governance entry
        max_chars=999999,
    )
    result = _budget(policy=policy, governance_context=gov_items, relevant_memory=mem_items)

    assert len(result.governance_context) == 1
    assert len(result.relevant_memory) == 5  # all memory fits


def test_max_governance_chars_still_respects_overall_budget():
    """max_governance_chars does not override max_chars: the overall budget still applies."""
    gov1 = _governance(1)
    one_entry_chars = len(gov1.render()) + 4
    # overall budget fits 1 governance entry exactly; governance cap is larger
    policy = _policy(max_chars=one_entry_chars, max_governance_chars=one_entry_chars * 10)

    result = _budget(
        policy=policy,
        governance_context=[gov1, _governance(2)],
        relevant_memory=[_mem(10)],
    )
    # Overall budget exhausted after gov1; gov2 and memory must not fit
    assert len(result.governance_context) == 1
    assert len(result.relevant_memory) == 0


def test_max_governance_chars_default_is_6000():
    """Default max_governance_chars must be GOVERNANCE_CHAR_BUDGET_DEFAULT (6000)."""
    from session.models import GOVERNANCE_CHAR_BUDGET_DEFAULT
    policy = _policy()
    assert policy.max_governance_chars == GOVERNANCE_CHAR_BUDGET_DEFAULT
    assert policy.max_governance_chars == 6000


def test_max_governance_chars_zero_preserves_uncapped_behavior():
    """max_governance_chars=0 must still produce uncapped governance behavior."""
    gov_items = [_governance(i) for i in range(5)]
    result = _budget(
        policy=_policy(max_governance_chars=0, max_chars=999999),
        governance_context=gov_items,
    )
    assert len(result.governance_context) == 5


def test_governance_cap_releases_budget_for_unresolved():
    """Governance cap must leave budget for unresolved items."""
    gov_items = [_governance(i) for i in range(20)]
    unres_items = [_unresolved(i + 100) for i in range(3)]
    one_gov_chars = len(gov_items[0].render()) + 4
    result = _budget(
        policy=_policy(max_governance_chars=one_gov_chars, max_chars=999999),
        governance_context=gov_items,
        unresolved_items=unres_items,
    )
    assert len(result.governance_context) == 1
    assert len(result.unresolved_items) == 3


def test_from_dict_missing_max_governance_chars_uses_new_default():
    """Old policy dict without max_governance_chars must deserialize to GOVERNANCE_CHAR_BUDGET_DEFAULT."""
    from session.models import GOVERNANCE_CHAR_BUDGET_DEFAULT
    old_dict = {'max_chars': 12000, 'max_entries': 60}
    policy = ContextActivationPolicy.from_dict(old_dict)
    assert policy.max_governance_chars == GOVERNANCE_CHAR_BUDGET_DEFAULT
