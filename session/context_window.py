"""
Deterministic context window budgeting for session reconstruction.

A context window is a char-budget and entry-count constrained view over
the full set of activated items. Truncation is deterministic and
inspectable: the same activated list always produces the same window.

Preservation order (items from each tier fill the window before the next):
  Tier 0: governance context (always preserved first)
  Tier 1: unresolved items  (preserved second)
  Tier 2: active workflows  (preserved third)
  Tier 3: active investigations (preserved fourth)
  Tier 4: relevant memory   (fills remaining budget)
  Tier 5: execution lineage / runtime snapshots (lowest priority)

Within each tier, items are accepted in their pre-sorted activation_rank
order. Items that exceed the budget are skipped (not dropped from the
original list — the raw context always preserves all candidates).
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .models import (
    ActivatedMemory,
    ActiveWorkflow,
    ContextActivationPolicy,
    RuntimeSnapshot,
)

_SEPARATOR_OVERHEAD = 4  # newlines between items


@dataclass
class BudgetedContext:
    """Result of applying the context window to all activated items."""
    governance_context: List[ActivatedMemory]
    unresolved_items: List[ActivatedMemory]
    active_workflows: List[ActiveWorkflow]
    active_investigations: List[ActivatedMemory]
    relevant_memory: List[ActivatedMemory]
    execution_lineage: List[ActiveWorkflow]
    runtime_snapshots: List[RuntimeSnapshot]

    char_budget: int
    chars_used: int
    total_candidates: int
    included_entries: int
    truncated: bool

    def all_included_ids(self) -> List[int]:
        """Return all included memory_ids for deduplication inspection."""
        ids = []
        for m in self.governance_context:
            ids.append(m.memory_id)
        for m in self.unresolved_items:
            ids.append(m.memory_id)
        for m in self.active_investigations:
            ids.append(m.memory_id)
        for m in self.relevant_memory:
            ids.append(m.memory_id)
        return ids


def _char_count(text: str) -> int:
    return len(text) + _SEPARATOR_OVERHEAD


def apply_context_budget(
    policy: ContextActivationPolicy,
    governance_context: List[ActivatedMemory],
    unresolved_items: List[ActivatedMemory],
    active_workflows: List[ActiveWorkflow],
    active_investigations: List[ActivatedMemory],
    relevant_memory: List[ActivatedMemory],
    execution_lineage: List[ActiveWorkflow],
    runtime_snapshots: List[RuntimeSnapshot],
) -> BudgetedContext:
    """
    Apply the char and entry budget to the full set of activated items.

    Truncation is deterministic: same inputs → same output. Items are never
    reordered — only dropped from the tail of each tier when the budget is
    exhausted.

    Returns a BudgetedContext with the subset that fits, plus accounting
    fields describing what was included and whether truncation occurred.
    """
    max_chars = policy.max_chars
    max_entries = policy.max_entries
    max_governance_chars = policy.max_governance_chars      # 0 = uncapped (legacy)
    max_investigation_chars = policy.max_investigation_chars  # 0 = uncapped
    max_governance_rule_chars       = policy.max_governance_rule_chars
    max_architecture_decision_chars = policy.max_architecture_decision_chars

    chars_used = 0
    entries_used = 0
    governance_chars_used = 0
    investigation_chars_used = 0
    governance_rule_chars_used       = 0
    architecture_decision_chars_used = 0

    # Contract A — Tier-0 budget mode:
    #   Sub-budget mode (both fields > 0): governance_rule and architecture_decision
    #     are each governed by their own sub-budget independently.
    #     max_governance_chars is NOT applied to Tier-0 in this mode.
    #   Legacy mode (either field is 0): max_governance_chars is the single
    #     Tier-0 total envelope for all governance items.
    _sub_budget_mode = (
        max_governance_rule_chars > 0 and max_architecture_decision_chars > 0
    )

    total_candidates = (
        len(governance_context)
        + len(unresolved_items)
        + len(active_workflows)
        + len(active_investigations)
        + len(relevant_memory)
        + len(execution_lineage)
        + len(runtime_snapshots)
    )

    def _fits(text: str) -> bool:
        return (
            chars_used + _char_count(text) <= max_chars
            and entries_used < max_entries
        )

    def _accept(text: str) -> None:
        nonlocal chars_used, entries_used
        chars_used += _char_count(text)
        entries_used += 1

    def _governance_fits(text: str, event_type: str) -> bool:
        if not _fits(text):
            return False
        n = _char_count(text)
        if _sub_budget_mode:
            # Sub-budget mode: each type checked against its own cap only.
            # max_governance_chars is not applied in this mode.
            if event_type == 'governance_rule':
                return governance_rule_chars_used + n <= max_governance_rule_chars
            return architecture_decision_chars_used + n <= max_architecture_decision_chars
        # Legacy mode: max_governance_chars is the single Tier-0 total envelope.
        if max_governance_chars > 0:
            return governance_chars_used + n <= max_governance_chars
        return True

    def _accept_governance(text: str, event_type: str) -> None:
        nonlocal governance_chars_used, governance_rule_chars_used, architecture_decision_chars_used
        n = _char_count(text)
        governance_chars_used += n
        if _sub_budget_mode:
            if event_type == 'governance_rule':
                governance_rule_chars_used += n
            else:
                architecture_decision_chars_used += n
        _accept(text)

    def _investigation_fits(text: str) -> bool:
        if not _fits(text):
            return False
        if max_investigation_chars > 0:
            return investigation_chars_used + _char_count(text) <= max_investigation_chars
        return True

    def _accept_investigation(text: str) -> None:
        nonlocal investigation_chars_used
        investigation_chars_used += _char_count(text)
        _accept(text)

    out_gov: List[ActivatedMemory] = []
    out_unres: List[ActivatedMemory] = []
    out_wf: List[ActiveWorkflow] = []
    out_inv: List[ActivatedMemory] = []
    out_mem: List[ActivatedMemory] = []
    out_lin: List[ActiveWorkflow] = []
    out_rt: List[RuntimeSnapshot] = []

    # Tier 0: governance — evaluated in activation_rank order (rules first, then ADRs).
    # Budget mode is determined by max_governance_rule_chars and max_architecture_decision_chars.
    # See Contract A in apply_context_budget docstring and ContextActivationPolicy fields.
    for item in governance_context:
        rendered = item.render()
        if _governance_fits(rendered, item.event_type):
            out_gov.append(item)
            _accept_governance(rendered, item.event_type)

    # Tier 1: unresolved — preserved second
    for item in unresolved_items:
        rendered = item.render()
        if _fits(rendered):
            out_unres.append(item)
            _accept(rendered)

    # Tier 2: active workflows
    for item in active_workflows:
        rendered = item.render()
        if _fits(rendered):
            out_wf.append(item)
            _accept(rendered)

    # Tier 3: active investigations (optionally capped by max_investigation_chars)
    for item in active_investigations:
        rendered = item.render()
        if _investigation_fits(rendered):
            out_inv.append(item)
            _accept_investigation(rendered)

    # Tier 4: relevant memory (fills remaining budget)
    for item in relevant_memory:
        rendered = item.render()
        if _fits(rendered):
            out_mem.append(item)
            _accept(rendered)

    # Tier 5: execution lineage + runtime (lowest priority)
    for item in execution_lineage:
        rendered = item.render()
        if _fits(rendered):
            out_lin.append(item)
            _accept(rendered)

    for item in runtime_snapshots:
        rendered = item.render()
        if _fits(rendered):
            out_rt.append(item)
            _accept(rendered)

    included_entries = (
        len(out_gov) + len(out_unres) + len(out_wf)
        + len(out_inv) + len(out_mem) + len(out_lin) + len(out_rt)
    )
    truncated = included_entries < total_candidates

    return BudgetedContext(
        governance_context=out_gov,
        unresolved_items=out_unres,
        active_workflows=out_wf,
        active_investigations=out_inv,
        relevant_memory=out_mem,
        execution_lineage=out_lin,
        runtime_snapshots=out_rt,
        char_budget=max_chars,
        chars_used=chars_used,
        total_candidates=total_candidates,
        included_entries=included_entries,
        truncated=truncated,
    )
