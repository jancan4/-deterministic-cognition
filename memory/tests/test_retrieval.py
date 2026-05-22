import json

import pytest
from memory import service
from memory.retrieval import (
    DOCTRINE_PRIORITY,
    RETRIEVAL_SCORING_VERSION,
    RetrievalLogEntry,
    RetrievalQuery,
    ScoredEvent,
    _canonical_query_dict,
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
    def test_init_db_sets_schema_version_5(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        import sqlite3
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 5

    def test_init_db_idempotent(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        service.init_db(db)
        import sqlite3
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 5

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
        assert version_row[0] == 5

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
        assert row[0] == 5
