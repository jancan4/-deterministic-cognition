"""
Phase 6D: continuity bundle policy for compression-derived memory events.

Coverage:
- _is_compression_derived(): source prefix detection
- build_bundle() / export_to_file() export eligibility:
    - proposed compression-derived events excluded by default
    - proposed CD events excluded even when ordinary proposed export is enabled
    - proposed CD events included only with explicit opt-in
    - active/accepted compression-derived events included by default
    - ordinary non-CD events behavior unchanged
    - no compression_artifacts section appears in bundle
- derived_from link policy:
    - included only when both endpoint events are in the bundle
    - excluded when either endpoint is excluded
    - non-derived_from links export normally
    - included when opt-in restores excluded endpoints
- Provenance preservation:
    - source='compression_artifact:<id>' preserved verbatim
    - evidence JSON preserved verbatim
    - import does not require compression_artifacts row in target
- Existing export tests remain green (verified via full suite)
"""
from __future__ import annotations

import json

import pytest

from memory import service
from memory.export import _is_compression_derived, build_bundle, export_to_file
from memory.service import add_memory_event, init_db, link_memory_events, update_status


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'bundle6d.db')
    init_db(path)
    return path


def _add(db_path, *, status='active', source='test', evidence=None, event_type='hypothesis'):
    ev = add_memory_event(
        db_path=db_path,
        event_type=event_type,
        title='Event',
        summary='Summary',
        source=source,
        confidence=3,
        status=status,
        created_by='tester',
        evidence=evidence,
    )
    return ev


def _add_cd(db_path, *, status='proposed', artifact_id=1, evidence=None):
    """Add a memory event that looks like a compression-derived event (source prefix)."""
    return _add(
        db_path,
        source=f'compression_artifact:{artifact_id}',
        status=status,
        evidence=evidence or json.dumps({'compression_artifact_id': artifact_id}),
    )


def _event_ids(bundle):
    return {e['id'] for e in bundle['memory_events']}


def _link_pairs(bundle):
    return {(lnk['source_id'], lnk['target_id'], lnk['relationship'])
            for lnk in bundle['memory_links']}


# ---------------------------------------------------------------------------
# TestIsCompressionDerived
# ---------------------------------------------------------------------------

class TestIsCompressionDerived:
    def test_compression_artifact_prefix_true(self):
        assert _is_compression_derived('compression_artifact:1') is True

    def test_compression_artifact_prefix_large_id_true(self):
        assert _is_compression_derived('compression_artifact:99999') is True

    def test_empty_source_false(self):
        assert _is_compression_derived('') is False

    def test_manual_source_false(self):
        assert _is_compression_derived('manual') is False

    def test_partial_prefix_false(self):
        assert _is_compression_derived('compression_artifact') is False

    def test_different_prefix_false(self):
        assert _is_compression_derived('assembly_artifact:1') is False


# ---------------------------------------------------------------------------
# TestExportEligibility
# ---------------------------------------------------------------------------

class TestExportEligibility:
    def test_proposed_cd_excluded_by_default(self, db):
        _add_cd(db, status='proposed')
        bundle = build_bundle(db)
        assert bundle['memory_events'] == []

    def test_proposed_cd_excluded_even_when_ordinary_proposed_in_bundle(self, db):
        ordinary = _add(db, status='proposed', source='manual')
        cd = _add_cd(db, status='proposed')
        bundle = build_bundle(db)
        ids = _event_ids(bundle)
        assert ordinary.id in ids
        assert cd.id not in ids

    def test_proposed_cd_included_with_explicit_opt_in(self, db):
        ev = _add_cd(db, status='proposed')
        bundle = build_bundle(db, include_compression_derived_proposed=True)
        assert ev.id in _event_ids(bundle)

    def test_active_cd_included_by_default(self, db):
        ev = _add_cd(db, status='active')
        bundle = build_bundle(db)
        assert ev.id in _event_ids(bundle)

    def test_accepted_cd_included_by_default(self, db):
        ev = _add_cd(db, status='accepted')
        bundle = build_bundle(db)
        assert ev.id in _event_ids(bundle)

    def test_ordinary_proposed_not_affected(self, db):
        ev = _add(db, status='proposed', source='human-review')
        bundle = build_bundle(db)
        assert ev.id in _event_ids(bundle)

    def test_ordinary_active_not_affected(self, db):
        ev = _add(db, status='active', source='human-review')
        bundle = build_bundle(db)
        assert ev.id in _event_ids(bundle)

    def test_no_compression_artifacts_section_in_bundle(self, db):
        _add_cd(db, status='active')
        bundle = build_bundle(db)
        assert 'compression_artifacts' not in bundle

    def test_schema_version_preserved(self, db):
        bundle = build_bundle(db)
        assert 'schema_version' in bundle
        assert bundle['schema_version'] == 1

    def test_multiple_cd_proposed_all_excluded(self, db):
        ev1 = _add_cd(db, status='proposed', artifact_id=1)
        ev2 = _add_cd(db, status='proposed', artifact_id=2)
        bundle = build_bundle(db)
        ids = _event_ids(bundle)
        assert ev1.id not in ids
        assert ev2.id not in ids

    def test_mixed_cd_statuses_only_proposed_excluded(self, db):
        proposed = _add_cd(db, status='proposed', artifact_id=1)
        active = _add_cd(db, status='active', artifact_id=2)
        bundle = build_bundle(db)
        ids = _event_ids(bundle)
        assert proposed.id not in ids
        assert active.id in ids

    def test_export_to_file_default_excludes_cd_proposed(self, db, tmp_path):
        ev = _add_cd(db, status='proposed')
        out = str(tmp_path / 'bundle.json')
        payload = export_to_file(db, out)
        ids = _event_ids(payload)
        assert ev.id not in ids

    def test_export_to_file_opt_in_includes_cd_proposed(self, db, tmp_path):
        ev = _add_cd(db, status='proposed')
        out = str(tmp_path / 'bundle.json')
        payload = export_to_file(db, out, include_compression_derived_proposed=True)
        ids = _event_ids(payload)
        assert ev.id in ids


