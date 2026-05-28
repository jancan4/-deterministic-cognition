"""
Deterministic activation of memory events for session context assembly.

Activation scoring transforms a list of ScoredEvents from the memory
retrieval layer into ActivatedMemory items ranked by a composite key that
respects doctrine priority, confidence, recency, unresolved status, and
tag overlap.

No embeddings. No semantic search. No hidden heuristics. All scoring
components are explicit and inspectable.
"""
from typing import Dict, List, Optional, Set, Tuple

from memory.retrieval import DOCTRINE_PRIORITY, ScoredEvent, RetrievalQuery, retrieve, retrieve_governance, retrieve_unresolved
from memory.models import MemoryEvent, VALID_EVENT_TYPES
from .models import ActivatedMemory, ContextActivationPolicy

_DEFAULT_DOCTRINE_RANK = 7

# Event types classified as "active investigations"
INVESTIGATION_EVENT_TYPES = frozenset({'open_question', 'hypothesis'})

# Governance event types — always surfaced; not filterable by activation policy
GOVERNANCE_EVENT_TYPES = frozenset({'governance_rule', 'architecture_decision'})

# Statuses that indicate an unresolved item
UNRESOLVED_STATUSES = frozenset({'unresolved', 'proposed'})

# Terminal-negative statuses excluded from the governance_context partition.
# Events with these statuses have been explicitly rejected or superseded and
# must not consume governance tier budget or displace active institutional
# knowledge. (EI-006 fix)
GOVERNANCE_EXCLUDE_STATUSES = frozenset({'rejected', 'superseded', 'archived', 'deprecated'})


def _doctrine_rank(event_type: str) -> int:
    return DOCTRINE_PRIORITY.get(event_type, _DEFAULT_DOCTRINE_RANK)


def _is_unresolved(event: MemoryEvent) -> bool:
    return event.status in UNRESOLVED_STATUSES


def _activation_rank(
    scored: ScoredEvent,
    pin_governance: bool = False,
    pin_unresolved: bool = False,
) -> Tuple:
    """
    Composite sort key for activation priority. Lower = higher priority.

    Tier 0: pinned governance items (when pin_governance=True)
    Tier 1: pinned unresolved items (when pin_unresolved=True)
    Tier 2: primary events sorted by (doctrine_rank, -confidence, recency, -tag_overlap, id)
    Tier 3: related-expanded events (same sub-sort)

    Pinning ensures governance and unresolved items survive context truncation.
    """
    ev = scored.event
    tier: int
    if pin_governance and ev.event_type in GOVERNANCE_EVENT_TYPES:
        tier = 0
    elif pin_unresolved and _is_unresolved(ev):
        tier = 1
    elif not scored.is_expanded:
        tier = 2
    else:
        tier = 3

    return (
        tier,
        _doctrine_rank(ev.event_type),
        -scored.effective_confidence,
        scored.recency_rank,
        -scored.tag_overlap,
        ev.id,
    )


def score_and_rank(
    scored_events: List[ScoredEvent],
    pin_governance: bool = True,
    pin_unresolved: bool = True,
) -> List[ActivatedMemory]:
    """
    Convert a list of ScoredEvents into ranked ActivatedMemory items.

    Pure function — no I/O. Sorting is deterministic: same input always
    produces the same output.
    """
    result: List[ActivatedMemory] = []
    for scored in scored_events:
        ev = scored.event
        rank = _activation_rank(scored, pin_governance, pin_unresolved)
        result.append(ActivatedMemory(
            memory_id=ev.id,
            event_type=ev.event_type,
            title=ev.title,
            summary=ev.summary,
            evidence=ev.evidence,
            confidence=scored.effective_confidence,
            status=ev.status,
            tags=list(ev.tags),
            source=ev.source,
            related_ids=list(ev.related_ids),
            created_at=ev.created_at,
            updated_at=ev.updated_at,
            is_expanded=scored.is_expanded,
            tag_overlap=scored.tag_overlap,
            activation_rank=rank,
        ))
    result.sort(key=lambda m: m.activation_rank)
    return result


def activate_memory(
    memory_db_path: str,
    policy: ContextActivationPolicy,
) -> List[ActivatedMemory]:
    """
    Retrieve and rank memory events according to the activation policy.

    Combines:
    - Governance context (always — governance events are not filterable)
    - Unresolved items (if include_unresolved=True)
    - Tag/type filtered relevant memory
    - Adaptation events (if include_adaptations=True)

    Deduplicates by memory_id. Respects max_memory_candidates. Returns
    items sorted by activation_rank (lower = higher priority).
    """
    seen_ids: Set[int] = set()
    collected: List[ScoredEvent] = []

    def _add(events: List[ScoredEvent]) -> None:
        for e in events:
            if e.event.id not in seen_ids:
                seen_ids.add(e.event.id)
                collected.append(e)

    _add(retrieve_governance(memory_db_path, limit=policy.max_memory_candidates))

    if policy.include_unresolved:
        _add(retrieve_unresolved(memory_db_path, limit=policy.max_memory_candidates))

    # Adaptation-specific retrieve (extra pass if include_adaptations=True)
    if policy.include_adaptations:
        adaptation_query = RetrievalQuery(
            event_types=['adaptation'],
            tags=list(policy.tags),
            min_confidence=policy.min_confidence,
            limit=policy.max_memory_candidates,
            expand_related=False,
        )
        _add(retrieve(memory_db_path, adaptation_query))

    # General retrieve: always run to catch all events not covered by the
    # specific paths above (hypothesis, implementation_note, experiment, etc.)
    general_query = RetrievalQuery(
        tags=list(policy.tags),
        min_confidence=policy.min_confidence,
        limit=policy.max_memory_candidates,
        expand_related=policy.expand_related,
    )
    _add(retrieve(memory_db_path, general_query))

    return score_and_rank(
        collected,
        pin_governance=True,
        pin_unresolved=policy.include_unresolved,
    )


def partition_by_section(
    activated: List[ActivatedMemory],
) -> Dict[str, List[ActivatedMemory]]:
    """
    Partition activated memory into named session sections.

    Sections:
    - 'governance_context': governance_rule, architecture_decision
    - 'unresolved_items': status in UNRESOLVED_STATUSES
    - 'active_investigations': open_question, hypothesis
    - 'relevant_memory': everything else

    Items may appear in multiple categories (e.g. an unresolved governance
    rule appears in both governance_context and unresolved_items). The
    context_window layer deduplicates by section if needed.
    """
    sections: Dict[str, List[ActivatedMemory]] = {
        'governance_context': [],
        'unresolved_items': [],
        'active_investigations': [],
        'relevant_memory': [],
    }
    for mem in activated:
        placed = False
        if mem.event_type in GOVERNANCE_EVENT_TYPES and mem.status not in GOVERNANCE_EXCLUDE_STATUSES:
            sections['governance_context'].append(mem)
            placed = True
        if _is_unresolved_mem(mem):
            sections['unresolved_items'].append(mem)
            placed = True
        if mem.event_type in INVESTIGATION_EVENT_TYPES:
            sections['active_investigations'].append(mem)
            placed = True
        if not placed:
            sections['relevant_memory'].append(mem)
    return sections


def _is_unresolved_mem(mem: ActivatedMemory) -> bool:
    return mem.status in UNRESOLVED_STATUSES
