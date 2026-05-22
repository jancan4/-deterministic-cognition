"""Tests for sources.registry."""
import time
import pytest
from sources.registry import (
    init_registry,
    register_source,
    get_source_by_id,
    get_sources_by_path,
    get_sources_by_checksum,
    list_sources,
)
from sources.models import SourceDocument, SourceValidationError, VALID_SOURCE_TYPES, VALID_AUTHORITY_TIERS
from sources.checksums import compute_file_checksum


def _db(tmp_path) -> str:
    db = str(tmp_path / "memory.db")
    init_registry(db)
    return db


def _file(tmp_path, name="doc.txt", content=b"hello world") -> str:
    f = tmp_path / name
    f.write_bytes(content)
    return str(f)


# ---------------------------------------------------------------------------
# init_registry
# ---------------------------------------------------------------------------

def test_init_registry_creates_table(tmp_path):
    import sqlite3
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "source_documents" in tables


def test_init_registry_idempotent(tmp_path):
    db = str(tmp_path / "memory.db")
    init_registry(db)
    init_registry(db)  # must not raise


# ---------------------------------------------------------------------------
# register_source: basic contract
# ---------------------------------------------------------------------------

def test_register_returns_source_document(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    result = register_source(db, f)
    assert isinstance(result, SourceDocument)


def test_register_source_id_16_hex(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert len(doc.source_id) == 16
    int(doc.source_id, 16)


def test_register_path_is_absolute(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert doc.path.startswith("/")


def test_register_filename(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path, name="research.txt")
    doc = register_source(db, f)
    assert doc.filename == "research.txt"


def test_register_checksum_correct(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path, content=b"checksum test")
    doc = register_source(db, f)
    assert doc.checksum_sha256 == compute_file_checksum(f)


def test_register_size_bytes(tmp_path):
    db = _db(tmp_path)
    content = b"hello"
    f = _file(tmp_path, content=content)
    doc = register_source(db, f)
    assert doc.size_bytes == len(content)


def test_register_status_active(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert doc.status == "active"


def test_register_version_one(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert doc.version == 1


def test_register_default_source_type(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert doc.source_type == "unknown"


def test_register_default_authority_tier(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert doc.authority_tier == "unknown"


def test_register_custom_source_type(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f, source_type="research_note")
    assert doc.source_type == "research_note"


def test_register_custom_authority_tier(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f, authority_tier="high")
    assert doc.authority_tier == "high"


def test_register_metadata(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    meta = {"author": "Alice", "topic": "FX"}
    doc = register_source(db, f, metadata=meta)
    assert doc.metadata["author"] == "Alice"
    assert doc.metadata["topic"] == "FX"


def test_register_timestamps_utc_iso(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert doc.registered_at.endswith("Z")
    assert doc.modified_time.endswith("Z")


# ---------------------------------------------------------------------------
# Enum validation
# ---------------------------------------------------------------------------

def test_register_invalid_source_type_raises(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    with pytest.raises(SourceValidationError):
        register_source(db, f, source_type="not_a_real_type")


def test_register_invalid_authority_tier_raises(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    with pytest.raises(SourceValidationError):
        register_source(db, f, authority_tier="super_high")


def test_register_all_valid_source_types(tmp_path):
    db = _db(tmp_path)
    for i, st in enumerate(VALID_SOURCE_TYPES):
        f = _file(tmp_path, name=f"doc_{i}.txt", content=f"content {i}".encode())
        doc = register_source(db, f, source_type=st)
        assert doc.source_type == st


def test_register_all_valid_authority_tiers(tmp_path):
    db = _db(tmp_path)
    for i, tier in enumerate(VALID_AUTHORITY_TIERS):
        f = _file(tmp_path, name=f"tier_{i}.txt", content=f"tier {i}".encode())
        doc = register_source(db, f, authority_tier=tier)
        assert doc.authority_tier == tier


# ---------------------------------------------------------------------------
# Idempotency: same path + same checksum
# ---------------------------------------------------------------------------

def test_register_same_file_idempotent(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc1 = register_source(db, f)
    doc2 = register_source(db, f)
    assert doc1.source_id == doc2.source_id
    assert doc1.version == doc2.version == 1


def test_register_same_file_does_not_create_duplicate(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    register_source(db, f)
    register_source(db, f)
    docs = get_sources_by_path(db, f)
    assert len(docs) == 1


def test_register_same_file_returns_existing_record(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path, content=b"stable content")
    doc1 = register_source(db, f)
    doc2 = register_source(db, f)
    assert doc1.source_id == doc2.source_id
    assert doc1.registered_at == doc2.registered_at  # same record


# ---------------------------------------------------------------------------
# Versioning: same path + changed checksum
# ---------------------------------------------------------------------------

def test_register_changed_file_creates_new_version(tmp_path):
    db = _db(tmp_path)
    f = tmp_path / "doc.txt"

    f.write_bytes(b"version one")
    doc1 = register_source(db, str(f))

    f.write_bytes(b"version two")
    doc2 = register_source(db, str(f))

    assert doc1.source_id != doc2.source_id
    assert doc2.version == 2


def test_register_changed_file_supersedes_old(tmp_path):
    db = _db(tmp_path)
    f = tmp_path / "doc.txt"

    f.write_bytes(b"original content")
    doc1 = register_source(db, str(f))
    assert doc1.status == "active"

    f.write_bytes(b"updated content")
    register_source(db, str(f))

    # Reload the original record
    reloaded = get_source_by_id(db, doc1.source_id)
    assert reloaded.status == "superseded"


def test_register_changed_file_new_version_is_active(tmp_path):
    db = _db(tmp_path)
    f = tmp_path / "doc.txt"

    f.write_bytes(b"v1")
    register_source(db, str(f))

    f.write_bytes(b"v2")
    doc2 = register_source(db, str(f))
    assert doc2.status == "active"


def test_register_three_versions(tmp_path):
    db = _db(tmp_path)
    f = tmp_path / "doc.txt"

    for i in range(1, 4):
        f.write_bytes(f"version {i}".encode())
        doc = register_source(db, str(f))
        assert doc.version == i

    all_versions = get_sources_by_path(db, str(f))
    assert len(all_versions) == 3
    statuses = [d.status for d in all_versions]
    assert statuses.count("active") == 1
    assert statuses.count("superseded") == 2


# ---------------------------------------------------------------------------
# source_id determinism
# ---------------------------------------------------------------------------

def test_source_id_deterministic_same_content_same_path(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path, content=b"stable")
    doc1 = register_source(db, f)

    # Drop the record and re-register; same source_id must come back
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM source_documents WHERE source_id = ?", (doc1.source_id,))
    conn.commit()
    conn.close()

    doc2 = register_source(db, f)
    assert doc2.source_id == doc1.source_id


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def test_get_source_by_id(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    retrieved = get_source_by_id(db, doc.source_id)
    assert retrieved is not None
    assert retrieved.source_id == doc.source_id


def test_get_source_by_id_not_found(tmp_path):
    db = _db(tmp_path)
    result = get_source_by_id(db, "0000000000000000")
    assert result is None


def test_get_sources_by_path(tmp_path):
    db = _db(tmp_path)
    f = _file(tmp_path)
    register_source(db, f)
    docs = get_sources_by_path(db, f)
    assert len(docs) == 1
    assert docs[0].path == str((tmp_path / "doc.txt").resolve())


def test_get_sources_by_path_returns_all_versions(tmp_path):
    db = _db(tmp_path)
    f = tmp_path / "doc.txt"

    f.write_bytes(b"v1")
    register_source(db, str(f))
    f.write_bytes(b"v2")
    register_source(db, str(f))

    docs = get_sources_by_path(db, str(f))
    assert len(docs) == 2
    assert [d.version for d in docs] == [1, 2]


def test_get_sources_by_path_ordered_by_version(tmp_path):
    db = _db(tmp_path)
    f = tmp_path / "doc.txt"
    for i in range(1, 4):
        f.write_bytes(f"v{i}".encode())
        register_source(db, str(f))

    docs = get_sources_by_path(db, str(f))
    versions = [d.version for d in docs]
    assert versions == sorted(versions)


def test_get_sources_by_checksum(tmp_path):
    db = _db(tmp_path)
    content = b"same content"
    f1 = _file(tmp_path, name="a.txt", content=content)
    f2 = _file(tmp_path, name="b.txt", content=content)

    register_source(db, f1)
    register_source(db, f2)

    checksum = compute_file_checksum(f1)
    docs = get_sources_by_checksum(db, checksum)
    assert len(docs) == 2


def test_get_sources_by_checksum_empty(tmp_path):
    db = _db(tmp_path)
    docs = get_sources_by_checksum(db, "a" * 64)
    assert docs == []


def test_list_sources_returns_all(tmp_path):
    db = _db(tmp_path)
    f1 = _file(tmp_path, name="a.txt", content=b"aaa")
    f2 = _file(tmp_path, name="b.txt", content=b"bbb")
    register_source(db, f1)
    register_source(db, f2)
    docs = list_sources(db)
    assert len(docs) >= 2


def test_list_sources_filter_by_status(tmp_path):
    db = _db(tmp_path)
    f = tmp_path / "doc.txt"
    f.write_bytes(b"v1")
    register_source(db, str(f))
    f.write_bytes(b"v2")
    register_source(db, str(f))

    active = list_sources(db, status="active")
    superseded = list_sources(db, status="superseded")
    assert all(d.status == "active" for d in active)
    assert all(d.status == "superseded" for d in superseded)


def test_list_sources_filter_by_type(tmp_path):
    db = _db(tmp_path)
    f1 = _file(tmp_path, name="a.txt", content=b"aaa")
    f2 = _file(tmp_path, name="b.txt", content=b"bbb")
    register_source(db, f1, source_type="research_note")
    register_source(db, f2, source_type="doctrine")

    notes = list_sources(db, source_type="research_note")
    assert all(d.source_type == "research_note" for d in notes)


def test_list_sources_invalid_status_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(SourceValidationError):
        list_sources(db, status="not_a_status")


def test_list_sources_invalid_source_type_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(SourceValidationError):
        list_sources(db, source_type="not_a_type")


# ---------------------------------------------------------------------------
# No network access
# ---------------------------------------------------------------------------

def test_register_does_not_make_network_calls(tmp_path, monkeypatch):
    """Patch socket.socket to prove no network is used."""
    import socket

    original_socket = socket.socket

    def _fail(*args, **kwargs):
        raise RuntimeError("Network call detected — registry must not use network")

    monkeypatch.setattr(socket, "socket", _fail)
    db = _db(tmp_path)
    f = _file(tmp_path)
    doc = register_source(db, f)
    assert isinstance(doc, SourceDocument)


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------

def test_list_sources_ordered_by_registered_at_desc(tmp_path):
    db = _db(tmp_path)
    for i in range(3):
        f = _file(tmp_path, name=f"doc_{i}.txt", content=f"content {i}".encode())
        register_source(db, f)

    docs = list_sources(db)
    timestamps = [d.registered_at for d in docs]
    assert timestamps == sorted(timestamps, reverse=True)
