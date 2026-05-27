import json
import sqlite3

import pytest
from memory import service
from memory.retrieval import (
    DOCTRINE_PRIORITY,
    RETRIEVAL_SCORING_VERSION,
    RetrievalLogEntry,
    RetrievalQuery,
    ScoredEvent,
    _canonical_query_dict,
    _cosine_similarity,
    _compute_semantic_ranks,
    _query_hash,
    get_retrieval_log,
    list_retrieval_log,
    log_retrieval_query,
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

    # ------------------------------------------------------------------
    # EI-004 regression: rejected events must not surface in governance tier
    # ------------------------------------------------------------------

    def test_rejected_governance_rule_excluded(self, tmp_path):
        """retrieve_governance must not return rejected governance_rule events."""
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='rejected')
        results = retrieve_governance(db)
        assert results == [], (
            "Rejected governance_rule surfaced in governance tier: "
            + str([s.event.title for s in results])
        )

    def test_rejected_architecture_decision_excluded(self, tmp_path):
        """retrieve_governance must not return rejected architecture_decision events."""
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='architecture_decision', status='rejected')
        results = retrieve_governance(db)
        assert results == [], (
            "Rejected architecture_decision surfaced in governance tier: "
            + str([s.event.title for s in results])
        )

    def test_active_governance_rule_included(self, tmp_path):
        """retrieve_governance must return active governance_rule events."""
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='governance_rule', status='active')
        results = retrieve_governance(db)
        ids = [s.event.id for s in results]
        assert ev.id in ids, "Active governance_rule missing from governance tier"

    def test_active_architecture_decision_included(self, tmp_path):
        """retrieve_governance must return active architecture_decision events."""
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, event_type='architecture_decision', status='active')
        results = retrieve_governance(db)
        ids = [s.event.id for s in results]
        assert ev.id in ids, "Active architecture_decision missing from governance tier"

    def test_ei004_high_id_rejected_does_not_displace_active(self, tmp_path):
        """
        EI-004 regression: a rejected event with a higher ID must not rank above
        an active event with a lower ID in the governance tier.

        Scenario:
          - id=1: governance_rule, active, confidence=3
          - id=2: governance_rule, rejected, confidence=3 (higher ID, same confidence)

        Before fix: both surfaced; id=2 (higher ID) won the recency tiebreaker.
        After fix: only id=1 is returned.
        """
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        active_ev = _add(db, event_type='governance_rule', status='active', confidence=3)
        rejected_ev = _add(db, event_type='governance_rule', status='rejected', confidence=3)
        results = retrieve_governance(db)
        ids = [s.event.id for s in results]
        assert active_ev.id in ids, "Active governance_rule not in results"
        assert rejected_ev.id not in ids, (
            "Rejected governance_rule (id=%d) displaced active event (id=%d)"
            % (rejected_ev.id, active_ev.id)
        )

    def test_ei004_active_surfaces_when_only_active(self, tmp_path):
        """
        EI-004 regression: with only active governance events, all active events
        surface regardless of count, confirming the filter doesn't suppress actives.
        """
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ids = [
            _add(db, event_type='governance_rule', status='active', confidence=4).id,
            _add(db, event_type='architecture_decision', status='active', confidence=3).id,
            _add(db, event_type='governance_rule', status='active', confidence=2).id,
        ]
        results = retrieve_governance(db)
        result_ids = [s.event.id for s in results]
        for eid in ids:
            assert eid in result_ids, "Active governance event id=%d missing from tier" % eid

    def test_superseded_governance_excluded(self, tmp_path):
        """retrieve_governance must not surface superseded events."""
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='governance_rule', status='superseded')
        results = retrieve_governance(db)
        assert results == [], "Superseded governance_rule surfaced in governance tier"

    def test_mixed_statuses_only_active_and_accepted_returned(self, tmp_path):
        """
        With a mix of active, accepted, rejected, superseded governance events,
        only active and accepted events must surface.
        """
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        active_ev   = _add(db, event_type='governance_rule', status='active')
        accepted_ev = _add(db, event_type='governance_rule', status='accepted')
        rejected_ev = _add(db, event_type='governance_rule', status='rejected')
        super_ev    = _add(db, event_type='governance_rule', status='superseded')
        results = retrieve_governance(db)
        ids = {s.event.id for s in results}
        assert active_ev.id in ids,   "active governance_rule must be in results"
        assert accepted_ev.id in ids, "accepted governance_rule must be in results"
        assert rejected_ev.id not in ids,  "rejected governance_rule must not be in results"
        assert super_ev.id not in ids,     "superseded governance_rule must not be in results"


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
        assert len(key) == 7


