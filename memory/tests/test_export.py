import json
import pytest
from memory import service
from memory import export as exporter


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'test.db')
    service.init_db(path)
    return path


def _add(db, **kw):
    defaults = dict(
        event_type='hypothesis',
        title='Title',
        summary='Summary',
        source='test',
        confidence=3,
        status='proposed',
        created_by='tester',
    )
    defaults.update(kw)
    return service.add_memory_event(db, **defaults)


@pytest.fixture
def populated_db(db):
    e1 = _add(db, event_type='architecture_decision', title='Use SQLite',
               summary='Chose SQLite for determinism', source='brief', confidence=5,
               status='accepted', created_by='user+gpt', tags=['db', 'determinism'])
    e2 = _add(db, event_type='open_question', title='Decay model',
               summary='How should old events decay?', source='review', confidence=2,
               status='unresolved', created_by='user')
    service.update_status(db, e1.id, 'active', reason='deployed', created_by='user')
    service.link_memory_events(db, e1.id, e2.id, 'related_to')
    return db


class TestExportToFile:
    def test_creates_file(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        exporter.export_to_file(populated_db, out)
        assert (tmp_path / 'export.json').exists()

    def test_file_is_valid_json(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        exporter.export_to_file(populated_db, out)
        with open(out) as fh:
            data = json.load(fh)
        assert 'memory_events' in data

    def test_file_ends_with_newline(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        exporter.export_to_file(populated_db, out)
        with open(out) as fh:
            content = fh.read()
        assert content.endswith('\n')

    def test_return_value_matches_file(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        returned = exporter.export_to_file(populated_db, out)
        with open(out) as fh:
            on_disk = json.load(fh)
        assert returned == on_disk

    def test_includes_memory_events(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        assert len(payload['memory_events']) == 2

    def test_includes_memory_revisions(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        assert len(payload['memory_revisions']) == 1

    def test_includes_memory_links(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        assert len(payload['memory_links']) == 1

    def test_events_ordered_by_id(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        ids = [e['id'] for e in payload['memory_events']]
        assert ids == sorted(ids)

    def test_revisions_ordered_by_id(self, db, tmp_path):
        e = _add(db)
        service.update_status(db, e.id, 'active', reason='r1', created_by='u')
        service.update_status(db, e.id, 'archived', reason='r2', created_by='u')
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(db, out)
        ids = [r['id'] for r in payload['memory_revisions']]
        assert ids == sorted(ids)

    def test_links_ordered_by_id(self, db, tmp_path):
        e1 = _add(db)
        e2 = _add(db)
        e3 = _add(db)
        service.link_memory_events(db, e1.id, e2.id, 'supports')
        service.link_memory_events(db, e1.id, e3.id, 'related_to')
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(db, out)
        ids = [lnk['id'] for lnk in payload['memory_links']]
        assert ids == sorted(ids)

    def test_json_keys_sorted(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        exporter.export_to_file(populated_db, out)
        with open(out) as fh:
            content = fh.read()
        # In sorted-key JSON, 'confidence' < 'created_at' < 'created_by'
        pos_conf = content.find('"confidence"')
        pos_cat = content.find('"created_at"')
        assert pos_conf < pos_cat

    def test_deterministic_repeated_calls(self, populated_db, tmp_path):
        out1 = str(tmp_path / 'a.json')
        out2 = str(tmp_path / 'b.json')
        exporter.export_to_file(populated_db, out1)
        exporter.export_to_file(populated_db, out2)
        with open(out1) as f1, open(out2) as f2:
            assert f1.read() == f2.read()

    def test_empty_db(self, db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(db, out)
        assert payload['memory_events'] == []
        assert payload['memory_revisions'] == []
        assert payload['memory_links'] == []

    def test_event_fields_complete(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        e = payload['memory_events'][0]
        for key in ('id', 'event_type', 'title', 'summary', 'evidence', 'source',
                    'confidence', 'status', 'tags', 'related_ids',
                    'created_by', 'created_at', 'updated_at', 'version'):
            assert key in e

    def test_revision_fields_complete(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        r = payload['memory_revisions'][0]
        for key in ('id', 'memory_id', 'old_value', 'new_value', 'reason',
                    'created_at', 'created_by'):
            assert key in r

    def test_link_fields_complete(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        lnk = payload['memory_links'][0]
        for key in ('id', 'source_id', 'target_id', 'relationship', 'created_at'):
            assert key in lnk

    def test_tags_are_list_in_export(self, populated_db, tmp_path):
        out = str(tmp_path / 'export.json')
        payload = exporter.export_to_file(populated_db, out)
        e = next(e for e in payload['memory_events'] if e['tags'])
        assert isinstance(e['tags'], list)
