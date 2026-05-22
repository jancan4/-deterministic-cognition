import pytest
from memory import service
from memory.retrieval import RetrievalQuery, retrieve
from memory.context_builder import (
    AssembledContext,
    ContextEntry,
    ContextSection,
    build_context,
    _assign_section,
    _SECTION_ORDER,
)


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
    return service.add_memory_event(db, **defaults)


def _retrieve_all(db):
    return retrieve(db, RetrievalQuery(expand_related=False, limit=1000))


# ---------------------------------------------------------------------------
# section assignment
# ---------------------------------------------------------------------------

class TestSectionAssignment:
    def test_governance_rule_to_governance_context(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='governance_rule', status='accepted')
        assert _assign_section(ev) == 'GOVERNANCE CONTEXT'

    def test_architecture_decision_to_architecture_context(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='architecture_decision', status='accepted')
        assert _assign_section(ev) == 'ARCHITECTURE CONTEXT'

    def test_open_question_to_active_questions(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='open_question', status='unresolved')
        assert _assign_section(ev) == 'ACTIVE QUESTIONS'

    def test_adaptation_to_recent_adaptations(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='adaptation', status='accepted')
        assert _assign_section(ev) == 'RECENT ADAPTATIONS'

    def test_experiment_to_related_experiments(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='experiment', status='proposed')
        assert _assign_section(ev) == 'RELATED EXPERIMENTS'

    def test_hypothesis_to_related_experiments(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='hypothesis')
        assert _assign_section(ev) == 'RELATED EXPERIMENTS'

    def test_other_types_to_relevant_memory_events(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for etype in ('incident', 'validation_result', 'regime_observation',
                      'implementation_note', 'rejected_idea', 'source_reference'):
            ev = _add(db, event_type=etype, status='accepted')
            assert _assign_section(ev) == 'RELEVANT MEMORY EVENTS'


# ---------------------------------------------------------------------------
# context structure
# ---------------------------------------------------------------------------

class TestContextStructure:
    def test_returns_assembled_context(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events)
        assert isinstance(ctx, AssembledContext)

    def test_sections_in_correct_order(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ctx = build_context([])
        names = [s.name for s in ctx.sections]
        assert names == _SECTION_ORDER

    def test_total_events_counted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(3):
            _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events)
        assert ctx.total_events == 3

    def test_included_events_counted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events)
        assert ctx.included_events == 1

    def test_empty_events_gives_zero_included(self, tmp_path):
        ctx = build_context([])
        assert ctx.included_events == 0
        assert ctx.total_events == 0


# ---------------------------------------------------------------------------
# field presence
# ---------------------------------------------------------------------------

class TestFieldPresence:
    def test_entry_has_required_fields(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted')
        events = _retrieve_all(db)
        ctx = build_context(events)
        governance = next(s for s in ctx.sections if s.name == 'GOVERNANCE CONTEXT')
        assert len(governance.entries) == 1
        entry = governance.entries[0]
        assert entry.event_id is not None
        assert entry.event_type == 'governance_rule'
        assert entry.title == 'Test'
        assert entry.summary == 'Test summary'
        assert entry.confidence == 3
        assert entry.status == 'accepted'

    def test_evidence_present_when_set(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, evidence='Supporting evidence text')
        events = _retrieve_all(db)
        ctx = build_context(events)
        all_entries = [e for s in ctx.sections for e in s.entries]
        assert any(e.evidence == 'Supporting evidence text' for e in all_entries)

    def test_evidence_none_when_not_set(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events)
        all_entries = [e for s in ctx.sections for e in s.entries]
        assert all(e.evidence is None for e in all_entries)

    def test_tags_present(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, tags=['fx', 'governance'])
        events = _retrieve_all(db)
        ctx = build_context(events)
        all_entries = [e for s in ctx.sections for e in s.entries]
        assert any(set(e.tags) == {'fx', 'governance'} for e in all_entries)


# ---------------------------------------------------------------------------
# char-budget truncation
# ---------------------------------------------------------------------------

class TestCharBudget:
    def test_unlimited_budget_includes_all(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events, char_budget=999999)
        assert ctx.included_events == 5

    def test_zero_budget_includes_none(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events, char_budget=0)
        assert ctx.included_events == 0

    def test_small_budget_limits_entries(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(10):
            _add(db, summary='A' * 200)
        events = _retrieve_all(db)
        ctx = build_context(events, char_budget=500)
        assert ctx.included_events < 10

    def test_chars_used_does_not_exceed_budget(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events, char_budget=300)
        assert ctx.chars_used <= 300

    def test_budget_reported(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        events = _retrieve_all(db)
        ctx = build_context(events, char_budget=5000)
        assert ctx.char_budget == 5000


# ---------------------------------------------------------------------------
# include_sections filter
# ---------------------------------------------------------------------------

class TestIncludeSections:
    def test_include_sections_limits_output(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted')
        _add(db, event_type='hypothesis')
        events = _retrieve_all(db)
        ctx = build_context(events, include_sections=['GOVERNANCE CONTEXT'])
        names = [s.name for s in ctx.sections]
        assert names == ['GOVERNANCE CONTEXT']

    def test_excluded_section_events_not_included(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis')
        events = _retrieve_all(db)
        ctx = build_context(events, include_sections=['GOVERNANCE CONTEXT'])
        assert ctx.included_events == 0


# ---------------------------------------------------------------------------
# to_text and to_dict
# ---------------------------------------------------------------------------

class TestOutputFormats:
    def test_to_text_returns_string(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted')
        events = _retrieve_all(db)
        ctx = build_context(events)
        text = ctx.to_text()
        assert isinstance(text, str)

    def test_to_text_contains_section_header(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted')
        events = _retrieve_all(db)
        ctx = build_context(events)
        text = ctx.to_text()
        assert 'GOVERNANCE CONTEXT' in text

    def test_to_text_contains_event_title(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted', title='My Rule')
        events = _retrieve_all(db)
        ctx = build_context(events)
        assert 'My Rule' in ctx.to_text()

    def test_to_text_empty_when_no_events(self):
        ctx = build_context([])
        assert ctx.to_text().strip() == ''

    def test_to_dict_structure(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        events = _retrieve_all(db)
        ctx = build_context(events)
        d = ctx.to_dict()
        assert 'char_budget' in d
        assert 'chars_used' in d
        assert 'total_events' in d
        assert 'included_events' in d
        assert 'sections' in d
        assert isinstance(d['sections'], list)

    def test_to_dict_section_has_entries(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted')
        events = _retrieve_all(db)
        ctx = build_context(events)
        d = ctx.to_dict()
        gov = next(s for s in d['sections'] if s['name'] == 'GOVERNANCE CONTEXT')
        assert gov['entry_count'] == 1
        entry = gov['entries'][0]
        assert 'event_id' in entry
        assert 'event_type' in entry
        assert 'title' in entry
        assert 'summary' in entry
        assert 'confidence' in entry
        assert 'status' in entry
        assert 'tags' in entry
        assert 'is_expanded' in entry


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

class TestContextDeterminism:
    def test_repeated_build_identical_text(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for i in range(4):
            _add(db, confidence=i + 1, event_type='hypothesis')
        events = _retrieve_all(db)
        t1 = build_context(events).to_text()
        t2 = build_context(events).to_text()
        assert t1 == t2

    def test_repeated_build_identical_dict(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted')
        events = _retrieve_all(db)
        d1 = build_context(events).to_dict()
        d2 = build_context(events).to_dict()
        assert d1 == d2