# ---------------------------------------------------------------------------
# retrieval logging substrate
# ---------------------------------------------------------------------------

class TestCanonicalQueryDict:
    def test_tags_sorted(self):
        q = RetrievalQuery(tags=['z', 'a', 'm'])
        d = _canonical_query_dict(q)
        assert d['tags'] == ['a', 'm', 'z']

    def test_event_types_sorted(self):
        q = RetrievalQuery(event_types=['hypothesis', 'adaptation'])
        d = _canonical_query_dict(q)
        assert d['event_types'] == ['adaptation', 'hypothesis']

    def test_statuses_sorted(self):
        q = RetrievalQuery(statuses=['proposed', 'accepted'])
        d = _canonical_query_dict(q)
        assert d['statuses'] == ['accepted', 'proposed']

    def test_scalars_preserved(self):
        q = RetrievalQuery(min_confidence=3, limit=10, offset=5, expand_related=False)
        d = _canonical_query_dict(q)
        assert d['min_confidence'] == 3
        assert d['limit'] == 10
        assert d['offset'] == 5
        assert d['expand_related'] is False

    def test_deterministic_json(self):
        q = RetrievalQuery(tags=['b', 'a'], event_types=['hypothesis'])
        j1 = json.dumps(_canonical_query_dict(q), sort_keys=True, ensure_ascii=True)
        j2 = json.dumps(_canonical_query_dict(q), sort_keys=True, ensure_ascii=True)
        assert j1 == j2

    def test_different_tag_order_same_json(self):
        q1 = RetrievalQuery(tags=['b', 'a'])
        q2 = RetrievalQuery(tags=['a', 'b'])
        j1 = json.dumps(_canonical_query_dict(q1), sort_keys=True, ensure_ascii=True)
        j2 = json.dumps(_canonical_query_dict(q2), sort_keys=True, ensure_ascii=True)
        assert j1 == j2


class TestQueryHash:
    def test_hash_is_16_chars(self):
        q = RetrievalQuery()
        j = json.dumps(_canonical_query_dict(q), sort_keys=True, ensure_ascii=True)
        h = _query_hash(j)
        assert len(h) == 16

    def test_hash_is_valid_hex(self):
        q = RetrievalQuery(tags=['fx'])
        j = json.dumps(_canonical_query_dict(q), sort_keys=True, ensure_ascii=True)
        h = _query_hash(j)
        int(h, 16)

    def test_same_query_same_hash(self):
        q = RetrievalQuery(tags=['fx'], event_types=['hypothesis'])
        j = json.dumps(_canonical_query_dict(q), sort_keys=True, ensure_ascii=True)
        assert _query_hash(j) == _query_hash(j)

    def test_different_queries_different_hashes(self):
        q1 = RetrievalQuery(tags=['fx'])
        q2 = RetrievalQuery(tags=['rates'])
        j1 = json.dumps(_canonical_query_dict(q1), sort_keys=True, ensure_ascii=True)
        j2 = json.dumps(_canonical_query_dict(q2), sort_keys=True, ensure_ascii=True)
        assert _query_hash(j1) != _query_hash(j2)


