import json
import pytest
from memory import service
from memory.cli import main
from memory.embedding_pins import create_pin
from memory.embeddings import embed_event
from models.embedding_adapter import StubEmbeddingAdapter


def run(args, db_path):
    # Match brief's style: cmd --db PATH [options...]
    main([args[0], '--db', db_path] + args[1:])


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
# init
# ---------------------------------------------------------------------------

class TestCliInit:
    def test_creates_tables(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        run(['init'], db)
        with service._connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert {'memory_events', 'memory_revisions', 'memory_links'}.issubset(tables)

    def test_prints_confirmation(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        run(['init'], db)
        assert 'Initialized' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

class TestCliAdd:
    def _base_args(self):
        return [
            'add',
            '--type', 'architecture_decision',
            '--title', 'External memory uses event lineage',
            '--summary', 'Memory stores structured governed events, not raw chat.',
            '--source', 'EXTERNAL_MEMORY_LAYER_IMPLEMENTATION_BRIEF.md',
            '--confidence', '5',
            '--status', 'accepted',
            '--created-by', 'user+gpt',
        ]

    def test_add_outputs_json(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        run(self._base_args(), db)
        data = json.loads(capsys.readouterr().out)
        assert data['event_type'] == 'architecture_decision'
        assert data['confidence'] == 5
        assert data['status'] == 'accepted'
        assert data['id'] == 1
        assert data['version'] == 1

    def test_add_with_tags(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        run(self._base_args() + ['--tags', 'memory,event-sourcing,replayability'], db)
        data = json.loads(capsys.readouterr().out)
        assert sorted(data['tags']) == sorted(['memory', 'event-sourcing', 'replayability'])

    def test_add_with_evidence(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        run(self._base_args() + ['--evidence', 'Run 109 confirms this'], db)
        data = json.loads(capsys.readouterr().out)
        assert data['evidence'] == 'Run 109 confirms this'

    def test_invalid_type_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc:
            run(['add', '--type', 'observation', '--title', 't', '--summary', 's',
                 '--source', 'x', '--confidence', '3', '--status', 'proposed',
                 '--created-by', 'u'], db)
        assert exc.value.code != 0

    def test_invalid_confidence_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc:
            run(['add', '--type', 'hypothesis', '--title', 't', '--summary', 's',
                 '--source', 'x', '--confidence', '6', '--status', 'proposed',
                 '--created-by', 'u'], db)
        assert exc.value.code != 0

    def test_invalid_status_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc:
            run(['add', '--type', 'hypothesis', '--title', 't', '--summary', 's',
                 '--source', 'x', '--confidence', '3', '--status', 'deleted',
                 '--created-by', 'u'], db)
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestCliList:
    def test_empty_shows_message(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        run(['list'], db)
        assert 'No memory events' in capsys.readouterr().out

    def test_shows_event(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, title='Important decision')
        run(['list'], db)
        assert 'Important decision' in capsys.readouterr().out

    def test_filter_by_type(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis', title='a hypothesis')
        _add(db, event_type='incident', title='an incident')
        run(['list', '--type', 'hypothesis'], db)
        out = capsys.readouterr().out
        assert 'a hypothesis' in out
        assert 'an incident' not in out

    def test_filter_by_status(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed', title='proposed one')
        _add(db, status='accepted', title='accepted one')
        run(['list', '--status', 'accepted'], db)
        out = capsys.readouterr().out
        assert 'accepted one' in out
        assert 'proposed one' not in out

    def test_filter_by_tag(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, tags=['governance'], title='has tag')
        _add(db, title='no tag')
        run(['list', '--tag', 'governance'], db)
        out = capsys.readouterr().out
        assert 'has tag' in out
        assert 'no tag' not in out


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestCliSearch:
    def test_search_by_query(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, title='replayability is key')
        run(['search', '--query', 'replayability'], db)
        assert 'replayability' in capsys.readouterr().out

    def test_search_by_tag(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, tags=['governance'], title='governance event')
        run(['search', '--tag', 'governance'], db)
        assert 'governance event' in capsys.readouterr().out

    def test_no_query_or_tag_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc:
            run(['search'], db)
        assert exc.value.code != 0

    def test_no_match(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        run(['search', '--query', 'xyz_absent'], db)
        assert 'No results' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

class TestCliShow:
    def test_show_outputs_json(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        run(['show', '--id', str(ev.id)], db)
        data = json.loads(capsys.readouterr().out)
        assert data['event']['id'] == ev.id
        assert 'revisions' in data
        assert 'links' in data
        assert 'revision_count' in data

    def test_show_includes_revisions(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        service.update_status(db, ev.id, 'accepted', reason='ok', created_by='u')
        run(['show', '--id', str(ev.id)], db)
        data = json.loads(capsys.readouterr().out)
        assert data['revision_count'] == 1
        assert len(data['revisions']) == 1

    def test_show_missing_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc:
            run(['show', '--id', '9999'], db)
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# update-status
# ---------------------------------------------------------------------------

class TestCliUpdateStatus:
    def test_updates_status(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db, status='proposed')
        run(['update-status', '--id', str(ev.id), '--status', 'superseded',
             '--reason', 'Testing revision tracking', '--created-by', 'user'], db)
        data = json.loads(capsys.readouterr().out)
        assert data['status'] == 'superseded'

    def test_increments_version(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        run(['update-status', '--id', str(ev.id), '--status', 'accepted',
             '--reason', 'ok', '--created-by', 'u'], db)
        data = json.loads(capsys.readouterr().out)
        assert data['version'] == 2

    def test_creates_revision(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        run(['update-status', '--id', str(ev.id), '--status', 'accepted',
             '--reason', 'validated', '--created-by', 'user'], db)
        capsys.readouterr()
        _, revisions, _ = service.get_memory_event(db, ev.id)
        assert len(revisions) == 1
        assert revisions[0].reason == 'validated'

    def test_invalid_status_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        with pytest.raises(SystemExit):
            run(['update-status', '--id', str(ev.id), '--status', 'deleted',
                 '--reason', 'x', '--created-by', 'u'], db)

    def test_missing_id_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc:
            run(['update-status', '--id', '9999', '--status', 'accepted',
                 '--reason', 'x', '--created-by', 'u'], db)
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------

class TestCliLink:
    def test_creates_link(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db)
        e2 = _add(db)
        run(['link', '--source-id', str(e1.id), '--target-id', str(e2.id),
             '--relationship', 'supports'], db)
        data = json.loads(capsys.readouterr().out)
        assert data['source_id'] == e1.id
        assert data['target_id'] == e2.id
        assert data['relationship'] == 'supports'

    def test_invalid_relationship_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db)
        e2 = _add(db)
        with pytest.raises(SystemExit):
            run(['link', '--source-id', str(e1.id), '--target-id', str(e2.id),
                 '--relationship', 'relation'], db)

    def test_missing_source_exits(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e = _add(db)
        with pytest.raises(SystemExit) as exc:
            run(['link', '--source-id', '9999', '--target-id', str(e.id),
                 '--relationship', 'supports'], db)
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

class TestCliExport:
    def test_export_writes_file(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='architecture_decision', title='Use SQLite',
             summary='Deterministic', source='brief', confidence=5,
             status='accepted', created_by='user+gpt')
        out = str(tmp_path / 'memory_export.json')
        run(['export', '--out', out], db)
        capsys.readouterr()
        with open(out) as fh:
            data = json.load(fh)
        assert len(data['memory_events']) == 1

    def test_export_prints_confirmation(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        out = str(tmp_path / 'out.json')
        run(['export', '--out', out], db)
        assert 'Exported' in capsys.readouterr().out

    def test_export_deterministic(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        out1 = str(tmp_path / 'a.json')
        out2 = str(tmp_path / 'b.json')
        run(['export', '--out', out1], db)
        run(['export', '--out', out2], db)
        capsys.readouterr()
        with open(out1) as f1, open(out2) as f2:
            assert f1.read() == f2.read()


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

class TestCliReview:
    def test_no_pending(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        run(['review'], db)
        assert 'No memory events pending review' in capsys.readouterr().out

    def test_shows_proposed(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed', title='needs review')
        run(['review'], db)
        assert 'needs review' in capsys.readouterr().out

    def test_hides_accepted(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted', title='done')
        run(['review'], db)
        assert 'done' not in capsys.readouterr().out

    def test_filter_by_status(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed', title='proposed event')
        _add(db, status='unresolved', title='unresolved event')
        run(['review', '--status', 'proposed'], db)
        out = capsys.readouterr().out
        assert 'proposed event' in out
        assert 'unresolved event' not in out

    def test_filter_by_type(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='hypothesis', status='proposed', title='a hypothesis')
        _add(db, event_type='incident', status='proposed', title='an incident')
        run(['review', '--type', 'hypothesis'], db)
        out = capsys.readouterr().out
        assert 'a hypothesis' in out
        assert 'an incident' not in out


# ---------------------------------------------------------------------------
# promote-embedding
# ---------------------------------------------------------------------------

def _embed(db, event, dimensions=4):
    adapter = StubEmbeddingAdapter(dimensions=dimensions)
    create_pin(
        db,
        adapter_name=adapter.adapter_name,
        adapter_version=adapter.adapter_version,
        model_name=adapter.model_name,
        model_digest=adapter.model_digest,
        dimensions=adapter.dimensions,
        provider_name=adapter.provider_name,
        pinned_by='test-operator',
    )
    return embed_event(db, event, adapter)


class TestCliPromoteEmbedding:
    def test_promote_success_exits_zero(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        event = _add(db)
        row_id = _embed(db, event)
        run(['promote-embedding', '--id', str(row_id),
             '--reason', 'model validated', '--operator', 'quant'], db)
        # No SystemExit means success.

    def test_promote_success_outputs_json(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        event = _add(db)
        row_id = _embed(db, event)
        run(['promote-embedding', '--id', str(row_id),
             '--reason', 'ok', '--operator', 'quant'], db)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload['id'] == row_id
        assert payload['status'] == 'active'

    def test_promote_output_contains_audit_in_provenance(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        event = _add(db)
        row_id = _embed(db, event)
        run(['promote-embedding', '--id', str(row_id),
             '--reason', 'risk cleared', '--operator', 'risk-engine'], db)
        payload = json.loads(capsys.readouterr().out)
        prov = json.loads(payload['provenance_json'])
        assert prov['promotion']['operator'] == 'risk-engine'
        assert prov['promotion']['reason'] == 'risk cleared'

    def test_promote_unknown_id_exits_nonzero(self, tmp_path, capsys):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc_info:
            run(['promote-embedding', '--id', '9999',
                 '--reason', 'ok', '--operator', 'quant'], db)
        assert exc_info.value.code != 0

    def test_promote_missing_reason_exits_nonzero(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc_info:
            run(['promote-embedding', '--id', '1', '--operator', 'quant'], db)
        assert exc_info.value.code != 0

    def test_promote_missing_operator_exits_nonzero(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        with pytest.raises(SystemExit) as exc_info:
            run(['promote-embedding', '--id', '1', '--reason', 'ok'], db)
        assert exc_info.value.code != 0

    def test_promote_governance_error_exits_nonzero(self, tmp_path, capsys):
        """Promoting an already-active embedding must exit nonzero."""
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        event = _add(db)
        row_id = _embed(db, event)
        run(['promote-embedding', '--id', str(row_id),
             '--reason', 'first', '--operator', 'quant'], db)
        capsys.readouterr()  # clear output
        with pytest.raises(SystemExit) as exc_info:
            run(['promote-embedding', '--id', str(row_id),
                 '--reason', 'again', '--operator', 'quant'], db)
        assert exc_info.value.code != 0