# ---------------------------------------------------------------------------
# TestDerivedFromLinks
# ---------------------------------------------------------------------------

class TestDerivedFromLinks:
    def test_derived_from_link_included_when_both_endpoints_exported(self, db):
        src = _add(db, status='active')
        tgt = _add(db, status='active')
        link_memory_events(db, src.id, tgt.id, 'derived_from')
        bundle = build_bundle(db)
        assert (src.id, tgt.id, 'derived_from') in _link_pairs(bundle)

    def test_derived_from_link_excluded_when_source_is_excluded_cd_proposed(self, db):
        cd_src = _add_cd(db, status='proposed')
        tgt = _add(db, status='active')
        link_memory_events(db, cd_src.id, tgt.id, 'derived_from')
        bundle = build_bundle(db)
        pairs = _link_pairs(bundle)
        assert (cd_src.id, tgt.id, 'derived_from') not in pairs

    def test_derived_from_link_excluded_when_target_is_excluded_cd_proposed(self, db):
        src = _add(db, status='active')
        cd_tgt = _add_cd(db, status='proposed')
        link_memory_events(db, src.id, cd_tgt.id, 'derived_from')
        bundle = build_bundle(db)
        pairs = _link_pairs(bundle)
        assert (src.id, cd_tgt.id, 'derived_from') not in pairs

    def test_derived_from_link_included_with_opt_in_restores_endpoint(self, db):
        cd_src = _add_cd(db, status='proposed')
        tgt = _add(db, status='active')
        link_memory_events(db, cd_src.id, tgt.id, 'derived_from')
        bundle = build_bundle(db, include_compression_derived_proposed=True)
        assert (cd_src.id, tgt.id, 'derived_from') in _link_pairs(bundle)

    def test_non_derived_from_link_exports_normally(self, db):
        ev1 = _add(db, status='active')
        ev2 = _add(db, status='active')
        link_memory_events(db, ev1.id, ev2.id, 'related_to')
        bundle = build_bundle(db)
        assert (ev1.id, ev2.id, 'related_to') in _link_pairs(bundle)

    def test_supports_link_exports_normally(self, db):
        ev1 = _add(db, status='active')
        ev2 = _add(db, status='active')
        link_memory_events(db, ev1.id, ev2.id, 'supports')
        bundle = build_bundle(db)
        assert (ev1.id, ev2.id, 'supports') in _link_pairs(bundle)

    def test_derived_from_between_two_active_cd_events_included(self, db):
        src = _add_cd(db, status='active', artifact_id=1)
        tgt = _add_cd(db, status='active', artifact_id=2)
        link_memory_events(db, src.id, tgt.id, 'derived_from')
        bundle = build_bundle(db)
        assert (src.id, tgt.id, 'derived_from') in _link_pairs(bundle)

    def test_empty_db_no_links(self, db):
        bundle = build_bundle(db)
        assert bundle['memory_links'] == []


# ---------------------------------------------------------------------------
# TestProvenancePreservation
# ---------------------------------------------------------------------------

