"""Tests for continuity/manifest.py."""
import copy
import json

import pytest

from continuity.manifest import (
    BundleValidationError,
    build_manifest,
    compute_bundle_checksum,
    validate_bundle,
)
from continuity.models import BUNDLE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minimal_bundle() -> dict:
    bundle = {
        'schema_version': BUNDLE_SCHEMA_VERSION,
        'memory_events': [],
        'source_documents': [],
        'ingestion_runs': [],
        'workflow_references': [],
        'semantic_execution_runs': [],
        'semantic_candidate_events': [],
    }
    manifest = build_manifest(
        bundle=bundle,
        exported_at='2026-01-01T00:00:00Z',
        exported_by='test',
        filters={},
    )
    bundle['manifest'] = manifest
    return bundle


def _bundle_with_event(event_id: int = 1) -> dict:
    event = {
        'id': event_id,
        'event_type': 'regime_observation',
        'title': 'Fed hold',
        'summary': 'Fed holds rates steady',
        'evidence': '',
        'source': '/data/a.txt',
        'confidence': 3,
        'status': 'unresolved',
        'tags': [],
        'related_ids': [],
        'created_by': 'test',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
        'version': 1,
    }
    bundle = {
        'schema_version': BUNDLE_SCHEMA_VERSION,
        'memory_events': [event],
        'source_documents': [],
        'ingestion_runs': [],
        'workflow_references': [],
        'semantic_execution_runs': [],
        'semantic_candidate_events': [],
    }
    manifest = build_manifest(
        bundle=bundle,
        exported_at='2026-01-01T00:00:00Z',
        exported_by='test',
        filters={},
    )
    bundle['manifest'] = manifest
    return bundle


# ---------------------------------------------------------------------------
# compute_bundle_checksum
# ---------------------------------------------------------------------------

class TestComputeBundleChecksum:
    def test_deterministic(self):
        b = _minimal_bundle()
        assert compute_bundle_checksum(b) == compute_bundle_checksum(b)

    def test_empty_bundle_produces_string(self):
        b = _minimal_bundle()
        checksum = compute_bundle_checksum(b)
        assert isinstance(checksum, str) and len(checksum) == 64

    def test_event_change_changes_checksum(self):
        b1 = _bundle_with_event(event_id=1)
        b2 = copy.deepcopy(b1)
        b2['memory_events'][0]['title'] = 'Different title'
        # Recompute manifest on b2 so its checksum field is updated
        b2['manifest'] = build_manifest(
            bundle={k: v for k, v in b2.items() if k != 'manifest'},
            exported_at=b2['manifest']['exported_at'],
            exported_by=b2['manifest']['exported_by'],
            filters=b2['manifest'].get('filters', {}),
        )
        assert compute_bundle_checksum(b1) != compute_bundle_checksum(b2)

    def test_checksum_excludes_manifest_checksum_field(self):
        b = _bundle_with_event()
        # Mutate the stored checksum field — recomputed checksum should still match original
        stored = b['manifest']['checksum_sha256']
        b_copy = copy.deepcopy(b)
        b_copy['manifest']['checksum_sha256'] = 'deadbeef' * 8
        assert compute_bundle_checksum(b_copy) == stored


# ---------------------------------------------------------------------------
# build_manifest
# ---------------------------------------------------------------------------

class TestBuildManifest:
    def test_count_fields(self):
        b = _bundle_with_event()
        m = b['manifest']
        assert m['memory_event_count'] == 1
        assert m['source_count'] == 0
        assert m['ingestion_run_count'] == 0
        assert m['workflow_reference_count'] == 0

    def test_bundle_id_is_hex_string(self):
        b = _bundle_with_event()
        assert isinstance(b['manifest']['bundle_id'], str)
        assert len(b['manifest']['bundle_id']) == 16

    def test_checksum_matches_computed(self):
        b = _bundle_with_event()
        assert b['manifest']['checksum_sha256'] == compute_bundle_checksum(b)

    def test_exported_at_preserved(self):
        bundle = {
            'schema_version': BUNDLE_SCHEMA_VERSION,
            'memory_events': [],
            'source_documents': [],
            'ingestion_runs': [],
            'workflow_references': [],
        }
        m = build_manifest(bundle, '2025-06-15T12:00:00Z', 'pytest', {})
        assert m['exported_at'] == '2025-06-15T12:00:00Z'

    def test_same_events_same_bundle_id(self):
        b1 = _bundle_with_event()
        b2 = _bundle_with_event()
        assert b1['manifest']['bundle_id'] == b2['manifest']['bundle_id']

    def test_different_events_different_bundle_id(self):
        b1 = _bundle_with_event(event_id=1)
        b2 = _bundle_with_event(event_id=2)
        assert b1['manifest']['bundle_id'] != b2['manifest']['bundle_id']


# ---------------------------------------------------------------------------
# validate_bundle
# ---------------------------------------------------------------------------

class TestValidateBundle:
    def test_valid_minimal_bundle(self):
        validate_bundle(_minimal_bundle())  # no exception

    def test_valid_bundle_with_event(self):
        validate_bundle(_bundle_with_event())

    def test_wrong_schema_version(self):
        b = _minimal_bundle()
        b['schema_version'] = '999.0'
        b['manifest']['schema_version'] = '999.0'
        with pytest.raises(BundleValidationError, match='schema_version'):
            validate_bundle(b)

    def test_missing_top_level_key(self):
        b = _minimal_bundle()
        del b['memory_events']
        with pytest.raises(BundleValidationError, match='missing required keys'):
            validate_bundle(b)

    def test_missing_manifest_key(self):
        b = _minimal_bundle()
        del b['manifest']['bundle_id']
        with pytest.raises(BundleValidationError, match='Manifest missing'):
            validate_bundle(b)

    def test_tampered_event_fails_checksum(self):
        b = _bundle_with_event()
        b['memory_events'][0]['title'] = 'TAMPERED'
        with pytest.raises(BundleValidationError, match='checksum mismatch'):
            validate_bundle(b)

    def test_wrong_count_fails(self):
        b = _minimal_bundle()
        b['manifest']['memory_event_count'] = 99
        with pytest.raises(BundleValidationError, match='memory_event_count'):
            validate_bundle(b)

    def test_not_a_dict(self):
        with pytest.raises(BundleValidationError, match='dict'):
            validate_bundle([])
