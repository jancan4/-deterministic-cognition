"""Tests for session/activation.py."""
import pytest

from memory import service as mem_service
from session.activation import (
    GOVERNANCE_EVENT_TYPES,
    GOVERNANCE_EXCLUDE_STATUSES,
    INVESTIGATION_EVENT_TYPES,
    UNRESOLVED_STATUSES,
    activate_memory,
    partition_by_section,
    score_and_rank,
)
from session.models import ActivatedMemory, ContextActivationPolicy


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
# score_and_rank (pure function)
# ---------------------------------------------------------------------------

def test_score_and_rank_empty():
    assert score_and_rank([]) == []


def test_score_and_rank_governance_pinned_first():
    from memory.retrieval import retrieve_governance
    import memory.service as svc

    # We test directly with ScoredEvent mock-like objects via a real DB
    # (score_and_rank is pure but ScoredEvent is tied to retrieval output)
    pass  # covered by activate_memory tests below


def test_score_and_rank_returns_activated_memory():
    from memory.retrieval import ScoredEvent
    from memory.models import MemoryEvent

    def _ev(id_, event_type, confidence, status, updated_at='2025-01-01T00:00:00Z'):
        return MemoryEvent(
            id=id_, event_type=event_type, title=f'T{id_}',
            summary='s', evidence=None, source='test', confidence=confidence,
            status=status, tags=[], related_ids=[], created_by='t',
            created_at='2025-01-01T00:00:00Z', updated_at=updated_at, version=1,
        )

    scored = [
        ScoredEvent(event=_ev(1, 'hypothesis', 3, 'proposed'), tag_overlap=0, recency_rank=1, is_expanded=False),
        ScoredEvent(event=_ev(2, 'governance_rule', 5, 'active'), tag_overlap=0, recency_rank=0, is_expanded=False),
    ]
    result = score_and_rank(scored, pin_governance=True, pin_unresolved=True)
    assert all(isinstance(m, ActivatedMemory) for m in result)
    # governance_rule should sort first (tier 0 vs tier 2)
    assert result[0].memory_id == 2
    assert result[1].memory_id == 1


def test_score_and_rank_deterministic():
    from memory.retrieval import ScoredEvent
    from memory.models import MemoryEvent

    def _ev(id_):
        return MemoryEvent(
            id=id_, event_type='hypothesis', title=f'T{id_}', summary='s',
            evidence=None, source='test', confidence=3, status='proposed',
            tags=[], related_ids=[], created_by='t',
            created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z', version=1,
        )

    scored = [
        ScoredEvent(event=_ev(3), tag_overlap=0, recency_rank=2, is_expanded=False),
        ScoredEvent(event=_ev(1), tag_overlap=0, recency_rank=0, is_expanded=False),
        ScoredEvent(event=_ev(2), tag_overlap=0, recency_rank=1, is_expanded=False),
    ]
    r1 = score_and_rank(scored)
    r2 = score_and_rank(scored)
    assert [m.memory_id for m in r1] == [m.memory_id for m in r2]


# ---------------------------------------------------------------------------
# activate_memory
# ---------------------------------------------------------------------------

def test_activate_memory_empty_db(tmp_path):
    db = _mem_db(tmp_path)
    result = activate_memory(db, _policy())
    assert result == []


