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


def test_max_governance_chars_default_is_6500():
    """Default max_governance_chars must be GOVERNANCE_CHAR_BUDGET_DEFAULT (6500)."""
    from session.models import GOVERNANCE_CHAR_BUDGET_DEFAULT
    policy = _policy()
    assert policy.max_governance_chars == GOVERNANCE_CHAR_BUDGET_DEFAULT
    assert policy.max_governance_chars == 6500


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


# ---------------------------------------------------------------------------
# Layer 5 regression: ADR-012-sized governance item fits at 6500 cap
#
# Empirical baseline from L1-C8 longitudinal run:
#   6 governance_rules consume ~4893 chars cumulative (including separator overhead).
#   ADR-012 rendered text: ~1334 chars. Cumulative: ~6227.
#   At cap=6000: 6227 > 6000 → ADR-012 excluded.
#   At cap=6500: 6227 ≤ 6500 → ADR-012 included.
#   Next ADR (rendered ~1219 chars): cumulative ~7446 > 6500 → still excluded.
# ---------------------------------------------------------------------------

def _governance_with_summary(id_, summary_len: int) -> ActivatedMemory:
    """Governance item with a specific rendered summary length for regression tests."""
    summary = 'x' * summary_len
    return ActivatedMemory(
        memory_id=id_, event_type='architecture_decision', title=f'ADR-{id_:03d}',
        summary=summary, evidence=None, confidence=4,
        status='active', tags=[], source='test', related_ids=[],
        created_at='2026-01-01T00:00:00Z', updated_at='2026-01-01T00:00:00Z',
        is_expanded=False, tag_overlap=0,
        activation_rank=(0, 2, -4, id_, 0, id_),
    )


def test_layer5_adr012_sized_item_fits_at_6500():
    """Six governance_rules + one ADR-012-class item must fit within the 6500 cap.

    Calibrated from L1-C8 empirical measurements:
      6 rules × summary_len=670 → each 768 chars with sep → cumulative 4608.
      ADR-012-class summary_len=1334 → 1434 chars with sep.
      Total: 4608 + 1434 = 6042. 6000 < 6042 ≤ 6500 → included at 6500.
    """
    rules = [_governance_with_summary(i, 670) for i in range(1, 7)]
    adr012 = _governance_with_summary(100, 1334)

    policy = _policy(max_governance_chars=6500, max_chars=999999)
    result = _budget(policy=policy, governance_context=rules + [adr012])

    included_ids = [m.memory_id for m in result.governance_context]
    assert 100 in included_ids, (
        f"Layer 5: ADR-012-class item must fit at 6500 cap; included: {included_ids}"
    )
    assert len(result.governance_context) == 7


def test_layer5_next_adr_still_excluded_at_6500():
    """After the ADR-012-class item is included, the next ADR must still be excluded.

    After 6 rules (4608 chars) + ADR-012 (1434 chars) = 6042 chars cumulative.
    Next ADR: summary_len=1219 → 1319 chars with sep. Total: 7361 > 6500 → excluded.
    """
    rules = [_governance_with_summary(i, 670) for i in range(1, 7)]
    adr012 = _governance_with_summary(100, 1334)
    adr_next = _governance_with_summary(101, 1219)

    policy = _policy(max_governance_chars=6500, max_chars=999999)
    result = _budget(policy=policy, governance_context=rules + [adr012, adr_next])

    included_ids = [m.memory_id for m in result.governance_context]
    assert 100 in included_ids, "ADR-012-class item must be included"
    assert 101 not in included_ids, (
        f"Layer 5: next ADR must still be excluded at 6500 cap; included: {included_ids}"
    )


def test_layer5_adr012_excluded_at_6000_included_at_6500():
    """Regression: same inputs produce exclusion at cap=6000 and inclusion at cap=6500.

    Uses calibrated summary lengths from L1-C8: rules at 670, ADR-012 at 1334.
    Combined cumulative (6042) sits strictly between 6000 and 6500.
    """
    rules = [_governance_with_summary(i, 670) for i in range(1, 7)]
    adr012 = _governance_with_summary(100, 1334)
    all_gov = rules + [adr012]

    result_old = _budget(
        policy=_policy(max_governance_chars=6000, max_chars=999999),
        governance_context=all_gov,
    )
    result_new = _budget(
        policy=_policy(max_governance_chars=6500, max_chars=999999),
        governance_context=all_gov,
    )

    old_ids = [m.memory_id for m in result_old.governance_context]
    new_ids = [m.memory_id for m in result_new.governance_context]

    assert 100 not in old_ids, "ADR-012-class item must be excluded at cap=6000"
    assert 100 in new_ids, "ADR-012-class item must be included at cap=6500"


# ---------------------------------------------------------------------------
# Layer 4.5: max_investigation_chars cap
# ---------------------------------------------------------------------------

def _investigation(id_) -> ActivatedMemory:
    """Investigation-type item for Layer 4.5 tests."""
    return _mem(id_, event_type='open_question', status='active', confidence=3)


def test_max_investigation_chars_default_is_3500():
    """Default max_investigation_chars must be INVESTIGATION_CHAR_BUDGET_DEFAULT (3500)."""
    from session.models import INVESTIGATION_CHAR_BUDGET_DEFAULT
    policy = _policy()
    assert policy.max_investigation_chars == INVESTIGATION_CHAR_BUDGET_DEFAULT
    assert policy.max_investigation_chars == 3500


