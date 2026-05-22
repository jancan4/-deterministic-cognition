import json
import pytest
from memory import service
from memory.models import (
    VALID_EVENT_TYPES, VALID_STATUSES, VALID_RELATIONSHIPS,
    CONFIDENCE_MIN, CONFIDENCE_MAX,
)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'test.db')
    service.init_db(path)
    return path


def _add(db, **kw):
    defaults = dict(
        event_type='hypothesis',
        title='Test title',
        summary='Test summary',
        source='test',
        confidence=3,
        status='proposed',
        created_by='tester',
    )
    defaults.update(kw)
    return service.add_memory_event(db, **defaults)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_memory_events_table(self, db):
        with service._connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert 'memory_events' in tables

    def test_creates_memory_revisions_table(self, db):
        with service._connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert 'memory_revisions' in tables

    def test_creates_memory_links_table(self, db):
        with service._connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert 'memory_links' in tables

    def test_idempotent(self, db):
        service.init_db(db)  # second call must not raise
        with service._connect(db) as conn:
            assert conn.execute('SELECT COUNT(*) FROM memory_events').fetchone()[0] == 0


# ---------------------------------------------------------------------------
# add_memory_event
# ---------------------------------------------------------------------------

class TestAddMemoryEvent:
    def test_basic_add(self, db):
        ev = _add(db)
        assert ev.id == 1
        assert ev.event_type == 'hypothesis'
        assert ev.title == 'Test title'
        assert ev.summary == 'Test summary'
        assert ev.source == 'test'
        assert ev.confidence == 3
        assert ev.status == 'proposed'
        assert ev.created_by == 'tester'
        assert ev.version == 1
        assert ev.tags == []
        assert ev.related_ids == []
        assert ev.evidence is None

    def test_with_tags(self, db):
        ev = _add(db, tags=['governance', 'fx'])
        assert sorted(ev.tags) == ['fx', 'governance']

    def test_tags_stored_sorted(self, db):
        ev = _add(db, tags=['z', 'a', 'm'])
        assert ev.tags == ['a', 'm', 'z']

    def test_with_related_ids(self, db):
        ev = _add(db, related_ids=[3, 1, 2])
        assert ev.related_ids == [1, 2, 3]

    def test_with_evidence(self, db):
        ev = _add(db, evidence='Run 109 confirms stable_neutral')
        assert ev.evidence == 'Run 109 confirms stable_neutral'

    def test_timestamps_set(self, db):
        ev = _add(db)
        assert ev.created_at.endswith('Z')
        assert ev.updated_at == ev.created_at

    def test_ids_autoincrement(self, db):
        e1 = _add(db)
        e2 = _add(db)
        assert e2.id == e1.id + 1

    # --- event_type validation ---

    def test_invalid_event_type_rejected(self, db):
        with pytest.raises(service.ValidationError, match="Invalid event_type"):
            _add(db, event_type='observation')

    def test_invalid_event_type_belief_rejected(self, db):
        with pytest.raises(service.ValidationError, match="Invalid event_type"):
            _add(db, event_type='belief')

    def test_invalid_event_type_fact_rejected(self, db):
        with pytest.raises(service.ValidationError, match="Invalid event_type"):
            _add(db, event_type='fact')

    def test_invalid_event_type_signal_rejected(self, db):
        with pytest.raises(service.ValidationError, match="Invalid event_type"):
            _add(db, event_type='signal')

    @pytest.mark.parametrize('et', VALID_EVENT_TYPES)
    def test_all_valid_event_types_accepted(self, db, et):
        ev = _add(db, event_type=et)
        assert ev.event_type == et

    # --- status validation ---

    def test_invalid_status_rejected(self, db):
        with pytest.raises(service.ValidationError, match="Invalid status"):
            _add(db, status='active_old')

    @pytest.mark.parametrize('s', VALID_STATUSES)
    def test_all_valid_statuses_accepted(self, db, s):
        ev = _add(db, status=s)
        assert ev.status == s

    # --- confidence validation ---

    def test_confidence_below_1_rejected(self, db):
        with pytest.raises(service.ValidationError, match="confidence"):
            _add(db, confidence=0)

    def test_confidence_above_5_rejected(self, db):
        with pytest.raises(service.ValidationError, match="confidence"):
            _add(db, confidence=6)

    def test_confidence_negative_rejected(self, db):
        with pytest.raises(service.ValidationError, match="confidence"):
            _add(db, confidence=-1)

    def test_confidence_string_rejected(self, db):
        with pytest.raises(service.ValidationError, match="confidence"):
            _add(db, confidence='high')

    def test_confidence_string_number_rejected(self, db):
        with pytest.raises(service.ValidationError, match="confidence"):
            _add(db, confidence='3')

    def test_confidence_float_rejected(self, db):
        with pytest.raises(service.ValidationError, match="confidence"):
            _add(db, confidence=3.0)

    def test_confidence_bool_rejected(self, db):
        with pytest.raises(service.ValidationError, match="confidence"):
            _add(db, confidence=True)

    @pytest.mark.parametrize('c', range(CONFIDENCE_MIN, CONFIDENCE_MAX + 1))
    def test_all_valid_confidence_values(self, db, c):
        ev = _add(db, confidence=c)
        assert ev.confidence == c

    # --- required field validation ---

    def test_empty_title_rejected(self, db):
        with pytest.raises(service.ValidationError, match="title"):
            _add(db, title='')

    def test_empty_summary_rejected(self, db):
        with pytest.raises(service.ValidationError, match="summary"):
            _add(db, summary='')

    def test_empty_source_rejected(self, db):
        with pytest.raises(service.ValidationError, match="source"):
            _add(db, source='')

    def test_empty_created_by_rejected(self, db):
        with pytest.raises(service.ValidationError, match="created_by"):
            _add(db, created_by='')

    # --- JSON serialization ---

    def test_tags_serialized_as_json_list(self, db):
        ev = _add(db, tags=['a', 'b'])
        with service._connect(db) as conn:
            row = conn.execute('SELECT tags_json FROM memory_events WHERE id = ?', (ev.id,)).fetchone()
        parsed = json.loads(row['tags_json'])
        assert isinstance(parsed, list)

    def test_related_ids_serialized_as_json_list(self, db):
        ev = _add(db, related_ids=[1, 2])
        with service._connect(db) as conn:
            row = conn.execute('SELECT related_ids_json FROM memory_events WHERE id = ?', (ev.id,)).fetchone()
        parsed = json.loads(row['related_ids_json'])
        assert isinstance(parsed, list)