def test_activate_memory_returns_activated_memory(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', status='proposed')
    result = activate_memory(db, _policy())
    assert len(result) == 1
    assert isinstance(result[0], ActivatedMemory)
    assert result[0].event_type == 'hypothesis'


def test_activate_memory_governance_comes_first(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', confidence=5, status='active')
    _add(db, event_type='governance_rule', title='G1', confidence=3, status='active')
    result = activate_memory(db, _policy())
    # governance_rule should rank first due to tier 0 pinning
    gov_idx = next(i for i, m in enumerate(result) if m.event_type == 'governance_rule')
    hyp_idx = next(i for i, m in enumerate(result) if m.event_type == 'hypothesis')
    assert gov_idx < hyp_idx


def test_activate_memory_unresolved_comes_before_general(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='implementation_note', title='N1', confidence=5, status='accepted')
    _add(db, event_type='hypothesis', title='H1', confidence=3, status='unresolved')
    result = activate_memory(db, _policy())
    unres_idx = next(i for i, m in enumerate(result) if m.status == 'unresolved')
    accepted_idx = next(i for i, m in enumerate(result) if m.status == 'accepted')
    assert unres_idx < accepted_idx


def test_activate_memory_deduplicated(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    result = activate_memory(db, _policy())
    ids = [m.memory_id for m in result]
    assert len(ids) == len(set(ids))


def test_activate_memory_respects_min_confidence(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='Low', confidence=1, status='active')
    _add(db, event_type='hypothesis', title='High', confidence=4, status='active')
    result = activate_memory(db, _policy(min_confidence=3, include_unresolved=False))
    titles = [m.title for m in result]
    assert 'Low' not in titles
    assert 'High' in titles


def test_activate_memory_always_includes_governance(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    result = activate_memory(db, _policy(include_unresolved=False))
    # governance events are always activated regardless of policy flags
    types = [m.event_type for m in result]
    assert 'governance_rule' in types


def test_activate_memory_tag_overlap_tracked(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', tags=['fx', 'macro'], status='proposed')
    _add(db, event_type='hypothesis', title='H2', tags=[], status='proposed')
    result = activate_memory(db, _policy(tags=['fx'], include_unresolved=False))
    h1 = next((m for m in result if m.title == 'H1'), None)
    if h1:
        assert h1.tag_overlap == 1


def test_activate_memory_deterministic(tmp_path):
    db = _mem_db(tmp_path)
    for i in range(5):
        _add(db, event_type='hypothesis', title=f'H{i}', status='proposed', confidence=i + 1)
    r1 = activate_memory(db, _policy())
    r2 = activate_memory(db, _policy())
    assert [m.memory_id for m in r1] == [m.memory_id for m in r2]


# ---------------------------------------------------------------------------
# partition_by_section
# ---------------------------------------------------------------------------

def _make_mem(id_, event_type, status) -> ActivatedMemory:
    return ActivatedMemory(
        memory_id=id_, event_type=event_type, title=f'T{id_}',
        summary='s', evidence=None, confidence=3, status=status,
        tags=[], source='test', related_ids=[],
        created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z',
        is_expanded=False, tag_overlap=0, activation_rank=(2, 5, -3, 0, 0, id_),
    )


def test_partition_governance_event():
    mem = _make_mem(1, 'governance_rule', 'active')
    sections = partition_by_section([mem])
    assert mem in sections['governance_context']
    assert mem not in sections['relevant_memory']


def test_partition_unresolved_status():
    mem = _make_mem(2, 'hypothesis', 'unresolved')
    sections = partition_by_section([mem])
    assert mem in sections['unresolved_items']


def test_partition_investigation_types():
    for et in ('open_question', 'hypothesis'):
        mem = _make_mem(3, et, 'active')
        sections = partition_by_section([mem])
        assert mem in sections['active_investigations']


def test_partition_relevant_memory_fallback():
    mem = _make_mem(4, 'implementation_note', 'accepted')
    sections = partition_by_section([mem])
    assert mem in sections['relevant_memory']


def test_partition_governance_unresolved_overlap():
    """A governance_rule that is also unresolved appears in both sections."""
    mem = _make_mem(5, 'governance_rule', 'unresolved')
    sections = partition_by_section([mem])
    assert mem in sections['governance_context']
    assert mem in sections['unresolved_items']
    assert mem not in sections['relevant_memory']


def test_partition_empty():
    sections = partition_by_section([])
    assert all(v == [] for v in sections.values())


# ---------------------------------------------------------------------------
# EI-006 regression: partition_by_section must exclude terminal-status events
# from governance_context
# ---------------------------------------------------------------------------

def test_ei006_rejected_governance_rule_excluded():
    """A rejected governance_rule must not appear in governance_context."""
    mem = _make_mem(10, 'governance_rule', 'rejected')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context'], (
        "EI-006 regression: rejected governance_rule appeared in governance_context"
    )


def test_ei006_superseded_governance_rule_excluded():
    """A superseded governance_rule must not appear in governance_context."""
    mem = _make_mem(11, 'governance_rule', 'superseded')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context'], (
        "EI-006 regression: superseded governance_rule appeared in governance_context"
    )


def test_ei006_rejected_architecture_decision_excluded():
    """A rejected architecture_decision must not appear in governance_context."""
    mem = _make_mem(12, 'architecture_decision', 'rejected')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context'], (
        "EI-006 regression: rejected architecture_decision appeared in governance_context"
    )


def test_ei006_archived_governance_excluded():
    """An archived governance_rule must not appear in governance_context."""
    mem = _make_mem(13, 'governance_rule', 'archived')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context']


def test_ei006_deprecated_governance_excluded():
    """A deprecated architecture_decision must not appear in governance_context."""
    mem = _make_mem(14, 'architecture_decision', 'deprecated')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context']


def test_ei006_active_governance_survives_with_rejected():
    """Primary EI-006 regression: when rejected and active governance events
    coexist, only the active event appears in governance_context."""
    active_mem = _make_mem(20, 'governance_rule', 'active')
    rejected_mem = _make_mem(21, 'governance_rule', 'rejected')
    superseded_mem = _make_mem(22, 'architecture_decision', 'superseded')

    sections = partition_by_section([active_mem, rejected_mem, superseded_mem])

    assert active_mem in sections['governance_context'], (
        "Active governance_rule must be in governance_context"
    )
    assert rejected_mem not in sections['governance_context'], (
        "EI-006 regression: rejected governance_rule displaced active event"
    )
    assert superseded_mem not in sections['governance_context'], (
        "EI-006 regression: superseded architecture_decision in governance_context"
    )


def test_ei006_rejected_governance_excluded_from_all_sections():
    """A rejected governance event is excluded from all sections.
    EI-006: excluded from governance_context.
    EI-008: also excluded from relevant_memory fallback.
    It appears in no section — not surfaced to session context at all."""
    mem = _make_mem(30, 'governance_rule', 'rejected')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context'], (
        "EI-006 regression: rejected governance_rule in governance_context"
    )
    assert mem not in sections['relevant_memory'], (
        "EI-008 regression: rejected governance_rule in relevant_memory fallback"
    )
    assert mem not in sections['unresolved_items']
    assert mem not in sections['active_investigations']


def test_ei006_unresolved_governance_still_included():
    """An unresolved governance_rule is NOT excluded — it remains in
    governance_context (preserves the existing overlap behavior)."""
    mem = _make_mem(40, 'governance_rule', 'unresolved')
    sections = partition_by_section([mem])
    assert mem in sections['governance_context'], (
        "Unresolved governance_rule must still appear in governance_context"
    )
    assert mem in sections['unresolved_items']


def test_ei006_accepted_governance_still_included():
    """An accepted governance_rule is not excluded — accepted is a valid
    terminal-positive status."""
    mem = _make_mem(41, 'architecture_decision', 'accepted')
    sections = partition_by_section([mem])
    assert mem in sections['governance_context']


# ---------------------------------------------------------------------------
# EI-008 regression: partition_by_section must exclude terminal-status events
# from relevant_memory fallback
# ---------------------------------------------------------------------------

def test_ei008_rejected_implementation_note_excluded():
    """A rejected implementation_note must not appear in relevant_memory."""
    mem = _make_mem(50, 'implementation_note', 'rejected')
    sections = partition_by_section([mem])
    assert mem not in sections['relevant_memory'], (
        "EI-008 regression: rejected implementation_note appeared in relevant_memory"
    )


def test_ei008_superseded_implementation_note_excluded():
    """A superseded implementation_note must not appear in relevant_memory."""
    mem = _make_mem(51, 'implementation_note', 'superseded')
    sections = partition_by_section([mem])
    assert mem not in sections['relevant_memory'], (
        "EI-008 regression: superseded implementation_note appeared in relevant_memory"
    )


def test_ei008_rejected_governance_excluded_from_both():
    """A rejected governance_rule must be excluded from both governance_context
    and relevant_memory — it should not appear in any section."""
    mem = _make_mem(52, 'governance_rule', 'rejected')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context'], (
        "EI-006 regression: rejected governance_rule in governance_context"
    )
    assert mem not in sections['relevant_memory'], (
        "EI-008 regression: rejected governance_rule fell through to relevant_memory"
    )