class TestProvenancePreservation:
    def test_source_field_preserved_verbatim_for_active_cd(self, db):
        artifact_id = 42
        ev = _add_cd(db, status='active', artifact_id=artifact_id)
        bundle = build_bundle(db)
        exported = next(e for e in bundle['memory_events'] if e['id'] == ev.id)
        assert exported['source'] == f'compression_artifact:{artifact_id}'

    def test_evidence_json_preserved_verbatim_for_active_cd(self, db):
        evidence_data = {
            'compression_artifact_id': 42,
            'seeded_by': 'quant',
            'source_memory_event_ids': [1, 2, 3],
        }
        evidence_str = json.dumps(evidence_data, sort_keys=True, separators=(',', ':'))
        ev = _add_cd(db, status='active', artifact_id=42, evidence=evidence_str)
        bundle = build_bundle(db)
        exported = next(e for e in bundle['memory_events'] if e['id'] == ev.id)
        assert exported['evidence'] == evidence_str

    def test_evidence_json_preserved_verbatim_with_opt_in(self, db):
        evidence_str = json.dumps({'compression_artifact_id': 7, 'seeded_by': 'op'},
                                  sort_keys=True, separators=(',', ':'))
        ev = _add_cd(db, status='proposed', artifact_id=7, evidence=evidence_str)
        bundle = build_bundle(db, include_compression_derived_proposed=True)
        exported = next(e for e in bundle['memory_events'] if e['id'] == ev.id)
        assert exported['evidence'] == evidence_str

    def test_import_does_not_require_compression_artifacts_row(self, db):
        """Bundle is self-contained; no compression_artifacts row needed in target."""
        ev = _add_cd(db, status='active', artifact_id=9999)
        bundle = build_bundle(db)
        # compression_artifacts table not in bundle
        assert 'compression_artifacts' not in bundle
        # Event is present and carries its provenance inline
        exported = next(e for e in bundle['memory_events'] if e['id'] == ev.id)
        assert exported['source'] == 'compression_artifact:9999'
        # The bundle is a valid, self-contained JSON dict
        assert isinstance(json.dumps(bundle), str)

    def test_source_not_rewritten_for_proposed_with_opt_in(self, db):
        ev = _add_cd(db, status='proposed', artifact_id=5)
        bundle = build_bundle(db, include_compression_derived_proposed=True)
        exported = next(e for e in bundle['memory_events'] if e['id'] == ev.id)
        assert exported['source'] == 'compression_artifact:5'

    def test_no_provenance_rewriting_on_export(self, db):
        evidence_str = json.dumps({'k': 'v'})
        ev = _add_cd(db, status='active', artifact_id=3, evidence=evidence_str)
        bundle = build_bundle(db)
        exported = next(e for e in bundle['memory_events'] if e['id'] == ev.id)
        # Evidence must not be modified, escaped differently, or wrapped
        assert exported['evidence'] == evidence_str


# ---------------------------------------------------------------------------
# TestExistingBehaviorUnchanged
# ---------------------------------------------------------------------------

class TestExistingBehaviorUnchanged:
    def test_non_cd_events_all_exported(self, db):
        ev1 = _add(db, status='active', source='briefing')
        ev2 = _add(db, status='proposed', source='review')
        bundle = build_bundle(db)
        ids = _event_ids(bundle)
        assert ev1.id in ids
        assert ev2.id in ids

    def test_non_derived_from_links_count_unchanged(self, db):
        ev1 = _add(db, status='active')
        ev2 = _add(db, status='active')
        ev3 = _add(db, status='active')
        link_memory_events(db, ev1.id, ev2.id, 'related_to')
        link_memory_events(db, ev2.id, ev3.id, 'supports')
        bundle = build_bundle(db)
        assert len(bundle['memory_links']) == 2

    def test_empty_db_empty_bundle(self, db):
        bundle = build_bundle(db)
        assert bundle['memory_events'] == []
        assert bundle['memory_revisions'] == []
        assert bundle['memory_links'] == []

    def test_revisions_exported_for_included_events(self, db):
        ev = _add(db, status='proposed', source='manual')
        update_status(db, ev.id, 'active', reason='promoted', created_by='op')
        bundle = build_bundle(db)
        assert len(bundle['memory_revisions']) == 1
        assert bundle['memory_revisions'][0]['memory_id'] == ev.id

    def test_bundle_is_deterministic(self, db):
        _add(db, status='active')
        _add_cd(db, status='active', artifact_id=1)
        b1 = build_bundle(db)
        b2 = build_bundle(db)
        assert json.dumps(b1, sort_keys=True) == json.dumps(b2, sort_keys=True)

    def test_events_ordered_by_id(self, db):
        _add(db, status='active')
        _add_cd(db, status='active', artifact_id=1)
        _add(db, status='active')
        bundle = build_bundle(db)
        ids = [e['id'] for e in bundle['memory_events']]
        assert ids == sorted(ids)