class TestLogRetrievalQuery:
    def test_log_returns_int_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(), [], actor='test')
        assert isinstance(log_id, int)
        assert log_id >= 1

    def test_log_persisted_and_fetchable(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(tags=['fx']), [], actor='quant', session_id='s1')
        entry = get_retrieval_log(db, log_id)
        assert entry.actor == 'quant'
        assert entry.session_id == 's1'
        assert entry.scoring_version == RETRIEVAL_SCORING_VERSION
        assert entry.result_count == 0

    def test_result_ids_preserve_rank_order(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db)
        e2 = _add(db)
        q = RetrievalQuery(expand_related=False)
        results = retrieve(db, q)
        log_id = log_retrieval_query(db, q, results, actor='system')
        entry = get_retrieval_log(db, log_id)
        logged_ids = json.loads(entry.result_event_ids_json)
        assert logged_ids == [s.event.id for s in results]

    def test_query_hash_matches_canonical(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        q = RetrievalQuery(tags=['macro'], min_confidence=2)
        log_id = log_retrieval_query(db, q, [], actor='system')
        entry = get_retrieval_log(db, log_id)
        expected_json = json.dumps(_canonical_query_dict(q), sort_keys=True, ensure_ascii=True)
        assert entry.query_hash == _query_hash(expected_json)
        assert entry.query_json == expected_json

    def test_retrieve_log_retrieval_kwarg(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        retrieve(db, RetrievalQuery(expand_related=False), log_retrieval=True, actor='agent')
        entries = list_retrieval_log(db)
        assert len(entries) == 1
        assert entries[0].actor == 'agent'


class TestListRetrievalLog:
    def test_returns_all_entries(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_retrieval_query(db, RetrievalQuery(), [], actor='a')
        log_retrieval_query(db, RetrievalQuery(), [], actor='b')
        entries = list_retrieval_log(db)
        assert len(entries) == 2

    def test_ordered_descending_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_retrieval_query(db, RetrievalQuery(), [], actor='first')
        log_retrieval_query(db, RetrievalQuery(), [], actor='second')
        entries = list_retrieval_log(db)
        assert entries[0].actor == 'second'

    def test_session_id_filter(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_retrieval_query(db, RetrievalQuery(), [], actor='a', session_id='s1')
        log_retrieval_query(db, RetrievalQuery(), [], actor='b', session_id='s2')
        entries = list_retrieval_log(db, session_id='s1')
        assert len(entries) == 1
        assert entries[0].actor == 'a'

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            log_retrieval_query(db, RetrievalQuery(), [], actor='system')
        entries = list_retrieval_log(db, limit=3)
        assert len(entries) == 3


class TestRetrievalLogEntry:
    def test_to_dict_roundtrip_fields(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(tags=['fx']), [], actor='test', session_id='sess')
        entry = get_retrieval_log(db, log_id)
        d = entry.to_dict()
        assert d['id'] == log_id
        assert d['actor'] == 'test'
        assert d['session_id'] == 'sess'
        assert d['scoring_version'] == RETRIEVAL_SCORING_VERSION

    def test_query_property_reconstructs(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        q = RetrievalQuery(tags=['fx', 'macro'], min_confidence=3, limit=5, offset=1, expand_related=False)
        log_id = log_retrieval_query(db, q, [], actor='system')
        entry = get_retrieval_log(db, log_id)
        reconstructed = entry.query
        assert reconstructed.tags == sorted(q.tags)
        assert reconstructed.min_confidence == q.min_confidence
        assert reconstructed.limit == q.limit
        assert reconstructed.offset == q.offset
        assert reconstructed.expand_related == q.expand_related

    def test_status_field_defaults_active(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(), [], actor='agent')
        entry = get_retrieval_log(db, log_id)
        assert entry.status == 'active'

    def test_status_present_in_to_dict(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(), [], actor='system')
        entry = get_retrieval_log(db, log_id)
        d = entry.to_dict()
        assert 'status' in d
        assert d['status'] == 'active'


# ---------------------------------------------------------------------------
# governance schema validation on retrieval_log
# ---------------------------------------------------------------------------

class TestGovernanceOnRetrieval:
    def test_validate_schema_passes_on_retrieval_log(self, tmp_path):
        from memory.artifact_governance import validate_artifact_table_schema, GovernanceSchemaError
        import sqlite3
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        conn = sqlite3.connect(db)
        validate_artifact_table_schema(
            conn,
            'retrieval_log',
            required_columns=[
                'id', 'query_hash', 'session_id', 'query_json',
                'scoring_version', 'scoring_params_json',
                'result_event_ids_json', 'result_count',
                'executed_at', 'actor', 'status',
            ],
            required_indices=[
                'idx_retrieval_log_query_hash',
                'idx_retrieval_log_scoring_version',
                'idx_retrieval_log_session_id',
                'idx_retrieval_log_executed_at',
                'idx_retrieval_log_status',
            ],
        )
        conn.close()

    def test_validate_schema_fails_if_status_missing(self, tmp_path):
        from memory.artifact_governance import validate_artifact_table_schema, GovernanceSchemaError
        import sqlite3
        db = str(tmp_path / 'no_status.db')
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT NOT NULL,
                executed_at TEXT NOT NULL,
                actor TEXT NOT NULL
            )
        """)
        conn.commit()
        with pytest.raises(GovernanceSchemaError, match="status"):
            validate_artifact_table_schema(
                conn, 'retrieval_log',
                required_columns=['id', 'query_hash', 'executed_at', 'actor', 'status'],
            )
        conn.close()

    def test_status_index_present_after_init(self, tmp_path):
        import sqlite3
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        conn = sqlite3.connect(db)
        indices = {row[1] for row in conn.execute('PRAGMA index_list(retrieval_log)')}
        conn.close()
        assert 'idx_retrieval_log_status' in indices


class TestMemorySchemaVersion:
    def test_init_db_sets_schema_version_16(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        import sqlite3
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_init_db_idempotent(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        service.init_db(db)
        import sqlite3
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_migrate_from_v2_adds_status_column(self, tmp_path):
        """Simulate a v2 DB without status column and verify migration adds it."""
        import sqlite3
        db = str(tmp_path / 'mem_v2.db')
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version VALUES (2);
            CREATE TABLE memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                evidence TEXT, source TEXT NOT NULL, confidence INTEGER NOT NULL,
                status TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                related_ids_json TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
                old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
            );
            CREATE TABLE memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL, relationship TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_id, target_id, relationship)
            );
            CREATE TABLE retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT NOT NULL,
                session_id TEXT,
                query_json TEXT NOT NULL,
                scoring_version TEXT NOT NULL,
                scoring_params_json TEXT NOT NULL,
                result_event_ids_json TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                executed_at TEXT NOT NULL,
                actor TEXT NOT NULL
            );
        """)
        conn.close()

        service.init_db(db)

        conn2 = sqlite3.connect(db)
        cols = {row[1] for row in conn2.execute('PRAGMA table_info(retrieval_log)')}
        version_row = conn2.execute('SELECT version FROM memory_schema_version').fetchone()
        conn2.close()
        assert 'status' in cols
        assert version_row[0] == 16

    def test_migrate_from_v2_backfills_existing_rows(self, tmp_path):
        """Rows inserted before migration must have status='active' after migration."""
        import sqlite3
        db = str(tmp_path / 'mem_v2_rows.db')
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version VALUES (2);
            CREATE TABLE memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                evidence TEXT, source TEXT NOT NULL, confidence INTEGER NOT NULL,
                status TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                related_ids_json TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
                old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
            );
            CREATE TABLE memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL, relationship TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_id, target_id, relationship)
            );
            CREATE TABLE retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT NOT NULL,
                session_id TEXT,
                query_json TEXT NOT NULL,
                scoring_version TEXT NOT NULL,
                scoring_params_json TEXT NOT NULL,
                result_event_ids_json TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                executed_at TEXT NOT NULL,
                actor TEXT NOT NULL
            );
            INSERT INTO retrieval_log
                (query_hash, query_json, scoring_version, scoring_params_json,
                 result_event_ids_json, result_count, executed_at, actor)
            VALUES
                ('abc123', '{}', '1.0.0', '{}', '[]', 0, '2026-01-01T00:00:00Z', 'tester');
        """)
        conn.close()

        service.init_db(db)

        conn2 = sqlite3.connect(db)
        row = conn2.execute("SELECT status FROM retrieval_log WHERE id=1").fetchone()
        conn2.close()
        assert row[0] == 'active'

    def test_migration_is_idempotent_on_v2_db(self, tmp_path):
        """Calling init_db twice on a migrated DB must not raise or corrupt data."""
        import sqlite3
        db = str(tmp_path / 'mem_v2_idem.db')
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version VALUES (2);
            CREATE TABLE memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                evidence TEXT, source TEXT NOT NULL, confidence INTEGER NOT NULL,
                status TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                related_ids_json TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
                old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
            );
            CREATE TABLE memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL, relationship TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_id, target_id, relationship)
            );
            CREATE TABLE retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT NOT NULL, session_id TEXT,
                query_json TEXT NOT NULL, scoring_version TEXT NOT NULL,
                scoring_params_json TEXT NOT NULL, result_event_ids_json TEXT NOT NULL,
                result_count INTEGER NOT NULL, executed_at TEXT NOT NULL,
                actor TEXT NOT NULL
            );
        """)
        conn.close()

        service.init_db(db)
        service.init_db(db)  # second call must not raise

        conn2 = sqlite3.connect(db)
        row = conn2.execute('SELECT version FROM memory_schema_version').fetchone()
        conn2.close()
        assert row[0] == 16


