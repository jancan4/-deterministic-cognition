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

    def test_import_result_has_warnings_field(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _promote_candidate(src_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db)
        assert hasattr(result, 'warnings')
        assert isinstance(result.warnings, list)

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


# ---------------------------------------------------------------------------
# Phase 9B: import warnings
# ---------------------------------------------------------------------------

def _make_bundle_with_compression_source(src_db: str) -> dict:
    """Export a bundle containing an event with a compression_artifact: source."""
    from memory import service as mem_service
    from continuity.exporter import export_bundle
    mem_service.init_db(src_db)
    conn = sqlite3.connect(src_db)
    now = '2026-01-01T00:00:00Z'
    conn.execute(
        """INSERT INTO memory_events
           (event_type, title, summary, source, confidence, status,
            tags_json, related_ids_json, created_by, created_at, updated_at, version)
           VALUES (?, ?, ?, ?, 3, 'active', '[]', '[]', 'test', ?, ?, 1)""",
        ('hypothesis', 'Compr event', 'summary', 'compression_artifact:42', now, now),
    )
    conn.commit()
    conn.close()
    return export_bundle(src_db, include_compression_derived_proposed=True)


class TestImportWarnings:
    def test_no_warnings_on_clean_import(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db)
        assert result.warnings == []

    def test_dangling_compression_source_warns(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        bundle = _make_bundle_with_compression_source(src_db)
        _init_db(dst_db)
        result = import_bundle(bundle, dst_db)
        assert result.success
        assert len(result.warnings) >= 1
        assert any('compression_artifact:42' in w for w in result.warnings)

    def test_dangling_compression_source_warns_in_dry_run(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        bundle = _make_bundle_with_compression_source(src_db)
        _init_db(dst_db)
        result = import_bundle(bundle, dst_db, dry_run=True)
        assert result.dry_run is True
        assert any('compression_artifact:42' in w for w in result.warnings)

    def test_compression_policy_disclosure_warning(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        # Insert a compression-derived proposed event in source
        conn = sqlite3.connect(src_db)
        now = '2026-01-01T00:00:00Z'
        conn.execute(
            """INSERT INTO memory_events
               (event_type, title, summary, source, confidence, status,
                tags_json, related_ids_json, created_by, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 3, 'proposed', '[]', '[]', 'test', ?, ?, 1)""",
            ('hypothesis', 'Compr proposed', 'summary', 'compression_artifact:1', now, now),
        )
        conn.commit()
        conn.close()
        # Export without proposed (default): count=1 in manifest
        bundle = export_bundle(src_db)
        assert bundle['manifest']['compression_derived_proposed_excluded_count'] == 1
        result = import_bundle(bundle, dst_db)
        assert any('Phase 6D policy' in w for w in result.warnings)

    def test_no_policy_disclosure_when_count_zero(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db, status='active')
        bundle = export_bundle(src_db)
        assert bundle['manifest']['compression_derived_proposed_excluded_count'] == 0
        result = import_bundle(bundle, dst_db)
        assert not any('Phase 6D policy' in w for w in result.warnings)

    def test_schema_version_mismatch_warns(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        # Forge a different exported_db_schema_version to simulate mismatch
        import copy
        bundle_forged = copy.deepcopy(bundle)
        bundle_forged['manifest']['exported_db_schema_version'] = 1  # old schema
        # Recompute checksum to keep bundle valid
        from continuity.manifest import compute_bundle_checksum, build_manifest
        bundle_content = {k: v for k, v in bundle_forged.items() if k != 'manifest'}
        manifest = build_manifest(
            bundle=bundle_content,
            exported_at=bundle_forged['manifest']['exported_at'],
            exported_by=bundle_forged['manifest']['exported_by'],
            filters=bundle_forged['manifest'].get('filters', {}),
            exported_db_schema_version=1,
            compression_derived_proposed_excluded=bundle_forged['manifest']['compression_derived_proposed_excluded'],
            compression_derived_proposed_excluded_count=bundle_forged['manifest']['compression_derived_proposed_excluded_count'],
            dangling_compression_source_count=bundle_forged['manifest']['dangling_compression_source_count'],
            lineage_integrity_checked=bundle_forged['manifest']['lineage_integrity_checked'],
            lineage_integrity_all_ok=bundle_forged['manifest']['lineage_integrity_all_ok'],
            lineage_integrity_broken_count=bundle_forged['manifest']['lineage_integrity_broken_count'],
        )
        bundle_forged['manifest'] = manifest
        result = import_bundle(bundle_forged, dst_db)
        assert any('schema v1' in w and 'target' in w for w in result.warnings)

    def test_no_schema_mismatch_warning_when_same(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        bundle = export_bundle(src_db)
        result = import_bundle(bundle, dst_db)
        assert not any('schema' in w.lower() and 'mismatch' in w.lower() for w in result.warnings)

    def test_warnings_in_to_dict(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        bundle = _make_bundle_with_compression_source(src_db)
        _init_db(dst_db)
        result = import_bundle(bundle, dst_db)
        d = result.to_dict()
        assert 'warnings' in d
        assert 'warning_count' in d
        assert d['warning_count'] == len(result.warnings)

    def test_no_warning_when_artifact_present_in_target(self, tmp_path):
        """No dangling warning when the artifact actually exists in target."""
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        bundle = _make_bundle_with_compression_source(src_db)
        _init_db(dst_db)
        # Drop and recreate a minimal stub — _init_db creates the real table with
        # many NOT NULL columns; we only need id for the importer's SELECT id check.
        conn = sqlite3.connect(dst_db)
        conn.execute("DROP TABLE IF EXISTS compression_artifacts")
        conn.execute("CREATE TABLE compression_artifacts (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO compression_artifacts (id) VALUES (42)")
        conn.commit()
        conn.close()
        result = import_bundle(bundle, dst_db)
        assert not any('compression_artifact:42' in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Phase 9B: bundle-inspect CLI smoke tests
# ---------------------------------------------------------------------------

def _run_main(argv):
    """Run cli.main with captured stdout/stderr; returns (stdout, stderr, exit_code)."""
    import io
    from cli.main import main
    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    import sys as _sys
    real_out, real_err = _sys.stdout, _sys.stderr
    exit_code = 0
    try:
        _sys.stdout = stdout_cap
        _sys.stderr = stderr_cap
        code = main(argv)
        if code is not None:
            exit_code = code
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0
    finally:
        _sys.stdout = real_out
        _sys.stderr = real_err
    return stdout_cap.getvalue(), stderr_cap.getvalue(), exit_code


class TestBundleInspect:
    def _write_bundle(self, tmp_path: object, db_path: str) -> str:
        import json
        bundle = export_bundle(db_path)
        path = str(tmp_path / 'bundle.json')
        with open(path, 'w') as fh:
            json.dump(bundle, fh)
        return path

    def test_inspect_valid_bundle_exits_0(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_db(db)
        path = self._write_bundle(tmp_path, db)
        stdout, stderr, code = _run_main(['bundle-inspect', path])
        assert code == 0

    def test_inspect_prints_bundle_id(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_db(db)
        path = self._write_bundle(tmp_path, db)
        stdout, stderr, code = _run_main(['bundle-inspect', path])
        assert 'bundle_id' in stdout

    def test_inspect_prints_valid(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_db(db)
        path = self._write_bundle(tmp_path, db)
        stdout, stderr, code = _run_main(['bundle-inspect', path])
        assert 'VALID' in stdout

    def test_inspect_json_format(self, tmp_path):
        import json
        db = str(tmp_path / 'memory.db')
        _init_db(db)
        path = self._write_bundle(tmp_path, db)
        stdout, stderr, code = _run_main(['bundle-inspect', path, '--format', 'json'])
        assert code == 0
        parsed = json.loads(stdout)
        assert 'bundle_id' in parsed
        assert 'checksum_sha256' in parsed

    def test_inspect_invalid_bundle_exits_nonzero(self, tmp_path):
        import json
        path = str(tmp_path / 'bad.json')
        with open(path, 'w') as fh:
            json.dump({'not': 'a bundle'}, fh)
        stdout, stderr, code = _run_main(['bundle-inspect', path])
        assert code != 0

    def test_inspect_missing_file_exits_nonzero(self, tmp_path):
        path = str(tmp_path / 'nonexistent.json')
        stdout, stderr, code = _run_main(['bundle-inspect', path])
        assert code != 0

    def test_inspect_makes_no_db_writes(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_db(db)
        _add_event(db)
        path = self._write_bundle(tmp_path, db)
        before = _count_table(db, 'memory_events')
        _run_main(['bundle-inspect', path])
        after = _count_table(db, 'memory_events')
        assert before == after

    def test_inspect_with_target_db_schema_match(self, tmp_path):
        db = str(tmp_path / 'memory.db')
        _init_db(db)
        path = self._write_bundle(tmp_path, db)
        stdout, stderr, code = _run_main(['bundle-inspect', path, '--db', db])
        assert code == 0
        assert 'schema_match' in stdout or 'target_schema' in stdout


# ---------------------------------------------------------------------------
# Phase 9B: import-bundle CLI exit code tests
# ---------------------------------------------------------------------------

class TestImportBundleCLIExitCodes:
    def _write_bundle_file(self, tmp_path, src_db: str) -> str:
        import json
        bundle = export_bundle(src_db)
        path = str(tmp_path / 'bundle.json')
        with open(path, 'w') as fh:
            json.dump(bundle, fh)
        return path

    def test_clean_import_exits_0(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db)
        path = self._write_bundle_file(tmp_path, src_db)
        _, _, code = _run_main(['import-bundle', '--db', dst_db, '--path', path])
        assert code == 0

    def test_dry_run_exits_0_even_with_warnings(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        import json
        bundle = _make_bundle_with_compression_source(src_db)
        _init_db(dst_db)
        path = str(tmp_path / 'bundle.json')
        with open(path, 'w') as fh:
            json.dump(bundle, fh)
        _, stderr, code = _run_main(['import-bundle', '--db', dst_db, '--path', path, '--dry-run'])
        assert code == 0  # dry-run always exits 0

    def test_import_with_warnings_exits_2(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        import json
        bundle = _make_bundle_with_compression_source(src_db)
        _init_db(dst_db)
        path = str(tmp_path / 'bundle.json')
        with open(path, 'w') as fh:
            json.dump(bundle, fh)
        _, stderr, code = _run_main(['import-bundle', '--db', dst_db, '--path', path])
        assert code == 2

    def test_collision_exits_1(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        _init_db(src_db)
        _init_db(dst_db)
        _add_event(src_db, title='A')
        path = self._write_bundle_file(tmp_path, src_db)
        _add_event(dst_db, title='B')  # collision on id=1
        _, _, code = _run_main(['import-bundle', '--db', dst_db, '--path', path])
        assert code == 1

    def test_warnings_printed_to_stderr(self, tmp_path):
        src_db = str(tmp_path / 'src.db')
        dst_db = str(tmp_path / 'dst.db')
        import json
        bundle = _make_bundle_with_compression_source(src_db)
        _init_db(dst_db)
        path = str(tmp_path / 'bundle.json')
        with open(path, 'w') as fh:
            json.dump(bundle, fh)
        _, stderr, code = _run_main(['import-bundle', '--db', dst_db, '--path', path])
        assert 'WARNING' in stderr