# ---------------------------------------------------------------------------
# list_memory_events
# ---------------------------------------------------------------------------

class TestListMemoryEvents:
    def test_empty_db(self, db):
        assert service.list_memory_events(db) == []

    def test_filter_by_type(self, db):
        _add(db, event_type='hypothesis')
        _add(db, event_type='incident')
        result = service.list_memory_events(db, event_type='hypothesis')
        assert all(e.event_type == 'hypothesis' for e in result)
        assert len(result) == 1

    def test_filter_by_status(self, db):
        _add(db, status='proposed')
        _add(db, status='accepted')
        result = service.list_memory_events(db, status='accepted')
        assert all(e.status == 'accepted' for e in result)
        assert len(result) == 1

    def test_filter_by_tag(self, db):
        _add(db, tags=['governance'])
        _add(db, tags=['fx'])
        result = service.list_memory_events(db, tag='governance')
        assert len(result) == 1
        assert 'governance' in result[0].tags

    def test_invalid_type_raises(self, db):
        with pytest.raises(service.ValidationError):
            service.list_memory_events(db, event_type='bogus')

    def test_invalid_status_raises(self, db):
        with pytest.raises(service.ValidationError):
            service.list_memory_events(db, status='bogus')


# ---------------------------------------------------------------------------
# search_memory_events
# ---------------------------------------------------------------------------

class TestSearchMemoryEvents:
    def test_requires_query_or_tag(self, db):
        with pytest.raises(service.ValidationError, match="At least one"):
            service.search_memory_events(db)

    def test_search_by_query_in_title(self, db):
        _add(db, title='BoC rate hold decision')
        result = service.search_memory_events(db, query='BoC')
        assert len(result) == 1

    def test_search_by_query_in_summary(self, db):
        _add(db, summary='Replayability requires deterministic ordering')
        result = service.search_memory_events(db, query='replayability')
        assert len(result) == 1

    def test_search_by_query_in_source(self, db):
        _add(db, source='EXTERNAL_MEMORY_LAYER_IMPLEMENTATION_BRIEF.md')
        result = service.search_memory_events(db, query='BRIEF')
        assert len(result) == 1

    def test_search_by_query_in_evidence(self, db):
        _add(db, evidence='Run 109 stable_neutral confirmed')
        result = service.search_memory_events(db, query='stable_neutral')
        assert len(result) == 1

    def test_search_by_tag(self, db):
        _add(db, tags=['governance', 'replayability'])
        result = service.search_memory_events(db, tag='governance')
        assert len(result) == 1

    def test_search_no_match(self, db):
        _add(db)
        result = service.search_memory_events(db, query='xyz_absent')
        assert result == []

    def test_search_combined_query_and_tag(self, db):
        _add(db, title='governance note', tags=['governance'])
        _add(db, title='governance note no tag')
        result = service.search_memory_events(db, query='governance', tag='governance')
        assert len(result) == 1
        assert 'governance' in result[0].tags