# ---------------------------------------------------------------------------
# Phase 3A: Schema v6 migration
# ---------------------------------------------------------------------------

_V5_DDL = """
    CREATE TABLE memory_schema_version (version INTEGER NOT NULL);
    INSERT INTO memory_schema_version VALUES (5);
    CREATE TABLE memory_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
        evidence TEXT, source TEXT NOT NULL, confidence INTEGER NOT NULL,
        status TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
        related_ids_json TEXT NOT NULL DEFAULT '[]',
        created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE memory_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
        old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
    );
    CREATE TABLE memory_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL, relationship TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE (source_id, target_id, relationship)
    );
    CREATE TABLE retrieval_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_hash TEXT NOT NULL, session_id TEXT, query_json TEXT NOT NULL,
        scoring_version TEXT NOT NULL, scoring_params_json TEXT NOT NULL,
        result_event_ids_json TEXT NOT NULL, result_count INTEGER NOT NULL,
        executed_at TEXT NOT NULL, actor TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
    );
    CREATE INDEX IF NOT EXISTS idx_retrieval_log_status ON retrieval_log(status);
    CREATE TABLE event_embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_event_id INTEGER NOT NULL, content_hash TEXT NOT NULL,
        vector_json TEXT NOT NULL, dimensions INTEGER NOT NULL,
        model_name TEXT NOT NULL, model_version TEXT NOT NULL,
        model_digest TEXT, provider_name TEXT NOT NULL,
        adapter_name TEXT NOT NULL, adapter_version TEXT NOT NULL,
        producer_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
        generated_at TEXT NOT NULL, invalidated_at TEXT, invalidated_reason TEXT,
        provenance_json TEXT NOT NULL
    );
    CREATE TABLE embedding_model_pins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pin_scope TEXT NOT NULL DEFAULT 'global',
        adapter_name TEXT NOT NULL, adapter_version TEXT NOT NULL,
        model_name TEXT NOT NULL, model_digest TEXT, dimensions INTEGER NOT NULL,
        embedding_visible_fields_version TEXT NOT NULL DEFAULT '1',
        pin_identity TEXT NOT NULL, provider_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', pinned_at TEXT NOT NULL,
        pinned_by TEXT NOT NULL, superseded_at TEXT, superseded_reason TEXT,
        notes TEXT
    );
"""