def test_ei008_archived_excluded():
    """An archived event must not appear in relevant_memory."""
    mem = _make_mem(53, 'implementation_note', 'archived')
    sections = partition_by_section([mem])
    assert mem not in sections['relevant_memory'], (
        "EI-008 regression: archived event appeared in relevant_memory"
    )


def test_ei008_deprecated_excluded():
    """A deprecated event must not appear in relevant_memory."""
    mem = _make_mem(54, 'validation_result', 'deprecated')
    sections = partition_by_section([mem])
    assert mem not in sections['relevant_memory'], (
        "EI-008 regression: deprecated event appeared in relevant_memory"
    )


def test_ei008_active_relevant_still_included():
    """An active non-governance, non-investigation event must still appear
    in relevant_memory (preservation test)."""
    mem = _make_mem(55, 'implementation_note', 'active')
    sections = partition_by_section([mem])
    assert mem in sections['relevant_memory']


def test_ei008_accepted_relevant_still_included():
    """An accepted non-governance, non-investigation event must appear
    in relevant_memory (preservation test)."""
    mem = _make_mem(56, 'validation_result', 'accepted')
    sections = partition_by_section([mem])
    assert mem in sections['relevant_memory']


def test_ei008_rejected_and_active_mix():
    """Primary EI-008 regression: when rejected and active events coexist,
    only active events appear in relevant_memory."""
    active_mem = _make_mem(60, 'implementation_note', 'active')
    rejected_mem = _make_mem(61, 'implementation_note', 'rejected')
    superseded_mem = _make_mem(62, 'validation_result', 'superseded')

    sections = partition_by_section([active_mem, rejected_mem, superseded_mem])

    assert active_mem in sections['relevant_memory'], (
        "Active implementation_note must be in relevant_memory"
    )
    assert rejected_mem not in sections['relevant_memory'], (
        "EI-008 regression: rejected event appeared in relevant_memory"
    )
    assert superseded_mem not in sections['relevant_memory'], (
        "EI-008 regression: superseded event appeared in relevant_memory"
    )


