"""
Data models for the source document registry.

A SourceDocument is a persistent record of a file that has been ingested or
registered for ingestion. It carries checksum, version, provenance metadata,
and an authority tier that weights how much cognitive trust the system places
on content from that source.

The registry is the authoritative store for source provenance. Memory events
point back to sources via the source path; the registry provides the checksum,
version, and authority context for that path.
"""
import json
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Approved enum values — validated at write time, enforced by CHECK constraints
# ---------------------------------------------------------------------------

VALID_SOURCE_TYPES = (
    'doctrine',
    'research_note',
    'article',
    'transcript',
    'implementation_brief',
    'architecture_doc',
    'external_reference',
    'unknown',
)

VALID_AUTHORITY_TIERS = (
    'authoritative',
    'high',
    'medium',
    'low',
    'unknown',
)

VALID_SOURCE_STATUSES = (
    'active',
    'superseded',
    'deprecated',
    'rejected',
    'archived',
)


class SourceValidationError(ValueError):
    pass


def _validate_source_type(source_type: str) -> None:
    if source_type not in VALID_SOURCE_TYPES:
        raise SourceValidationError(
            f"Invalid source_type {source_type!r}. Must be one of: {VALID_SOURCE_TYPES}"
        )


def _validate_authority_tier(authority_tier: str) -> None:
    if authority_tier not in VALID_AUTHORITY_TIERS:
        raise SourceValidationError(
            f"Invalid authority_tier {authority_tier!r}. Must be one of: {VALID_AUTHORITY_TIERS}"
        )


def _validate_source_status(status: str) -> None:
    if status not in VALID_SOURCE_STATUSES:
        raise SourceValidationError(
            f"Invalid status {status!r}. Must be one of: {VALID_SOURCE_STATUSES}"
        )


@dataclass
class SourceDocument:
    """
    A registered source document entry.

    source_id     — deterministic: sha256(abs_path + '\0' + checksum_sha256)[:16]
    path          — absolute filesystem path at time of registration
    filename      — basename of path
    checksum_sha256 — hex SHA-256 of the raw file bytes
    size_bytes    — file size at registration time
    modified_time — file mtime as ISO-8601 UTC string
    registered_at — wall-clock UTC time of registration
    source_type   — one of VALID_SOURCE_TYPES
    authority_tier — one of VALID_AUTHORITY_TIERS
    status        — one of VALID_SOURCE_STATUSES (default 'active')
    metadata      — arbitrary key-value pairs (stored as JSON)
    version       — 1 for initial registration; increments on content change
    """
    source_id: str
    path: str
    filename: str
    checksum_sha256: str
    size_bytes: int
    modified_time: str
    registered_at: str
    source_type: str
    authority_tier: str
    status: str
    metadata: dict
    version: int

    def __post_init__(self) -> None:
        _validate_source_type(self.source_type)
        _validate_authority_tier(self.authority_tier)
        _validate_source_status(self.status)
        if not self.source_id or not self.source_id.strip():
            raise SourceValidationError("source_id must not be empty")
        if not self.path or not self.path.strip():
            raise SourceValidationError("path must not be empty")
        if not self.checksum_sha256 or len(self.checksum_sha256) != 64:
            raise SourceValidationError(
                "checksum_sha256 must be a 64-character hex SHA-256 digest"
            )
        if self.size_bytes < 0:
            raise SourceValidationError("size_bytes must be non-negative")
        if self.version < 1:
            raise SourceValidationError("version must be >= 1")

    def to_dict(self) -> dict:
        return {
            'source_id': self.source_id,
            'path': self.path,
            'filename': self.filename,
            'checksum_sha256': self.checksum_sha256,
            'size_bytes': self.size_bytes,
            'modified_time': self.modified_time,
            'registered_at': self.registered_at,
            'source_type': self.source_type,
            'authority_tier': self.authority_tier,
            'status': self.status,
            'metadata': dict(self.metadata),
            'version': self.version,
        }

    @classmethod
    def from_row(cls, row) -> 'SourceDocument':
        return cls(
            source_id=row['source_id'],
            path=row['path'],
            filename=row['filename'],
            checksum_sha256=row['checksum_sha256'],
            size_bytes=row['size_bytes'],
            modified_time=row['modified_time'],
            registered_at=row['registered_at'],
            source_type=row['source_type'],
            authority_tier=row['authority_tier'],
            status=row['status'],
            metadata=json.loads(row['metadata_json'] or '{}'),
            version=row['version'],
        )
