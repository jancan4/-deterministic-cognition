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
    _add(db, event_type='hypothesis', title='Low', confidence=1, status='proposed')
    _add(db, event_type='hypothesis', title='High', confidence=4, status='proposed')
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


def test_ei006_rejected_governance_falls_to_relevant_memory():
    """A rejected governance event excluded from governance_context falls to
    relevant_memory (documents the post-fix fallthrough behavior)."""
    mem = _make_mem(30, 'governance_rule', 'rejected')
    sections = partition_by_section([mem])
    assert mem not in sections['governance_context']
    assert mem in sections['relevant_memory']


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
