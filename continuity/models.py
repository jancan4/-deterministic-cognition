"""
Data models for continuity bundle export/import.

A continuity bundle is a portable, deterministic snapshot of governed cognition
lineage. Bundles are transport containers — canonical truth remains in the
source database's lineage tables.
"""
import json
from dataclasses import dataclass, field
from typing import List, Optional

BUNDLE_SCHEMA_VERSION = '1.1'


@dataclass
class ExportFilter:
    """
    Optional filters applied during bundle export.

    All non-empty filter fields are combined with AND semantics.
    Empty / False fields are ignored (no restriction applied).
    """
    tags: List[str] = field(default_factory=list)
    source_ids: List[str] = field(default_factory=list)
    unresolved_only: bool = False
    since: Optional[str] = None   # ISO-8601; inclusive lower bound on created_at
    until: Optional[str] = None   # ISO-8601; inclusive upper bound on created_at

    def is_empty(self) -> bool:
        return (
            not self.tags
            and not self.source_ids
            and not self.unresolved_only
            and self.since is None
            and self.until is None
        )

    def to_dict(self) -> dict:
        return {
            'tags': list(self.tags),
            'source_ids': list(self.source_ids),
            'unresolved_only': self.unresolved_only,
            'since': self.since,
            'until': self.until,
        }


@dataclass
class ImportCollision:
    """A conflict detected during bundle import planning."""
    record_type: str   # 'memory_event', 'source_document', 'ingestion_run',
                       # 'semantic_execution_run', 'semantic_candidate_event', 'bundle'
    identifier: str    # id, source_id, run_id, candidate_id, or 'manifest'
    reason: str        # human-readable description

    def to_dict(self) -> dict:
        return {
            'record_type': self.record_type,
            'identifier': self.identifier,
            'reason': self.reason,
        }


@dataclass
class ImportResult:
    """
    The result of one import_bundle() call.

    In dry_run mode:
      imported_* counts reflect what WOULD be imported if run for real.
      No writes are made to any database.

    In actual import mode with collisions:
      imported_* counts are all 0 — the import is refused atomically.

    In actual import mode without collisions:
      imported_* counts reflect what was written.
    """
    imported_memory_events: int
    imported_source_documents: int
    imported_ingestion_runs: int
    imported_semantic_execution_runs: int
    imported_semantic_candidate_events: int
    skipped_memory_events: int
    skipped_source_documents: int
    skipped_ingestion_runs: int
    skipped_semantic_execution_runs: int
    skipped_semantic_candidate_events: int
    collisions: List[ImportCollision]
    dry_run: bool

    @property
    def has_collisions(self) -> bool:
        return len(self.collisions) > 0

    @property
    def success(self) -> bool:
        return not self.has_collisions

    def to_dict(self) -> dict:
        return {
            'dry_run': self.dry_run,
            'imported_memory_events': self.imported_memory_events,
            'imported_source_documents': self.imported_source_documents,
            'imported_ingestion_runs': self.imported_ingestion_runs,
            'imported_semantic_execution_runs': self.imported_semantic_execution_runs,
            'imported_semantic_candidate_events': self.imported_semantic_candidate_events,
            'skipped_memory_events': self.skipped_memory_events,
            'skipped_source_documents': self.skipped_source_documents,
            'skipped_ingestion_runs': self.skipped_ingestion_runs,
            'skipped_semantic_execution_runs': self.skipped_semantic_execution_runs,
            'skipped_semantic_candidate_events': self.skipped_semantic_candidate_events,
            'collision_count': len(self.collisions),
            'collisions': [c.to_dict() for c in self.collisions],
            'success': self.success,
        }
