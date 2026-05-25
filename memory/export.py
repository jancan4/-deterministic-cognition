"""
Continuity bundle export with Phase 6D compression-derived event policy.

Phase 6D policy (compression artifacts are never bundled):
- Compression-derived proposed events are excluded by default.
- Compression-derived active/accepted events export normally.
- derived_from links export only when both endpoint events are in the bundle.
- Evidence JSON and source fields are preserved verbatim.
- compression_artifacts rows are never included.
"""
import json

from . import service


def _is_compression_derived(source: str) -> bool:
    """Return True if a memory event source indicates compression artifact origin."""
    return source.startswith('compression_artifact:')


def build_bundle(
    db_path: str,
    *,
    include_compression_derived_proposed: bool = False,
) -> dict:
    """Build a governed continuity bundle applying Phase 6D compression policy.

    By default, compression-derived proposed events (unreviewed memory candidates)
    are excluded from the bundle even if other proposed events would be included.
    Pass include_compression_derived_proposed=True to opt in to including them.

    derived_from links are exported only when both the source and target memory
    event IDs are present in the exported event set. This avoids dangling link
    references in the target substrate.

    The source field ('compression_artifact:<id>') and evidence JSON are preserved
    verbatim. The target substrate is not required to have the originating
    compression_artifact row.
    """
    raw = service.export_memory(db_path)

    events = raw['memory_events']
    links = raw['memory_links']

    if not include_compression_derived_proposed:
        events = [
            e for e in events
            if not (_is_compression_derived(e['source']) and e['status'] == 'proposed')
        ]

    exported_ids = {e['id'] for e in events}

    filtered_links = []
    for lnk in links:
        if lnk['relationship'] == 'derived_from':
            if lnk['source_id'] in exported_ids and lnk['target_id'] in exported_ids:
                filtered_links.append(lnk)
        else:
            filtered_links.append(lnk)

    return {
        'schema_version': raw['schema_version'],
        'memory_events': events,
        'memory_revisions': raw['memory_revisions'],
        'memory_links': filtered_links,
    }


def export_to_file(
    db_path: str,
    out_path: str,
    *,
    include_compression_derived_proposed: bool = False,
) -> dict:
    payload = build_bundle(
        db_path,
        include_compression_derived_proposed=include_compression_derived_proposed,
    )
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write('\n')
    return payload
