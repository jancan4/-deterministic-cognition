"""Tests for continuity/importer.py."""
import copy
import sqlite3

import pytest

from continuity.exporter import export_bundle
from continuity.importer import import_bundle
from continuity.manifest import BundleValidationError, validate_bundle
from continuity.models import BUNDLE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_db(db_path: str) -> None:
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
        summary='Summary',
        source='/data/test.txt',
        confidence=3,
        status='unresolved',
        created_by='pytest',
    )
    defaults.update(kwargs)
    ev = mem_service.add_memory_event(db_path=db_path, **defaults)
    return ev.id


def _count_table(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Basic roundtrip
# ---------------------------------------------------------------------------

class TestRoundtrip:
    def test_empty_roundtrip(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db)
        assert result.success
        assert result.imported_memory_events == 0
        assert result.skipped_memory_events == 0

    def test_single_event_roundtrip(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db, title='Migrate me')
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db)
        assert result.success
        assert result.imported_memory_events == 1
        assert _count_table(dst_db, 'memory_events') == 1

    def test_imported_event_has_correct_id(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        event_id = _add_event(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        conn = sqlite3.connect(dst_db)
        row = conn.execute("SELECT id FROM memory_events").fetchone()
        conn.close()
        assert row[0] == event_id

    def test_multiple_events_roundtrip(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        for i in range(5):
            _add_event(src_db, title=f'Event {i}')
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db)
        assert result.imported_memory_events == 5
        assert _count_table(dst_db, 'memory_events') == 5

    def test_second_import_of_same_bundle_skips(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        result2 = import_bundle(bundle, dst_db)
        assert result2.success
        assert result2.imported_memory_events == 0
        assert result2.skipped_memory_events == 1
        assert _count_table(dst_db, 'memory_events') == 1

    def test_imported_bundle_validates(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        re_exported = export_bundle(dst_db)
        validate_bundle(re_exported)


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_makes_no_writes(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db, dry_run=True)
        assert result.dry_run is True
        assert result.imported_memory_events == 1  # would import
        assert _count_table(dst_db, 'memory_events') == 0  # nothing written

    def test_dry_run_reports_skips(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        result = import_bundle(bundle, dst_db, dry_run=True)
        assert result.dry_run is True
        assert result.skipped_memory_events == 1
        assert result.imported_memory_events == 0

    def test_dry_run_success_flag(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db, dry_run=True)
        assert result.success


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------

class TestCollisionDetection:
    def test_collision_when_id_exists_with_different_payload(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)

        # Add id=1 to src, export
        _add_event(src_db, title='Original title')
        bundle = export_bundle(src_db)

        # Add a conflicting record with same (expected) id=1 to dst
        _add_event(dst_db, title='Conflicting title')

        result = import_bundle(bundle, dst_db)
        assert result.has_collisions
        assert not result.success
        assert len(result.collisions) == 1
        assert result.collisions[0].record_type == 'memory_event'

    def test_collision_blocks_all_writes(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)

        _add_event(src_db, title='Event A')
        _add_event(src_db, title='Event B')
        bundle = export_bundle(src_db)

        # Only add one conflicting record to dst
        _add_event(dst_db, title='Conflict')

        before_count = _count_table(dst_db, 'memory_events')
        result = import_bundle(bundle, dst_db)
        after_count = _count_table(dst_db, 'memory_events')

        assert result.has_collisions
        assert before_count == after_count  # atomic rejection — nothing written

    def test_same_payload_is_skip_not_collision(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db, title='Stable')
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        result = import_bundle(bundle, dst_db)
        assert not result.has_collisions
        assert result.skipped_memory_events == 1

    def test_collision_result_dict(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db, title='A')
        bundle = export_bundle(src_db)
        _add_event(dst_db, title='B')
        result = import_bundle(bundle, dst_db)
        d = result.to_dict()
        assert d['success'] is False
        assert d['collision_count'] == 1
        assert d['collisions'][0]['record_type'] == 'memory_event'


# ---------------------------------------------------------------------------
# Bundle validation
# ---------------------------------------------------------------------------

class TestBundleValidation:
    def test_invalid_bundle_raises(self, tmp_path):
        dst_db = str(tmp_path / 'dst.db')
        _init_db(dst_db)
        bad = {'not': 'a valid bundle'}
        with pytest.raises(BundleValidationError):
            import_bundle(bad, dst_db)

    def test_tampered_checksum_raises(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        bundle['memory_events'][0]['title'] = 'TAMPERED'
        with pytest.raises(BundleValidationError, match='checksum'):
            import_bundle(bundle, dst_db)


# ---------------------------------------------------------------------------
# No mutation on read-only target
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_import_into_readonly_memory_db_raises(self, tmp_path):
        import os
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        os.chmod(dst_db, 0o444)
        try:
            with pytest.raises(Exception):
                import_bundle(bundle, dst_db)
        finally:
            os.chmod(dst_db, 0o644)


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


def _promote_candidate(db_path: str, text: str = 'The Fed held rates.') -> tuple:
    """Run, record, and promote one semantic candidate. Returns (run_id, cid, mid)."""
    from models.adapters import StubModelAdapter
    from semantic.pipeline import run_semantic_task
    from semantic.ledger import record_run, derive_candidate_id, promote_candidate
    _init_ledger_db(db_path)
    pr = run_semantic_task('tagging', text, StubModelAdapter())
    record_run(db_path, pr)
    run_id = pr.execution_result.request_id
    cid = derive_candidate_id(run_id, 0)
    mid = promote_candidate(db_path, cid, approved_by='test')
    return run_id, cid, mid


class TestSemanticLedgerRoundtrip:
    def test_promoted_candidate_survives_roundtrip(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        run_id, cid, mid = _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db)
        assert result.success
        assert result.imported_semantic_candidate_events == 1
        assert result.imported_semantic_execution_runs == 1

    def test_semantic_run_row_present_in_dst(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        run_id, cid, mid = _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        assert _count_table(dst_db, 'semantic_execution_runs') == 1

    def test_semantic_candidate_row_present_in_dst(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        run_id, cid, mid = _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        assert _count_table(dst_db, 'semantic_candidate_events') == 1

    def test_candidate_promoted_memory_id_preserved(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        run_id, cid, mid = _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        conn = sqlite3.connect(dst_db)
        row = conn.execute(
            "SELECT promoted_memory_id FROM semantic_candidate_events WHERE candidate_id = ?", (cid,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == mid

    def test_second_import_skips_semantic_rows(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        result2 = import_bundle(bundle, dst_db)
        assert result2.success
        assert result2.imported_semantic_execution_runs == 0
        assert result2.imported_semantic_candidate_events == 0
        assert result2.skipped_semantic_execution_runs == 1
        assert result2.skipped_semantic_candidate_events == 1

    def test_dry_run_reports_semantic_counts(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db, dry_run=True)
        assert result.dry_run is True
        assert result.imported_semantic_execution_runs == 1
        assert result.imported_semantic_candidate_events == 1
        assert _count_table(dst_db, 'semantic_execution_runs') == 0

    def test_semantic_bundle_validates_on_dst_re_export(self, tmp_path):
        from continuity.manifest import validate_bundle as vb
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        import_bundle(bundle, dst_db)
        re_exported = export_bundle(dst_db)
        vb(re_exported)  # no exception

    def test_dangling_promoted_memory_id_is_collision(self, tmp_path):
        """Candidate referencing a memory_event not in bundle or dst → collision."""
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        # Remove memory_events from bundle → candidate has dangling promoted_memory_id
        bundle_no_events = dict(bundle)
        bundle_no_events['memory_events'] = []
        # Recompute manifest and checksum to make bundle valid structurally
        from continuity.manifest import build_manifest
        manifest = build_manifest(
            bundle={k: v for k, v in bundle_no_events.items() if k != 'manifest'},
            exported_at=bundle['manifest']['exported_at'],
            exported_by=bundle['manifest']['exported_by'],
            filters=bundle['manifest'].get('filters', {}),
        )
        bundle_no_events['manifest'] = manifest
        result = import_bundle(bundle_no_events, dst_db)
        assert result.has_collisions
        assert any(
            c.record_type == 'semantic_candidate_event' for c in result.collisions
        )