def test_max_investigation_chars_zero_is_uncapped():
    """max_investigation_chars=0 must behave identically to no cap: all investigation items included."""
    inv_items = [_investigation(i) for i in range(5)]
    result = _budget(
        policy=_policy(max_investigation_chars=0, max_chars=999999),
        active_investigations=inv_items,
    )
    assert len(result.active_investigations) == 5


def test_max_investigation_chars_limits_investigation_tier():
    """When max_investigation_chars is set, investigation entries stop at the cap."""
    inv1 = _investigation(1)
    inv2 = _investigation(2)
    inv3 = _investigation(3)
    one_entry_chars = len(inv1.render()) + 4
    policy = _policy(max_investigation_chars=one_entry_chars, max_chars=999999)

    result = _budget(policy=policy, active_investigations=[inv1, inv2, inv3])
    assert len(result.active_investigations) == 1
    assert result.active_investigations[0].memory_id == 1


def test_max_investigation_chars_leaves_budget_for_relevant_memory():
    """After capping investigations, relevant_memory must fill the remaining budget."""
    inv_items = [_investigation(i) for i in range(10)]
    mem_items = [_mem(i + 100) for i in range(5)]

    one_inv_chars = len(inv_items[0].render()) + 4
    policy = _policy(
        max_investigation_chars=one_inv_chars,  # cap: only 1 investigation entry
        max_chars=999999,
    )
    result = _budget(policy=policy, active_investigations=inv_items, relevant_memory=mem_items)

    assert len(result.active_investigations) == 1
    assert len(result.relevant_memory) == 5


def test_max_investigation_chars_still_respects_overall_budget():
    """max_investigation_chars does not override max_chars: the overall budget still applies."""
    inv1 = _investigation(1)
    one_entry_chars = len(inv1.render()) + 4
    # overall budget fits 1 investigation entry exactly; investigation cap is larger
    policy = _policy(max_chars=one_entry_chars, max_investigation_chars=one_entry_chars * 10)

    result = _budget(
        policy=policy,
        active_investigations=[inv1, _investigation(2)],
        relevant_memory=[_mem(10)],
    )
    assert len(result.active_investigations) == 1
    assert len(result.relevant_memory) == 0


def test_max_investigation_chars_preserves_unresolved_precedence():
    """Unresolved items (Tier 1) must still precede investigations (Tier 3) when budget is tight."""
    unres = _unresolved(1)
    unres_chars = len(unres.render()) + 4
    inv = _investigation(2)
    inv_chars = len(inv.render()) + 4
    # Budget fits exactly one item; unresolved must win over investigation
    policy = _policy(max_chars=unres_chars, max_investigation_chars=999999)

    result = _budget(
        policy=policy,
        unresolved_items=[unres],
        active_investigations=[inv],
    )
    assert len(result.unresolved_items) == 1
    assert len(result.active_investigations) == 0


def test_from_dict_missing_max_investigation_chars_uses_new_default():
    """Old policy dict without max_investigation_chars must deserialize to INVESTIGATION_CHAR_BUDGET_DEFAULT."""
    from session.models import INVESTIGATION_CHAR_BUDGET_DEFAULT
    old_dict = {'max_chars': 12000, 'max_entries': 60}
    policy = ContextActivationPolicy.from_dict(old_dict)
    assert policy.max_investigation_chars == INVESTIGATION_CHAR_BUDGET_DEFAULT


def test_max_investigation_chars_in_to_dict():
    """max_investigation_chars must appear in the serialized policy dict."""
    policy = _policy(max_investigation_chars=2000)
    d = policy.to_dict()
    assert 'max_investigation_chars' in d
    assert d['max_investigation_chars'] == 2000


def test_relevant_memory_regains_visibility_under_investigation_cap():
    """With investigations capped at one entry, relevant_memory must receive remaining budget."""
    inv_items = [_investigation(i) for i in range(5)]
    mem_items = [_mem(i + 100, summary_len=30) for i in range(3)]

    one_inv_chars = len(inv_items[0].render()) + 4
    policy = _policy(
        max_investigation_chars=one_inv_chars,
        max_chars=999999,
    )
    result = _budget(
        policy=policy,
        active_investigations=inv_items,
        relevant_memory=mem_items,
    )
    assert len(result.active_investigations) == 1
    assert len(result.relevant_memory) == 3


def test_no_duplicate_ids_across_investigation_and_memory_sections():
    """Memory IDs must not appear in both active_investigations and relevant_memory."""
    inv_items = [_investigation(i) for i in range(3)]
    mem_items = [_mem(i + 10) for i in range(3)]

    result = _budget(
        policy=_policy(max_chars=999999, max_investigation_chars=999999),
        active_investigations=inv_items,
        relevant_memory=mem_items,
    )
    inv_ids = {m.memory_id for m in result.active_investigations}
    mem_ids = {m.memory_id for m in result.relevant_memory}
    assert inv_ids & mem_ids == set()


def test_investigation_cap_deterministic_same_inputs():
    """Same inputs with an investigation cap must always produce the same output."""
    inv_items = [_investigation(i) for i in range(10)]
    mem_items = [_mem(i + 100, summary_len=20) for i in range(5)]
    policy = _policy(max_investigation_chars=200, max_chars=999999)

    r1 = _budget(policy=policy, active_investigations=inv_items, relevant_memory=mem_items)
    r2 = _budget(policy=policy, active_investigations=inv_items, relevant_memory=mem_items)

    assert [m.memory_id for m in r1.active_investigations] == [m.memory_id for m in r2.active_investigations]
    assert [m.memory_id for m in r1.relevant_memory] == [m.memory_id for m in r2.relevant_memory]
    assert r1.chars_used == r2.chars_used
