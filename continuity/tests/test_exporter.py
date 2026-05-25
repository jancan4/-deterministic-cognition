"""Tests for continuity/exporter.py."""
import json
import sqlite3

import pytest

from continuity.exporter import export_bundle
from continuity.manifest import compute_bundle_checksum, validate_bundle
from continuity.models import BUNDLE_SCHEMA_VERSION, ExportFilter


# ---------------------------------------------------------------------------
# DB setup helpers
# ---------------------------------------------------------------------------

def _init_memory_db(db_path: str) -> None:
    from memory import service as mem_service
    mem_service.init_db(db_path)


def _init_full_db(db_path: str) -> None:
    from memory import service as mem_service
    from sources.registry import init_registry
    from ingestion.runs import init_run_ledger
    mem_service.init_db(db_path)
    init_registry(db_path)
    init_run_ledger(db_path)


def _add_event(db_path: str, **kwargs) -> int:
    from memory import service as mem_service
    defaults = dict(
        event_type='regime_observation',
        title='Test event',
        summary='A test memory event',
        source='/data/test.txt',
        confidence=3,
        status='unresolved',
        created_by='pytest',
    )
    defaults.update(kwargs)
    ev = mem_service.add_memory_event(db_path=db_path, **defaults)
    return ev.id


def _add_source(db_path: str, path: str = '/data/test.txt') -> str:
    from sources.registry import init_registry
    from sources import models as src_models
    init_registry(db_path)
    import hashlib
    checksum = hashlib.sha256(path.encode()).hexdigest()
    source_id = hashlib.sha256(f"{path}\x00{checksum}".encode()).hexdigest()[:16]
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR IGNORE INTO source_documents
           (source_id, path, filename, checksum_sha256, size_bytes,
            modified_time, registered_at, source_type, authority_tier, status,
            metadata_json, version)
           VALUES (?, ?, ?, ?, 0, '', '2026-01-01T00:00:00Z', 'unknown', 'unknown', 'active', '{}', 1)
        """,
        (source_id, path, path.split('/')[-1], checksum),
    )
    conn.commit()
    conn.close()
    return source_id


def _add_run(db_path: str, source_id: str) -> str:
    from ingestion.runs import record_run, make_started_at
    run = record_run(
        db_path=db_path,
        source_id=source_id,
        source_checksum='abc123',
        source_version=1,
        parser_version='1.0',
        extractor_version='1.0',
        chunk_count=5,
        candidate_count=2,
        committed_count=1,
        committed_memory_ids=[1],
        status='committed',
        started_at=make_started_at(),
    )
    return run.run_id


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------

class TestExportEmptyDb:
    def test_export_empty_db(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        assert bundle['schema_version'] == BUNDLE_SCHEMA_VERSION
        assert bundle['memory_events'] == []
        assert bundle['source_documents'] == []
        assert bundle['ingestion_runs'] == []
        assert bundle['workflow_references'] == []

    def test_empty_bundle_validates(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        validate_bundle(bundle)  # no exception

    def test_empty_bundle_checksum_stable(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        b1 = export_bundle(db, exported_by='test')
        b2 = export_bundle(db, exported_by='test')
        # Different exported_at → different checksum (timestamp in manifest)
        # Content sections must be identical
        assert b1['memory_events'] == b2['memory_events']
        assert b1['source_documents'] == b2['source_documents']

    def test_missing_tables_returns_empty_sections(self, tmp_path):
        # Completely bare SQLite database with no tables
        db = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db)
        conn.close()
        bundle = export_bundle(db)
        assert bundle['memory_events'] == []
        assert bundle['source_documents'] == []
        assert bundle['ingestion_runs'] == []


# ---------------------------------------------------------------------------
# Basic export with data
# ---------------------------------------------------------------------------

class TestExportWithData:
    def test_events_present(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db, title='Event A')
        bundle = export_bundle(db)
        assert len(bundle['memory_events']) == 1
        assert bundle['memory_events'][0]['title'] == 'Event A'

    def test_bundle_validates_with_events(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db)
        bundle = export_bundle(db)
        validate_bundle(bundle)

    def test_event_ordering_by_id(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db, title='First')
        _add_event(db, title='Second')
        _add_event(db, title='Third')
        bundle = export_bundle(db)
        ids = [e['id'] for e in bundle['memory_events']]
        assert ids == sorted(ids)

    def test_source_documents_present(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_source(db, '/data/source.txt')
        _add_event(db, source='/data/source.txt')
        bundle = export_bundle(db)
        assert len(bundle['source_documents']) == 1
        assert bundle['source_documents'][0]['path'] == '/data/source.txt'

    def test_ingestion_runs_present(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        sid = _add_source(db)
        _add_run(db, sid)
        _add_event(db, source='/data/test.txt')
        bundle = export_bundle(db)
        assert len(bundle['ingestion_runs']) == 1

    def test_manifest_counts_match(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db)
        bundle = export_bundle(db)
        m = bundle['manifest']
        assert m['memory_event_count'] == len(bundle['memory_events'])
        assert m['source_count'] == len(bundle['source_documents'])
        assert m['ingestion_run_count'] == len(bundle['ingestion_runs'])


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestExportDeterminism:
    def test_same_content_same_event_section(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db, title='Stable')
        b1 = export_bundle(db)
        b2 = export_bundle(db)
        assert b1['memory_events'] == b2['memory_events']
        assert b1['source_documents'] == b2['source_documents']
        assert b1['ingestion_runs'] == b2['ingestion_runs']

    def test_json_round_trip(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db)
        bundle = export_bundle(db)
        serialized = json.dumps(bundle, sort_keys=True, indent=2)
        parsed = json.loads(serialized)
        validate_bundle(parsed)


# ---------------------------------------------------------------------------
# Filter: unresolved_only
# ---------------------------------------------------------------------------

class TestExportFilterUnresolvedOnly:
    def test_unresolved_only_excludes_resolved(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db, title='Unresolved', status='unresolved')
        _add_event(db, title='Accepted', status='accepted')
        f = ExportFilter(unresolved_only=True)
        bundle = export_bundle(db, export_filter=f)
        titles = [e['title'] for e in bundle['memory_events']]
        assert 'Unresolved' in titles
        assert 'Accepted' not in titles

    def test_no_filter_includes_all(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db, status='unresolved')
        _add_event(db, status='accepted')
        bundle = export_bundle(db)
        assert len(bundle['memory_events']) == 2


# ---------------------------------------------------------------------------
# Filter: since / until
# ---------------------------------------------------------------------------

class TestExportFilterDateRange:
    def test_since_excludes_older(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db)
        f = ExportFilter(since='2099-01-01T00:00:00Z')
        bundle = export_bundle(db, export_filter=f)
        assert bundle['memory_events'] == []

    def test_since_includes_current(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db)
        f = ExportFilter(since='2000-01-01T00:00:00Z')
        bundle = export_bundle(db, export_filter=f)
        assert len(bundle['memory_events']) == 1

    def test_until_excludes_future(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db)
        f = ExportFilter(until='2000-01-01T00:00:00Z')
        bundle = export_bundle(db, export_filter=f)
        assert bundle['memory_events'] == []


# ---------------------------------------------------------------------------
# Filter: tags
# ---------------------------------------------------------------------------

class TestExportFilterTags:
    def test_tag_filter_matches(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        from memory import service as mem_service
        mem_service.add_memory_event(
            db_path=db,
            event_type='regime_observation',
            title='Tagged',
            summary='has tag',
            source='/data/x.txt',
            confidence=3,
            status='unresolved',
            created_by='pytest',
            tags=['usd', 'fed'],
        )
        _add_event(db, title='Untagged')
        f = ExportFilter(tags=['usd'])
        bundle = export_bundle(db, export_filter=f)
        titles = [e['title'] for e in bundle['memory_events']]
        assert 'Tagged' in titles
        assert 'Untagged' not in titles

    def test_nonexistent_tag_returns_empty(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db)
        f = ExportFilter(tags=['no_such_tag'])
        bundle = export_bundle(db, export_filter=f)
        assert bundle['memory_events'] == []


# ---------------------------------------------------------------------------
# Semantic ledger portability (schema 1.1)
# ---------------------------------------------------------------------------

def _init_ledger_db(db_path: str) -> None:
    from memory import service as mem_service
    from sources.registry import init_registry
    from ingestion.runs import init_run_ledger
    from semantic.ledger import init_ledger
    mem_service.init_db(db_path)
    init_registry(db_path)
    init_run_ledger(db_path)
    init_ledger(db_path)


def _promote_one_candidate(db_path: str) -> tuple:
    """
    Run a semantic task, record it, promote one candidate.
    Returns (run_id, candidate_id, memory_event_id).
    """
    from models.adapters import StubModelAdapter
    from semantic.pipeline import run_semantic_task
    from semantic.ledger import (
        init_ledger, record_run, derive_candidate_id, promote_candidate
    )
    from memory import service as mem_service

    _init_ledger_db(db_path)
    adapter = StubModelAdapter()
    pr = run_semantic_task('tagging', 'The Fed held rates steady.', adapter)
    record_run(db_path, pr)

    run_id = pr.execution_result.request_id
    cid = derive_candidate_id(run_id, 0)
    mid = promote_candidate(db_path, cid, approved_by='test-exporter')
    return run_id, cid, mid


class TestExportSemanticSections:
    def test_schema_version_is_1_2(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        assert bundle['schema_version'] == '1.2'

    def test_empty_db_has_empty_semantic_sections(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        assert bundle['semantic_execution_runs'] == []
        assert bundle['semantic_candidate_events'] == []

    def test_promoted_candidate_exported_with_run(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        run_id, cid, mid = _promote_one_candidate(db)
        bundle = export_bundle(db)
        assert len(bundle['semantic_candidate_events']) == 1
        assert len(bundle['semantic_execution_runs']) == 1
        assert bundle['semantic_candidate_events'][0]['candidate_id'] == cid
        assert bundle['semantic_execution_runs'][0]['run_id'] == run_id

    def test_candidate_references_memory_event(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        run_id, cid, mid = _promote_one_candidate(db)
        bundle = export_bundle(db)
        cand = bundle['semantic_candidate_events'][0]
        assert cand['promoted_memory_id'] == mid

    def test_unpromoted_candidate_not_exported(self, tmp_path):
        """Only promoted candidates (status='promoted') are included."""
        db = str(tmp_path / 'memory.db')
        _init_ledger_db(db)
        from models.adapters import StubModelAdapter
        from semantic.pipeline import run_semantic_task
        from semantic.ledger import record_run
        adapter = StubModelAdapter()
        pr = run_semantic_task('tagging', 'ECB kept rates unchanged.', adapter)
        record_run(db_path=db, pipeline_result=pr)
        # No promotion — candidate stays in 'candidate' status
        bundle = export_bundle(db)
        assert bundle['semantic_candidate_events'] == []
        assert bundle['semantic_execution_runs'] == []

    def test_manifest_semantic_counts_match(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _promote_one_candidate(db)
        bundle = export_bundle(db)
        m = bundle['manifest']
        assert m['semantic_execution_run_count'] == len(bundle['semantic_execution_runs'])
        assert m['semantic_candidate_event_count'] == len(bundle['semantic_candidate_events'])

    def test_semantic_bundle_validates(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _promote_one_candidate(db)
        bundle = export_bundle(db)
        validate_bundle(bundle)  # no exception

    def test_semantic_bundle_json_roundtrip(self, tmp_path):
        import json as _json
        db = str(tmp_path / 'memory.db')
        _promote_one_candidate(db)
        bundle = export_bundle(db)
        parsed = _json.loads(_json.dumps(bundle, sort_keys=True, indent=2))
        validate_bundle(parsed)

    def test_run_fields_present(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _promote_one_candidate(db)
        bundle = export_bundle(db)
        run = bundle['semantic_execution_runs'][0]
        for field in ('run_id', 'task_id', 'task_type', 'adapter_name', 'adapter_version',
                      'input_hash', 'input_text', 'normalized_result',
                      'candidate_count', 'promoted_count', 'status', 'started_at', 'completed_at'):
            assert field in run, f"Missing field: {field}"

    def test_candidate_fields_present(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _promote_one_candidate(db)
        bundle = export_bundle(db)
        cand = bundle['semantic_candidate_events'][0]
        for field in ('candidate_id', 'semantic_run_id', 'candidate_index',
                      'event_type', 'title', 'summary', 'source', 'confidence',
                      'extraction_method', 'provenance', 'tags', 'status',
                      'promoted_memory_id', 'created_at'):
            assert field in cand, f"Missing field: {field}"

    def test_filter_excludes_unrelated_candidates(self, tmp_path):
        """Export with unresolved_only filter: only unresolved memory events → only their candidates."""
        from continuity.models import ExportFilter
        db = str(tmp_path / 'memory.db')
        _promote_one_candidate(db)
        # Approve the promoted event → becomes 'active'
        from memory import service as mem_service
        conn = __import__('sqlite3').connect(db)
        mid = conn.execute("SELECT id FROM memory_events").fetchone()[0]
        conn.close()
        mem_service.update_status(db, mid, 'active', reason='test', created_by='test')
        # Now export with unresolved_only — active events excluded
        bundle = export_bundle(db, export_filter=ExportFilter(unresolved_only=True))
        assert bundle['semantic_candidate_events'] == []


# ---------------------------------------------------------------------------
# Phase 9B: v1.2 manifest metadata
# ---------------------------------------------------------------------------

class TestV12ManifestInExport:
    def test_exported_db_schema_version_populated(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        # memory.service.init_db sets schema version; must be an int
        assert isinstance(bundle['manifest']['exported_db_schema_version'], int)
        assert bundle['manifest']['exported_db_schema_version'] > 0

    def test_exported_db_schema_version_is_none_on_bare_db(self, tmp_path):
        db = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db)
        conn.close()
        bundle = export_bundle(db)
        assert bundle['manifest']['exported_db_schema_version'] is None

    def test_compression_proposed_excluded_flag_default_true(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        assert bundle['manifest']['compression_derived_proposed_excluded'] is True

    def test_compression_proposed_excluded_flag_false_when_opted_in(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db, include_compression_derived_proposed=True)
        assert bundle['manifest']['compression_derived_proposed_excluded'] is False

    def test_compression_excluded_count_zero_with_no_proposed(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db, status='accepted')
        bundle = export_bundle(db)
        assert bundle['manifest']['compression_derived_proposed_excluded_count'] == 0

    def test_compression_excluded_count_for_proposed_events(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        # Directly insert compression-derived proposed events
        conn = sqlite3.connect(db)
        now = '2026-01-01T00:00:00Z'
        for i in range(3):
            conn.execute(
                """INSERT INTO memory_events
                   (event_type, title, summary, source, confidence, status,
                    tags_json, related_ids_json, created_by, created_at, updated_at, version)
                   VALUES (?, ?, ?, ?, 3, 'proposed', '[]', '[]', 'test', ?, ?, 1)""",
                ('hypothesis', f'Compr {i}', 'summary',
                 f'compression_artifact:{i+1}', now, now),
            )
        conn.commit()
        conn.close()
        bundle = export_bundle(db)
        assert bundle['manifest']['compression_derived_proposed_excluded_count'] == 3
        assert bundle['manifest']['compression_derived_proposed_excluded'] is True
        # The excluded events must not appear in the bundle
        assert len(bundle['memory_events']) == 0

    def test_include_compression_proposed_includes_them(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        conn = sqlite3.connect(db)
        now = '2026-01-01T00:00:00Z'
        conn.execute(
            """INSERT INTO memory_events
               (event_type, title, summary, source, confidence, status,
                tags_json, related_ids_json, created_by, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 3, 'proposed', '[]', '[]', 'test', ?, ?, 1)""",
            ('hypothesis', 'Compr event', 'summary', 'compression_artifact:7', now, now),
        )
        conn.commit()
        conn.close()
        bundle = export_bundle(db, include_compression_derived_proposed=True)
        assert bundle['manifest']['compression_derived_proposed_excluded'] is False
        assert bundle['manifest']['compression_derived_proposed_excluded_count'] == 0
        assert len(bundle['memory_events']) == 1

    def test_dangling_compression_source_count(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        conn = sqlite3.connect(db)
        now = '2026-01-01T00:00:00Z'
        # An active event with compression_artifact source (dangling)
        conn.execute(
            """INSERT INTO memory_events
               (event_type, title, summary, source, confidence, status,
                tags_json, related_ids_json, created_by, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 3, 'active', '[]', '[]', 'test', ?, ?, 1)""",
            ('hypothesis', 'Active compr', 'summary', 'compression_artifact:5', now, now),
        )
        # A normal event (not dangling)
        conn.execute(
            """INSERT INTO memory_events
               (event_type, title, summary, source, confidence, status,
                tags_json, related_ids_json, created_by, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 3, 'active', '[]', '[]', 'test', ?, ?, 1)""",
            ('hypothesis', 'Normal', 'summary', '/data/file.txt', now, now),
        )
        conn.commit()
        conn.close()
        bundle = export_bundle(db)
        assert bundle['manifest']['dangling_compression_source_count'] == 1
        assert len(bundle['memory_events']) == 2  # both exported

    def test_lineage_integrity_not_checked_by_default(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        assert bundle['manifest']['lineage_integrity_checked'] is False
        assert bundle['manifest']['lineage_integrity_all_ok'] is None
        assert bundle['manifest']['lineage_integrity_broken_count'] == 0

    def test_lineage_integrity_checked_when_flag_set(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db, include_lineage_integrity=True)
        assert bundle['manifest']['lineage_integrity_checked'] is True
        assert bundle['manifest']['lineage_integrity_all_ok'] is True
        assert bundle['manifest']['lineage_integrity_broken_count'] == 0

    def test_recovery_metadata_deterministic(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_full_db(db)
        _add_event(db, title='Stable')
        b1 = export_bundle(db, exported_by='test')
        b2 = export_bundle(db, exported_by='test')
        for field in (
            'exported_db_schema_version',
            'compression_derived_proposed_excluded',
            'compression_derived_proposed_excluded_count',
            'dangling_compression_source_count',
            'lineage_integrity_checked',
            'lineage_integrity_all_ok',
            'lineage_integrity_broken_count',
        ):
            assert b1['manifest'][field] == b2['manifest'][field], f"field {field!r} not deterministic"

    def test_v1_2_bundle_validates(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        bundle = export_bundle(db)
        validate_bundle(bundle)  # no exception

    def test_total_events_in_db_via_excluded_count(self, tmp_path):
        """Verify total_events_in_db = exported + excluded_proposed."""
        db = str(tmp_path / 'memory.db')
        _init_memory_db(db)
        conn = sqlite3.connect(db)
        now = '2026-01-01T00:00:00Z'
        # 2 active, 3 proposed-compression
        for i in range(2):
            conn.execute(
                """INSERT INTO memory_events
                   (event_type, title, summary, source, confidence, status,
                    tags_json, related_ids_json, created_by, created_at, updated_at, version)
                   VALUES (?, ?, ?, ?, 3, 'active', '[]', '[]', 'test', ?, ?, 1)""",
                ('hypothesis', f'Active {i}', 'summary', '/data/x.txt', now, now),
            )
        for i in range(3):
            conn.execute(
                """INSERT INTO memory_events
                   (event_type, title, summary, source, confidence, status,
                    tags_json, related_ids_json, created_by, created_at, updated_at, version)
                   VALUES (?, ?, ?, ?, 3, 'proposed', '[]', '[]', 'test', ?, ?, 1)""",
                ('hypothesis', f'Compr {i}', 'summary', f'compression_artifact:{i}', now, now),
            )
        conn.commit()
        conn.close()
        bundle = export_bundle(db)
        m = bundle['manifest']
        assert m['memory_event_count'] == 2
        assert m['compression_derived_proposed_excluded_count'] == 3
