import pytest
from memory import service
from memory.retrieval import (
    DOCTRINE_PRIORITY,
    RetrievalQuery,
    ScoredEvent,
    retrieve,
    retrieve_adaptations,
    retrieve_governance,
    retrieve_unresolved,
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


# ---------------------------------------------------------------------------
# doctrine priority ordering
# ---------------------------------------------------------------------------

class TestDoctrineOrdering:
    def test_governance_rule_ranked_first(self):
        assert DOCTRINE_PRIORITY['governance_rule'] == 1

    def test_architecture_decision_ranked_second(self):
        assert DOCTRINE_PRIORITY['architecture_decision'] == 2

    def test_validation_result_ranked_third(self):
        assert DOCTRINE_PRIORITY['validation_result'] == 3

    def test_adaptation_ranked_fourth(self):
        assert DOCTRINE_PRIORITY['adaptation'] == 4

    def test_hypothesis_ranked_fifth(self):
        assert DOCTRINE_PRIORITY['hypothesis'] == 5

    def test_implementation_note_ranked_sixth(self):
        assert DOCTRINE_PRIORITY['implementation_note'] == 6

    def test_unlisted_type_gets_default(self):
        from memory.retrieval import _DEFAULT_DOCTRINE_RANK
        assert DOCTRINE_PRIORITY.get('incident', _DEFAULT_DOCTRINE_RANK) == _DEFAULT_DOCTRINE_RANK

    def test_composite_key_doctrine_governs_over_confidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis', confidence=5)
        _add(db, event_type='governance_rule', confidence=1, status='accepted')
        results = retrieve(db, RetrievalQuery())
        assert results[0].event.event_type == 'governance_rule'

    def test_governance_sorts_before_architecture(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='architecture_decision', confidence=5, status='accepted')
        _add(db, event_type='governance_rule', confidence=1, status='accepted')
        results = retrieve(db, RetrievalQuery())
        assert results[0].event.event_type == 'governance_rule'


# ---------------------------------------------------------------------------
# confidence ordering
# ---------------------------------------------------------------------------

class TestConfidenceOrdering:
    def test_higher_confidence_ranks_first_within_same_type(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis', confidence=2)
        _add(db, event_type='hypothesis', confidence=5)
        results = retrieve(db, RetrievalQuery())
        types = [s.event.event_type for s in results]
        assert all(t == 'hypothesis' for t in types)
        confs = [s.event.confidence for s in results]
        assert confs[0] == 5

    def test_confidence_tiebreak_uses_insertion_order(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, event_type='hypothesis', confidence=3)
        e2 = _add(db, event_type='hypothesis', confidence=3)
        results = retrieve(db, RetrievalQuery(expand_related=False))
        ids = [s.event.id for s in results]
        # Same confidence, same timestamp (sub-second): higher id (later insertion) wins.
        assert ids[0] == e2.id


# ---------------------------------------------------------------------------
# recency ordering
# ---------------------------------------------------------------------------

class TestRecencyOrdering:
    def test_recently_updated_ranks_higher(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, event_type='hypothesis', confidence=3)
        e2 = _add(db, event_type='hypothesis', confidence=3)
        service.update_status(db, e2.id, 'accepted', reason='ok', created_by='u')
        results = retrieve(db, RetrievalQuery(expand_related=False))
        # e2 has higher id, so it wins the insertion-order tiebreak when both updated_at
        # timestamps fall within the same second.
        assert results[0].event.id == e2.id

    def test_recency_rank_assigned(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis', confidence=3)
        _add(db, event_type='hypothesis', confidence=3)
        results = retrieve(db, RetrievalQuery(expand_related=False))
        ranks = [s.recency_rank for s in results]
        assert 0 in ranks


# ---------------------------------------------------------------------------
# tag filtering
# ---------------------------------------------------------------------------

class TestTagFiltering:
    def test_tag_filter_returns_matching_events(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, tags=['governance'], title='has tag')
        _add(db, tags=['other'], title='different tag')
        results = retrieve(db, RetrievalQuery(tags=['governance'], expand_related=False))
        titles = [s.event.title for s in results]
        assert 'has tag' in titles

    def test_tag_filter_excludes_non_matching(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, tags=['governance'], title='has tag')
        _add(db, tags=['other'], title='different tag')
        results = retrieve(db, RetrievalQuery(tags=['governance'], expand_related=False))
        titles = [s.event.title for s in results]
        assert 'different tag' not in titles

    def test_tag_overlap_counted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        # Insert 'one tag' first so 'three tags' gets a higher id (better recency tiebreak).
        _add(db, tags=['a'], title='one tag')
        _add(db, tags=['a', 'b', 'c'], title='three tags')
        results = retrieve(db, RetrievalQuery(tags=['a', 'b', 'c'], expand_related=False))
        assert results[0].event.title == 'three tags'
        assert results[0].tag_overlap == 3

    def test_no_tags_returns_all(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, tags=['x'])
        _add(db, tags=['y'])
        results = retrieve(db, RetrievalQuery(expand_related=False))
        assert len(results) == 2


# ---------------------------------------------------------------------------
# type filtering
# ---------------------------------------------------------------------------

class TestTypeFiltering:
    def test_event_type_filter(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis')
        _add(db, event_type='incident')
        results = retrieve(db, RetrievalQuery(event_types=['hypothesis'], expand_related=False))
        assert all(s.event.event_type == 'hypothesis' for s in results)
        assert len(results) == 1

    def test_multiple_event_types(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis')
        _add(db, event_type='incident')
        _add(db, event_type='governance_rule', status='accepted')
        results = retrieve(db, RetrievalQuery(
            event_types=['hypothesis', 'incident'], expand_related=False
        ))
        types = {s.event.event_type for s in results}
        assert types == {'hypothesis', 'incident'}


# ---------------------------------------------------------------------------
# status filtering
# ---------------------------------------------------------------------------

class TestStatusFiltering:
    def test_status_filter(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed')
        _add(db, status='accepted')
        results = retrieve(db, RetrievalQuery(statuses=['proposed'], expand_related=False))
        assert all(s.event.status == 'proposed' for s in results)
        assert len(results) == 1

    def test_multiple_statuses(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed')
        _add(db, status='accepted')
        _add(db, status='archived')
        results = retrieve(db, RetrievalQuery(
            statuses=['proposed', 'accepted'], expand_related=False
        ))
        statuses = {s.event.status for s in results}
        assert statuses == {'proposed', 'accepted'}


# ---------------------------------------------------------------------------
# unresolved prioritization
# ---------------------------------------------------------------------------

class TestRetrieveUnresolved:
    def test_returns_unresolved(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        results = retrieve_unresolved(db)
        assert len(results) == 1
        assert results[0].event.status == 'unresolved'

    def test_returns_proposed(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed')
        results = retrieve_unresolved(db)
        assert len(results) == 1

    def test_excludes_accepted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted')
        results = retrieve_unresolved(db)
        assert len(results) == 0

    def test_empty_db(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert retrieve_unresolved(db) == []


# ---------------------------------------------------------------------------
# adaptations helper
# ---------------------------------------------------------------------------

class TestRetrieveAdaptations:
    def test_returns_adaptation_type(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='adaptation', status='accepted')
        _add(db, event_type='hypothesis')
        results = retrieve_adaptations(db)
        assert all(s.event.event_type == 'adaptation' for s in results)
        assert len(results) == 1

    def test_tag_filter_applied(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='adaptation', status='accepted', tags=['fx'])
        _add(db, event_type='adaptation', status='accepted', tags=['rates'])
        results = retrieve_adaptations(db, tags=['fx'])
        assert len(results) == 1
        assert results[0].event.tags == ['fx']


# ---------------------------------------------------------------------------
# governance helper
# ---------------------------------------------------------------------------

class TestRetrieveGovernance:
    def test_returns_governance_and_architecture(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='accepted')
        _add(db, event_type='architecture_decision', status='accepted')
        _add(db, event_type='hypothesis')
        results = retrieve_governance(db)
        types = {s.event.event_type for s in results}
        assert types == {'governance_rule', 'architecture_decision'}

    def test_governance_sorts_before_architecture(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='architecture_decision', confidence=5, status='accepted')
        _add(db, event_type='governance_rule', confidence=1, status='accepted')
        results = retrieve_governance(db)
        assert results[0].event.event_type == 'governance_rule'


# ---------------------------------------------------------------------------
# related-event expansion
# ---------------------------------------------------------------------------

class TestExpandRelated:
    def test_related_ids_expanded(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, related_ids=[e1.id], status='proposed')
        results = retrieve(db, RetrievalQuery(
            statuses=['proposed'], expand_related=True
        ))
        ids = [s.event.id for s in results]
        assert e1.id in ids

    def test_expanded_events_marked(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, related_ids=[e1.id], status='proposed')
        results = retrieve(db, RetrievalQuery(
            statuses=['proposed'], expand_related=True
        ))
        by_id = {s.event.id: s for s in results}
        assert by_id[e2.id].is_expanded is False
        assert by_id[e1.id].is_expanded is True

    def test_expanded_events_sort_after_primary(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, confidence=5, status='accepted')
        e2 = _add(db, related_ids=[e1.id], confidence=1, status='proposed')
        results = retrieve(db, RetrievalQuery(
            statuses=['proposed'], expand_related=True
        ))
        primary_first = results[0].is_expanded is False
        assert primary_first

    def test_links_expand_related(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='proposed')
        service.link_memory_events(db, e2.id, e1.id, 'supports')
        results = retrieve(db, RetrievalQuery(
            statuses=['proposed'], expand_related=True
        ))
        ids = [s.event.id for s in results]
        assert e1.id in ids

    def test_no_duplicate_events(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, related_ids=[e1.id], status='proposed')
        results = retrieve(db, RetrievalQuery(expand_related=True))
        ids = [s.event.id for s in results]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# limit / offset
# ---------------------------------------------------------------------------

class TestLimitOffset:
    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db)
        results = retrieve(db, RetrievalQuery(limit=3, expand_related=False))
        assert len(results) == 3

    def test_offset_skips(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db)
        all_results = retrieve(db, RetrievalQuery(limit=5, expand_related=False))
        offset_results = retrieve(db, RetrievalQuery(limit=5, offset=2, expand_related=False))
        assert offset_results[0].event.id == all_results[2].event.id

    def test_offset_beyond_results_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        results = retrieve(db, RetrievalQuery(offset=100, expand_related=False))
        assert results == []


# ---------------------------------------------------------------------------
# min_confidence filter
# ---------------------------------------------------------------------------

class TestMinConfidence:
    def test_min_confidence_filters(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, confidence=2)
        _add(db, confidence=4)
        results = retrieve(db, RetrievalQuery(min_confidence=3, expand_related=False))
        assert all(s.event.confidence >= 3 for s in results)
        assert len(results) == 1

    def test_min_confidence_1_returns_all(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, confidence=1)
        _add(db, confidence=5)
        results = retrieve(db, RetrievalQuery(min_confidence=1, expand_related=False))
        assert len(results) == 2


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_repeated_calls_identical(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for i in range(5):
            _add(db, event_type='hypothesis', confidence=i % 5 + 1, tags=[f'tag{i}'])
        q = RetrievalQuery(expand_related=False)
        r1 = [s.event.id for s in retrieve(db, q)]
        r2 = [s.event.id for s in retrieve(db, q)]
        assert r1 == r2

    def test_composite_key_is_tuple(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        results = retrieve(db, RetrievalQuery(expand_related=False))
        key = results[0].composite_key
        assert isinstance(key, tuple)
        assert len(key) == 6