# ---------------------------------------------------------------------------
# get_memory_event
# ---------------------------------------------------------------------------

class TestGetMemoryEvent:
    def test_returns_event(self, db):
        ev = _add(db)
        result, revisions, links = service.get_memory_event(db, ev.id)
        assert result.id == ev.id
        assert revisions == []
        assert links == []

    def test_not_found_raises(self, db):
        with pytest.raises(service.NotFoundError):
            service.get_memory_event(db, 9999)

    def test_includes_revisions(self, db):
        ev = _add(db)
        service.update_status(db, ev.id, 'accepted', reason='approved', created_by='user')
        _, revisions, _ = service.get_memory_event(db, ev.id)
        assert len(revisions) == 1

    def test_includes_links(self, db):
        e1 = _add(db)
        e2 = _add(db)
        service.link_memory_events(db, e1.id, e2.id, 'supports')
        _, _, links = service.get_memory_event(db, e1.id)
        assert len(links) == 1


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------

class TestUpdateStatus:
    def test_status_changes(self, db):
        ev = _add(db, status='proposed')
        updated = service.update_status(db, ev.id, 'accepted', reason='approved', created_by='user')
        assert updated.status == 'accepted'

    def test_version_incremented(self, db):
        ev = _add(db)
        assert ev.version == 1
        updated = service.update_status(db, ev.id, 'accepted', reason='ok', created_by='user')
        assert updated.version == 2

    def test_creates_revision(self, db):
        ev = _add(db, status='proposed')
        service.update_status(db, ev.id, 'superseded', reason='replaced', created_by='tester')
        _, revisions, _ = service.get_memory_event(db, ev.id)
        assert len(revisions) == 1
        rev = revisions[0]
        assert rev.memory_id == ev.id
        assert rev.reason == 'replaced'
        assert rev.created_by == 'tester'

    def test_revision_captures_old_and_new_status(self, db):
        ev = _add(db, status='proposed')
        service.update_status(db, ev.id, 'accepted', reason='ok', created_by='user')
        _, revisions, _ = service.get_memory_event(db, ev.id)
        old_val = json.loads(revisions[0].old_value_json)
        new_val = json.loads(revisions[0].new_value_json)
        assert old_val['status'] == 'proposed'
        assert new_val['status'] == 'accepted'

    def test_revision_captures_version(self, db):
        ev = _add(db, status='proposed')
        service.update_status(db, ev.id, 'accepted', reason='ok', created_by='user')
        _, revisions, _ = service.get_memory_event(db, ev.id)
        old_val = json.loads(revisions[0].old_value_json)
        new_val = json.loads(revisions[0].new_value_json)
        assert old_val['version'] == 1
        assert new_val['version'] == 2

    def test_multiple_revisions_all_recorded(self, db):
        ev = _add(db, status='proposed')
        service.update_status(db, ev.id, 'active', reason='r1', created_by='u')
        service.update_status(db, ev.id, 'superseded', reason='r2', created_by='u')
        service.update_status(db, ev.id, 'deprecated', reason='r3', created_by='u')
        _, revisions, _ = service.get_memory_event(db, ev.id)
        assert len(revisions) == 3
        assert [json.loads(r.new_value_json)['status'] for r in revisions] == [
            'active', 'superseded', 'deprecated'
        ]

    def test_invalid_status_rejected(self, db):
        ev = _add(db)
        with pytest.raises(service.ValidationError, match="Invalid status"):
            service.update_status(db, ev.id, 'deleted', reason='x', created_by='u')

    def test_not_found_raises(self, db):
        with pytest.raises(service.NotFoundError):
            service.update_status(db, 9999, 'accepted', reason='x', created_by='u')

    def test_empty_reason_rejected(self, db):
        ev = _add(db)
        with pytest.raises(service.ValidationError, match="reason"):
            service.update_status(db, ev.id, 'accepted', reason='', created_by='u')

    def test_updated_at_changes(self, db):
        ev = _add(db)
        updated = service.update_status(db, ev.id, 'accepted', reason='ok', created_by='u')
        assert updated.updated_at.endswith('Z')

    @pytest.mark.parametrize('s', VALID_STATUSES)
    def test_all_valid_target_statuses(self, db, s):
        ev = _add(db, status='proposed')
        updated = service.update_status(db, ev.id, s, reason='test', created_by='u')
        assert updated.status == s


# ---------------------------------------------------------------------------
# link_memory_events
# ---------------------------------------------------------------------------