class TestSchemaV6Migration:
    def _v5_db(self, tmp_path) -> str:
        db = str(tmp_path / 'mem_v5.db')
        conn = sqlite3.connect(db)
        conn.executescript(_V5_DDL)
        conn.close()
        return db

    def test_schema_version_is_16(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_retrieval_log_has_semantic_mode(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(retrieval_log)')}
        conn.close()
        assert 'semantic_mode' in cols

    def test_retrieval_log_has_semantic_provenance_json(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(retrieval_log)')}
        conn.close()
        assert 'semantic_provenance_json' in cols

    def test_v5_db_migrates_to_v14(self, tmp_path):
        db = self._v5_db(tmp_path)
        service.init_db(db)
        conn = sqlite3.connect(db)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        cols = {row[1] for row in conn.execute('PRAGMA table_info(retrieval_log)')}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert version == 16
        assert 'semantic_mode' in cols
        assert 'semantic_provenance_json' in cols
        assert 'context_assembly_log' in tables

    def test_v6_migration_is_idempotent(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        service.init_db(db)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_v5_existing_rows_preserved_after_migration(self, tmp_path):
        db = self._v5_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO retrieval_log "
            "(query_hash, query_json, scoring_version, scoring_params_json, "
            " result_event_ids_json, result_count, executed_at, actor, status) "
            "VALUES ('abc', '{}', '1.0.0', '{}', '[]', 0, '2026-01-01T00:00:00Z', 'tester', 'active')"
        )
        conn.commit()
        conn.close()
        service.init_db(db)
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM retrieval_log').fetchone()[0]
        mode = conn.execute('SELECT semantic_mode FROM retrieval_log WHERE id=1').fetchone()[0]
        conn.close()
        assert count == 1
        assert mode == 'none'

    def test_semantic_mode_default_none_without_vector(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(), [], actor='test')
        entry = get_retrieval_log(db, log_id)
        assert entry.semantic_mode == 'none'
        assert entry.semantic_provenance_json is None


# ---------------------------------------------------------------------------
# Phase 3A: cosine similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_unit_vectors(self):
        assert abs(_cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9

    def test_orthogonal_vectors(self):
        assert abs(_cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_zero_vector_returns_zero(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_both_zero_vectors_returns_zero(self):
        assert _cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0

    def test_opposite_direction(self):
        assert abs(_cosine_similarity([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-9

    def test_symmetric(self):
        v1 = [0.3, 0.4, 0.5]
        v2 = [0.1, 0.2, 0.9]
        assert abs(_cosine_similarity(v1, v2) - _cosine_similarity(v2, v1)) < 1e-12

    def test_non_unit_vectors_same_as_normalized(self):
        v1 = [2.0, 0.0]
        v2 = [3.0, 0.0]
        assert abs(_cosine_similarity(v1, v2) - 1.0) < 1e-9

    def test_quantization_to_4_decimal_places(self, tmp_path):
        # _compute_semantic_ranks quantizes with round(sim, 4); verify round trips
        sim = _cosine_similarity([0.3, 0.4], [0.4, 0.3])
        quantized = round(sim, 4)
        assert len(str(quantized).split('.')[-1]) <= 4


# ---------------------------------------------------------------------------
# Phase 3A: _compute_semantic_ranks unit tests
# ---------------------------------------------------------------------------

def _insert_active_embedding(db: str, event_id: int, vector: list, content_hash: str) -> None:
    """Direct SQL insert of an active embedding row with a known vector for testing."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO event_embeddings "
        "(memory_event_id, content_hash, vector_json, dimensions, model_name, model_version, "
        " provider_name, adapter_name, adapter_version, producer_version, status, "
        " generated_at, provenance_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, content_hash, json.dumps(vector), len(vector),
         'stub-model', '1.0.0', 'stub', 'stub_embedding', '1.0.0',
         '1.0.0:1.0.0:stub-no-model-digest', 'active',
         '2026-01-01T00:00:00Z', '{}'),
    )
    conn.commit()
    conn.close()


class TestComputeSemanticRanks:
    def test_empty_events_returns_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ranks, scores, unembedded, stale, count = _compute_semantic_ranks([], [1.0, 0.0], db)
        assert ranks == {}
        assert scores == {}
        assert unembedded == []
        assert stale == []
        assert count == 0

    def test_event_with_no_active_embedding_is_unembedded(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        events = [service.get_memory_event(db, ev.id)[0]]
        ranks, scores, unembedded, stale, count = _compute_semantic_ranks(
            events, [1.0, 0.0, 0.0, 0.0], db
        )
        assert ev.id in unembedded
        assert ev.id not in scores
        assert count == 0

    def test_eligible_event_gets_rank_zero(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        event = service.get_memory_event(db, ev.id)[0]
        h = compute_content_hash(event.title, event.summary)
        _insert_active_embedding(db, ev.id, [1.0, 0.0, 0.0, 0.0], h)
        ranks, scores, unembedded, stale, count = _compute_semantic_ranks(
            [event], [1.0, 0.0, 0.0, 0.0], db
        )
        assert ranks[ev.id] == 0
        assert ev.id in scores
        assert count == 1

    def test_stale_embedding_goes_to_stale_list(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        event = service.get_memory_event(db, ev.id)[0]
        _insert_active_embedding(db, ev.id, [1.0, 0.0, 0.0, 0.0], 'wrong_hash')
        ranks, scores, unembedded, stale, count = _compute_semantic_ranks(
            [event], [1.0, 0.0, 0.0, 0.0], db
        )
        assert len(stale) == 1
        assert ev.id in unembedded
        assert count == 1

    def test_dimension_mismatch_excluded(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        event = service.get_memory_event(db, ev.id)[0]
        h = compute_content_hash(event.title, event.summary)
        _insert_active_embedding(db, ev.id, [1.0, 0.0, 0.0, 0.0], h)
        # Query vector with different dimensions
        ranks, scores, unembedded, stale, count = _compute_semantic_ranks(
            [event], [1.0, 0.0], db  # 2 dims vs 4 dims stored
        )
        assert ev.id in unembedded
        assert ev.id not in scores

    def test_rank_ordering_by_descending_similarity(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev1 = _add(db, title='Alpha', summary='first event')
        ev2 = _add(db, title='Beta', summary='second event')
        e1 = service.get_memory_event(db, ev1.id)[0]
        e2 = service.get_memory_event(db, ev2.id)[0]
        query_vector = [1.0, 0.0, 0.0, 0.0]
        # ev1 aligned with query (sim ≈ 1.0)
        _insert_active_embedding(db, ev1.id, [1.0, 0.0, 0.0, 0.0], compute_content_hash(e1.title, e1.summary))
        # ev2 orthogonal to query (sim ≈ 0.0)
        _insert_active_embedding(db, ev2.id, [0.0, 1.0, 0.0, 0.0], compute_content_hash(e2.title, e2.summary))
        ranks, scores, unembedded, stale, count = _compute_semantic_ranks(
            [e1, e2], query_vector, db
        )
        assert ranks[ev1.id] < ranks[ev2.id]
        assert unembedded == []

    def test_unembedded_events_get_rank_c(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev1 = _add(db, title='Embedded', summary='Has embedding')
        ev2 = _add(db, title='NoEmbed', summary='No embedding')
        e1 = service.get_memory_event(db, ev1.id)[0]
        e2 = service.get_memory_event(db, ev2.id)[0]
        query_vector = [1.0, 0.0, 0.0, 0.0]
        _insert_active_embedding(db, ev1.id, query_vector, compute_content_hash(e1.title, e1.summary))
        ranks, scores, unembedded, stale, count = _compute_semantic_ranks(
            [e1, e2], query_vector, db
        )
        C = len(scores)
        assert ranks[ev1.id] < C
        assert ranks[ev2.id] == C


# ---------------------------------------------------------------------------
# Phase 3A: semantic retrieve() integration
# ---------------------------------------------------------------------------

class TestSemanticRetrieval:
    def test_no_query_vector_all_semantic_rank_zero(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        _add(db)
        results = retrieve(db, RetrievalQuery(expand_related=False))
        assert all(s.semantic_rank == 0 for s in results)

    def test_composite_key_length_is_7(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        results = retrieve(db, RetrievalQuery(expand_related=False))
        assert len(results[0].composite_key) == 7

    def test_with_query_vector_assigns_rank_zero_to_embedded(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        event = service.get_memory_event(db, ev.id)[0]
        h = compute_content_hash(event.title, event.summary)
        query_vector = [1.0, 0.0, 0.0, 0.0]
        _insert_active_embedding(db, ev.id, query_vector, h)
        results = retrieve(db, RetrievalQuery(expand_related=False), query_vector=query_vector)
        assert results[0].semantic_rank == 0

    def test_events_without_embeddings_get_rank_c(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev1 = _add(db, title='Embedded', summary='Has embedding here')
        ev2 = _add(db, title='NoEmbed', summary='No embedding present')
        e1 = service.get_memory_event(db, ev1.id)[0]
        query_vector = [1.0, 0.0, 0.0, 0.0]
        _insert_active_embedding(db, ev1.id, query_vector, compute_content_hash(e1.title, e1.summary))
        results = retrieve(db, RetrievalQuery(expand_related=False), query_vector=query_vector)
        by_id = {s.event.id: s for s in results}
        # C=1: embedded event gets rank 0, unembedded gets rank 1
        assert by_id[ev1.id].semantic_rank == 0
        assert by_id[ev2.id].semantic_rank == 1

    def test_graceful_degradation_no_active_embeddings(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        _add(db)
        results = retrieve(db, RetrievalQuery(expand_related=False),
                           query_vector=[1.0, 0.0, 0.0, 0.0])
        # No active embeddings → C=0 → all events get rank C=0
        assert all(s.semantic_rank == 0 for s in results)

    def test_most_similar_event_gets_rank_zero(self, tmp_path):
        from memory.artifact_governance import compute_content_hash
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev1 = _add(db, title='Alpha', summary='first event here')
        ev2 = _add(db, title='Beta', summary='second event here')
        e1 = service.get_memory_event(db, ev1.id)[0]
        e2 = service.get_memory_event(db, ev2.id)[0]
        query_vector = [1.0, 0.0, 0.0, 0.0]
        # ev1 aligned with query
        _insert_active_embedding(db, ev1.id, [1.0, 0.0, 0.0, 0.0], compute_content_hash(e1.title, e1.summary))
        # ev2 orthogonal to query
        _insert_active_embedding(db, ev2.id, [0.0, 1.0, 0.0, 0.0], compute_content_hash(e2.title, e2.summary))
        results = retrieve(db, RetrievalQuery(expand_related=False), query_vector=query_vector)
        by_id = {s.event.id: s for s in results}
        assert by_id[ev1.id].semantic_rank < by_id[ev2.id].semantic_rank

    def test_existing_ordering_preserved_without_query_vector(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis', confidence=5)
        _add(db, event_type='governance_rule', confidence=1, status='accepted')
        results = retrieve(db, RetrievalQuery())
        assert results[0].event.event_type == 'governance_rule'


# ---------------------------------------------------------------------------
# Phase 3A: semantic provenance logging
# ---------------------------------------------------------------------------

class TestSemanticProvenance:
    def test_no_query_vector_semantic_mode_is_none(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(), [], actor='test')
        entry = get_retrieval_log(db, log_id)
        assert entry.semantic_mode == 'none'

    def test_with_query_vector_semantic_mode_is_vector(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(
            db, RetrievalQuery(), [], actor='test',
            query_vector=[1.0, 0.0, 0.0, 0.0],
        )
        entry = get_retrieval_log(db, log_id)
        assert entry.semantic_mode == 'vector'

    def test_no_query_vector_provenance_json_is_none(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(), [], actor='test')
        entry = get_retrieval_log(db, log_id)
        assert entry.semantic_provenance_json is None

    def test_with_query_vector_provenance_has_required_keys(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        meta = {
            'semantic_ranks': {1: 0},
            'semantic_scores': {1: 0.9999},
            'unembedded_event_ids': [],
            'stale_embedding_ids': [],
            'embedding_count_consulted': 1,
        }
        log_id = log_retrieval_query(
            db, RetrievalQuery(), [], actor='test',
            query_vector=[1.0, 0.0, 0.0, 0.0],
            semantic_meta=meta,
        )
        entry = get_retrieval_log(db, log_id)
        prov = json.loads(entry.semantic_provenance_json)
        required = {
            'query_vector_hash', 'query_vector_provenance', 'query_vector_dimensions',
            'pin_identity', 'pin_scope', 'embedding_count_consulted',
            'semantic_scores', 'semantic_ranks', 'unembedded_event_ids', 'stale_embedding_ids',
        }
        assert required <= set(prov.keys())

    def test_query_vector_hash_is_16_chars(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(
            db, RetrievalQuery(), [], actor='test',
            query_vector=[1.0, 0.0],
        )
        entry = get_retrieval_log(db, log_id)
        prov = json.loads(entry.semantic_provenance_json)
        assert len(prov['query_vector_hash']) == 16

    def test_query_vector_dimensions_recorded(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(
            db, RetrievalQuery(), [], actor='test',
            query_vector=[1.0, 0.0, 0.0],
        )
        entry = get_retrieval_log(db, log_id)
        prov = json.loads(entry.semantic_provenance_json)
        assert prov['query_vector_dimensions'] == 3

    def test_semantic_fields_in_to_dict(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(db, RetrievalQuery(), [], actor='test')
        entry = get_retrieval_log(db, log_id)
        d = entry.to_dict()
        assert 'semantic_mode' in d
        assert 'semantic_provenance_json' in d
        assert d['semantic_mode'] == 'none'
        assert d['semantic_provenance_json'] is None

    def test_retrieve_with_vector_logs_semantic_mode(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        retrieve(db, RetrievalQuery(expand_related=False),
                 query_vector=[1.0, 0.0, 0.0, 0.0],
                 log_retrieval=True, actor='agent')
        entries = list_retrieval_log(db)
        assert len(entries) == 1
        assert entries[0].semantic_mode == 'vector'
        assert entries[0].semantic_provenance_json is not None

    def test_query_vector_provenance_stored(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        prov_in = {'adapter_name': 'ollama-embedding', 'model_name': 'nomic-embed-text'}
        log_id = log_retrieval_query(
            db, RetrievalQuery(), [], actor='test',
            query_vector=[1.0, 0.0],
            query_vector_provenance=prov_in,
        )
        entry = get_retrieval_log(db, log_id)
        prov = json.loads(entry.semantic_provenance_json)
        assert prov['query_vector_provenance'] == prov_in

    def test_pin_identity_is_none_at_retrieval(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        log_id = log_retrieval_query(
            db, RetrievalQuery(), [], actor='test',
            query_vector=[1.0, 0.0],
        )
        entry = get_retrieval_log(db, log_id)
        prov = json.loads(entry.semantic_provenance_json)
        assert prov['pin_identity'] is None
        assert prov['pin_scope'] is None


# ---------------------------------------------------------------------------
# Phase 3A: CLI retrieve command
# ---------------------------------------------------------------------------

class TestCliRetrieve:
    def test_retrieve_empty_db(self, tmp_path, capsys):
        from memory.cli import main
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        main(['retrieve', '--db', db])
        output = json.loads(capsys.readouterr().out)
        assert output == []

    def test_retrieve_returns_events(self, tmp_path, capsys):
        from memory.cli import main
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        main(['retrieve', '--db', db])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert 'event_id' in output[0]
        assert 'semantic_rank' in output[0]

    def test_retrieve_with_query_vector(self, tmp_path, capsys):
        from memory.artifact_governance import compute_content_hash
        from memory.cli import main
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        event = service.get_memory_event(db, ev.id)[0]
        h = compute_content_hash(event.title, event.summary)
        _insert_active_embedding(db, ev.id, [1.0, 0.0, 0.0, 0.0], h)
        vec_json = json.dumps([1.0, 0.0, 0.0, 0.0])
        main(['retrieve', '--db', db, '--query-vector-json', vec_json])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]['semantic_rank'] == 0

    def test_retrieve_logs_when_flag_set(self, tmp_path, capsys):
        from memory.cli import main
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        main(['retrieve', '--db', db, '--log-retrieval'])
        capsys.readouterr()
        entries = list_retrieval_log(db)
        assert len(entries) == 1
