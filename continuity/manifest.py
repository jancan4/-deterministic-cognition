"""
Manifest generation and bundle validation for continuity bundles.

The manifest is a tamper-evident header:
  - It records counts for each section.
  - Its checksum_sha256 field covers the entire bundle content (excluding the
    checksum field itself). Any modification to any record changes the checksum.

Checksum computation is deterministic:
  sha256( json.dumps(bundle_minus_checksum, sort_keys=True, ensure_ascii=True) )

bundle_id derivation:
  sha256( exported_at + NUL + str(sorted(event_ids)) )[:16]
  Stable for the same export of the same set of events.
"""
import hashlib
import json
from typing import List

from .models import BUNDLE_SCHEMA_VERSION


class BundleValidationError(ValueError):
    pass


def _generate_bundle_id(exported_at: str, event_ids: List[int]) -> str:
    raw = f"{exported_at}\x00{sorted(event_ids)}".encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def compute_bundle_checksum(bundle_dict: dict) -> str:
    """
    Compute the SHA-256 digest of the canonical bundle JSON.

    The manifest.checksum_sha256 field is excluded so the checksum can be
    stored inside the manifest without a circularity problem.
    """
    manifest_without_checksum = {
        k: v
        for k, v in bundle_dict.get('manifest', {}).items()
        if k != 'checksum_sha256'
    }
    content = {
        'schema_version': bundle_dict.get('schema_version', ''),
        'manifest': manifest_without_checksum,
        'memory_events': bundle_dict.get('memory_events', []),
        'source_documents': bundle_dict.get('source_documents', []),
        'ingestion_runs': bundle_dict.get('ingestion_runs', []),
        'workflow_references': bundle_dict.get('workflow_references', []),
    }
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def build_manifest(
    bundle: dict,
    exported_at: str,
    exported_by: str,
    filters: dict,
) -> dict:
    """
    Construct the manifest dict for a bundle, including checksum.

    The returned dict is ready to be inserted as bundle['manifest'].
    """
    event_ids = [e['id'] for e in bundle.get('memory_events', [])]
    bundle_id = _generate_bundle_id(exported_at, event_ids)

    manifest = {
        'bundle_id': bundle_id,
        'schema_version': BUNDLE_SCHEMA_VERSION,
        'exported_at': exported_at,
        'exported_by': exported_by,
        'filters': filters,
        'source_count': len(bundle.get('source_documents', [])),
        'memory_event_count': len(bundle.get('memory_events', [])),
        'ingestion_run_count': len(bundle.get('ingestion_runs', [])),
        'workflow_reference_count': len(bundle.get('workflow_references', [])),
        'checksum_sha256': '',  # placeholder; filled after checksum computed
    }

    # Compute checksum over bundle content + manifest (minus checksum placeholder)
    bundle_with_manifest = {**bundle, 'manifest': manifest}
    manifest['checksum_sha256'] = compute_bundle_checksum(bundle_with_manifest)
    return manifest


_REQUIRED_BUNDLE_KEYS = frozenset({
    'schema_version', 'manifest',
    'memory_events', 'source_documents', 'ingestion_runs', 'workflow_references',
})
_REQUIRED_MANIFEST_KEYS = frozenset({
    'bundle_id', 'schema_version', 'exported_at', 'exported_by',
    'source_count', 'memory_event_count', 'ingestion_run_count',
    'workflow_reference_count', 'checksum_sha256',
})


def validate_bundle(bundle_dict: dict) -> None:
    """
    Validate structure, counts, and checksum of a bundle dict.

    Raises BundleValidationError on any problem. Does not write to any database.
    """
    if not isinstance(bundle_dict, dict):
        raise BundleValidationError("Bundle must be a JSON object (dict)")

    # Schema version
    schema_version = bundle_dict.get('schema_version')
    if schema_version != BUNDLE_SCHEMA_VERSION:
        raise BundleValidationError(
            f"Unsupported schema_version {schema_version!r}. "
            f"Expected {BUNDLE_SCHEMA_VERSION!r}."
        )

    # Required top-level keys
    missing = _REQUIRED_BUNDLE_KEYS - set(bundle_dict.keys())
    if missing:
        raise BundleValidationError(
            f"Bundle missing required keys: {sorted(missing)}"
        )

    # Required manifest keys
    manifest = bundle_dict['manifest']
    if not isinstance(manifest, dict):
        raise BundleValidationError("manifest must be a JSON object")
    missing_m = _REQUIRED_MANIFEST_KEYS - set(manifest.keys())
    if missing_m:
        raise BundleValidationError(
            f"Manifest missing required keys: {sorted(missing_m)}"
        )

    # Section count consistency
    _check_count(manifest, 'memory_event_count', bundle_dict['memory_events'])
    _check_count(manifest, 'source_count', bundle_dict['source_documents'])
    _check_count(manifest, 'ingestion_run_count', bundle_dict['ingestion_runs'])
    _check_count(manifest, 'workflow_reference_count', bundle_dict['workflow_references'])

    # Checksum
    actual = compute_bundle_checksum(bundle_dict)
    if actual != manifest['checksum_sha256']:
        raise BundleValidationError(
            f"Bundle checksum mismatch — bundle may be corrupted or tampered. "
            f"Manifest: {manifest['checksum_sha256']!r}  Computed: {actual!r}"
        )


def _check_count(manifest: dict, key: str, section: list) -> None:
    declared = manifest.get(key, -1)
    actual = len(section)
    if declared != actual:
        raise BundleValidationError(
            f"Manifest {key}={declared} does not match actual count {actual}"
        )