class TestLinkMemoryEvents:
    def test_basic_link(self, db):
        e1 = _add(db)
        e2 = _add(db)
        lnk = service.link_memory_events(db, e1.id, e2.id, 'supports')
        assert lnk.source_id == e1.id
        assert lnk.target_id == e2.id
        assert lnk.relationship == 'supports'

    @pytest.mark.parametrize('rel', [r for r in VALID_RELATIONSHIPS if r != 'contradicts'])
    def test_all_valid_relationships(self, db, rel):
        e1 = _add(db)
        e2 = _add(db)
        lnk = service.link_memory_events(db, e1.id, e2.id, rel)
        assert lnk.relationship == rel

    def test_invalid_relationship_rejected(self, db):
        e1 = _add(db)
        e2 = _add(db)
        with pytest.raises(service.ValidationError, match="Invalid relationship"):
            service.link_memory_events(db, e1.id, e2.id, 'relation')

    def test_duplicate_link_rejected(self, db):
        e1 = _add(db)
        e2 = _add(db)
        service.link_memory_events(db, e1.id, e2.id, 'supports')
        with pytest.raises(service.ValidationError, match="already exists"):
            service.link_memory_events(db, e1.id, e2.id, 'supports')

    def test_same_pair_different_relationships_allowed(self, db):
        e1 = _add(db)
        e2 = _add(db)
        service.link_memory_events(db, e1.id, e2.id, 'supports')
        lnk2 = service.link_memory_events(db, e1.id, e2.id, 'refines')
        assert lnk2.relationship == 'refines'

    def test_contradicts_relationship_rejected_by_generic_api(self, db):
        e1 = _add(db)
        e2 = _add(db)
        with pytest.raises(service.ValidationError, match="create_contradiction_link"):
            service.link_memory_events(db, e1.id, e2.id, 'contradicts')

    def test_source_not_found(self, db):
        e = _add(db)
        with pytest.raises(service.NotFoundError):
            service.link_memory_events(db, 9999, e.id, 'supports')

    def test_target_not_found(self, db):
        e = _add(db)
        with pytest.raises(service.NotFoundError):
            service.link_memory_events(db, e.id, 9999, 'supports')


# ---------------------------------------------------------------------------
# export_memory
# ---------------------------------------------------------------------------

class TestExportMemory:
    def test_top_level_keys(self, db):
        payload = service.export_memory(db)
        assert set(payload.keys()) == {'schema_version', 'memory_events', 'memory_revisions', 'memory_links'}

    def test_schema_version(self, db):
        assert service.export_memory(db)['schema_version'] == 1

    def test_empty_db(self, db):
        payload = service.export_memory(db)
        assert payload['memory_events'] == []
        assert payload['memory_revisions'] == []
        assert payload['memory_links'] == []

    def test_includes_all_three_tables(self, db):
        e1 = _add(db)
        e2 = _add(db)
        service.update_status(db, e1.id, 'accepted', reason='ok', created_by='u')
        service.link_memory_events(db, e1.id, e2.id, 'supports')
        payload = service.export_memory(db)
        assert len(payload['memory_events']) == 2
        assert len(payload['memory_revisions']) == 1
        assert len(payload['memory_links']) == 1

    def test_deterministic_ordering(self, db):
        for _ in range(5):
            _add(db)
        p1 = service.export_memory(db)
        p2 = service.export_memory(db)
        assert p1 == p2

    def test_events_ordered_by_id(self, db):
        for _ in range(3):
            _add(db)
        payload = service.export_memory(db)
        ids = [e['id'] for e in payload['memory_events']]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# review_memory
# ---------------------------------------------------------------------------

class TestReviewMemory:
    def test_default_shows_proposed(self, db):
        _add(db, status='proposed')
        result = service.review_memory(db)
        assert any(e.status == 'proposed' for e in result)

    def test_default_shows_unresolved(self, db):
        _add(db, status='unresolved')
        result = service.review_memory(db)
        assert any(e.status == 'unresolved' for e in result)

    def test_default_shows_active(self, db):
        _add(db, status='active')
        result = service.review_memory(db)
        assert any(e.status == 'active' for e in result)

    def test_default_excludes_accepted(self, db):
        _add(db, status='accepted')
        result = service.review_memory(db)
        assert all(e.status != 'accepted' for e in result)

    def test_status_filter(self, db):
        _add(db, status='proposed')
        _add(db, status='unresolved')
        result = service.review_memory(db, status='proposed')
        assert all(e.status == 'proposed' for e in result)

    def test_type_filter(self, db):
        _add(db, event_type='hypothesis', status='proposed')
        _add(db, event_type='incident', status='proposed')
        result = service.review_memory(db, event_type='hypothesis')
        assert all(e.event_type == 'hypothesis' for e in result)

    def test_empty_when_none_match(self, db):
        _add(db, status='accepted')
        result = service.review_memory(db)
        assert result == []