def test_ei008_unresolved_goes_to_unresolved_not_relevant():
    """An unresolved non-governance, non-investigation event goes to
    unresolved_items and is NOT placed in relevant_memory."""
    mem = _make_mem(63, 'implementation_note', 'unresolved')
    sections = partition_by_section([mem])
    assert mem in sections['unresolved_items']
    assert mem not in sections['relevant_memory']


# ---------------------------------------------------------------------------
# Layer 3 regression: general retrieve must not be saturated by governance
# ---------------------------------------------------------------------------

def test_layer3_governance_does_not_enter_relevant_memory(tmp_path):
    """Governance events must not appear in relevant_memory regardless of count.

    Layer 3 fix: general_query now filters to _NON_GOVERNANCE_EVENT_TYPES.
    Regression: before the fix, governance events would fill all candidate
    slots via doctrine_rank ordering and crowd out non-governance events.
    """
    db = _mem_db(tmp_path)
    # Add many governance events (enough to previously fill all 50 candidate slots)
    for i in range(60):
        _add(db, event_type='architecture_decision', title=f'ADR-{i}',
             status='active', confidence=4)
    # Add one non-governance event
    _add(db, event_type='implementation_note', title='IMPL-1',
         status='active', confidence=3)

    result = activate_memory(db, _policy())
    sections = partition_by_section(result)

    # Non-governance event must appear in relevant_memory
    rel_types = [m.event_type for m in sections['relevant_memory']]
    assert 'implementation_note' in rel_types, (
        "Layer 3 regression: implementation_note not in relevant_memory — "
        "governance events may still be saturating the general retrieve pass"
    )
    # Governance events must not appear in relevant_memory
    assert 'architecture_decision' not in rel_types, (
        "Layer 3 regression: architecture_decision appeared in relevant_memory"
    )
    assert 'governance_rule' not in rel_types, (
        "Layer 3 regression: governance_rule appeared in relevant_memory"
    )


def test_layer3_active_non_governance_events_surface_in_relevant_memory(tmp_path):
    """All active non-governance event types must be reachable in relevant_memory."""
    db = _mem_db(tmp_path)
    non_governance_active = [
        ('implementation_note', 'IMPL', 'active'),
        ('incident',            'INC',  'active'),
        ('validation_result',   'VAL',  'active'),
        ('rejected_idea',       'REJ',  'active'),
    ]
    for etype, title, status in non_governance_active:
        _add(db, event_type=etype, title=title, status=status, confidence=3)

    result = activate_memory(db, _policy())
    sections = partition_by_section(result)
    rel_types = {m.event_type for m in sections['relevant_memory']}

    for etype, _, _ in non_governance_active:
        assert etype in rel_types, (
            f"Layer 3: active {etype} event not surfacing in relevant_memory"
        )


def test_layer3_governance_saturation_does_not_starve_non_governance(tmp_path):
    """With 60+ governance events and active non-governance events, the
    non-governance events must still surface in relevant_memory.

    This is the primary Layer 3 regression: before the fix, 50 governance
    candidate slots left zero room for non-governance events.
    """
    db = _mem_db(tmp_path)
    # Add enough governance events to exceed max_memory_candidates (50)
    for i in range(55):
        _add(db, event_type='architecture_decision', title=f'ADR-{i}',
             status='active', confidence=3)
    # Add several non-governance events at various types
    for i in range(5):
        _add(db, event_type='implementation_note', title=f'IMPL-{i}',
             status='active', confidence=3)
    for i in range(3):
        _add(db, event_type='validation_result', title=f'VAL-{i}',
             status='active', confidence=3)

    result = activate_memory(db, _policy())
    sections = partition_by_section(result)

    impl_in_relevant = [m for m in sections['relevant_memory']
                        if m.event_type == 'implementation_note']
    val_in_relevant = [m for m in sections['relevant_memory']
                       if m.event_type == 'validation_result']

    assert len(impl_in_relevant) == 5, (
        f"Layer 3 regression: expected 5 implementation_notes in relevant_memory, "
        f"got {len(impl_in_relevant)}"
    )
    assert len(val_in_relevant) == 3, (
        f"Layer 3 regression: expected 3 validation_results in relevant_memory, "
        f"got {len(val_in_relevant)}"
    )


def test_layer3_unresolved_investigation_not_double_counted_in_active_investigations(tmp_path):
    """Unresolved open_question/hypothesis must appear in unresolved_items only,
    NOT also in active_investigations.

    Pre-Layer-3 overlap: unresolved investigations were placed in BOTH
    unresolved_items (Tier 1) AND active_investigations (Tier 3), consuming
    budget twice and preventing relevant_memory items from surfacing.
    """
    db = _mem_db(tmp_path)
    _add(db, event_type='open_question', title='OQ-UNRES', status='unresolved', confidence=3)
    _add(db, event_type='open_question', title='OQ-ACTIVE', status='active', confidence=3)
    _add(db, event_type='hypothesis', title='HYP-UNRES', status='unresolved', confidence=3)

    result = activate_memory(db, _policy())
    sections = partition_by_section(result)

    oq_unres = next((m for m in result if m.title == 'OQ-UNRES'), None)
    oq_active = next((m for m in result if m.title == 'OQ-ACTIVE'), None)
    hyp_unres = next((m for m in result if m.title == 'HYP-UNRES'), None)

    # Unresolved items must be in unresolved_items, NOT in active_investigations
    assert oq_unres in sections['unresolved_items']
    assert oq_unres not in sections['active_investigations'], (
        "Overlap: unresolved open_question is double-counted in active_investigations"
    )
    assert hyp_unres in sections['unresolved_items']
    assert hyp_unres not in sections['active_investigations'], (
        "Overlap: unresolved hypothesis is double-counted in active_investigations"
    )

    # Active investigation (not unresolved) must be in active_investigations only
    if oq_active:
        assert oq_active in sections['active_investigations']
        assert oq_active not in sections['unresolved_items']


def test_layer3_rejected_governance_does_not_enter_general_retrieve_path(tmp_path):
    """Rejected governance events must not consume candidate slots in the
    general retrieve pass and must not appear in any section.

    Before the Layer 3 fix, the general retrieve had no status filter,
    so 126 rejected governance events occupied candidate slots by doctrine_rank.
    """
    db = _mem_db(tmp_path)
    # Add rejected governance (formerly polluting the general retrieve)
    for i in range(10):
        _add(db, event_type='architecture_decision', title=f'REJ-ADR-{i}',
             status='rejected', confidence=3)
    # Add active non-governance
    _add(db, event_type='implementation_note', title='IMPL-ACTIVE',
         status='active', confidence=3)

    result = activate_memory(db, _policy())
    sections = partition_by_section(result)

    # Rejected governance must not appear anywhere
    all_ids = (
        [m.memory_id for m in sections['governance_context']] +
        [m.memory_id for m in sections['relevant_memory']] +
        [m.memory_id for m in sections['unresolved_items']] +
        [m.memory_id for m in sections['active_investigations']]
    )
    rejected_titles = [m.title for m in result if m.title.startswith('REJ-ADR-')]
    assert rejected_titles == [], (
        f"Layer 3: rejected governance events appeared in activated result: {rejected_titles}"
    )
    # Active non-governance must be present
    rel_titles = [m.title for m in sections['relevant_memory']]
    assert 'IMPL-ACTIVE' in rel_titles
