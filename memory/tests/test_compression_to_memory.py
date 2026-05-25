"""
Phase 7B: compression-to-memory candidate pathway tests.

Coverage:
- seed_memory_from_compression(): input guards, field mapping, confidence logic
- Truncation: short text verbatim, long text truncated at 4000, suffix once, source unchanged
- Evidence JSON: byte-stable, sort_keys, sorted list fields, required provenance fields
- derived_from links: created, missing source events skipped, IntegrityError skipped
- Idempotency: MemorySeedException on second call, existing_memory_event_id correct
- Status guards: candidate/superseded/invalidated rejected, only active allowed
  - Explicit test: superseded compression artifact cannot seed memory
- list_compression_derived_memory(): empty, results, status filter, non-compression excluded
- CLI: seed-memory-from-compression happy path, non-active artifact exits, already-seeded exits
- CLI: list-compression-derived-memory empty and with results
- Governance detector: empty, recent not flagged, stale flagged, non-proposed not flagged,
  non-compression source not flagged, metadata fields, wired into report, disable flag
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memory.cli import _COMMANDS, build_parser
from memory.compression import (
    _SUMMARY_TRUNCATION_LIMIT,
    _TRUNCATION_SUFFIX,
    _build_evidence_json,
    _compression_source_key,
    _truncate_artifact_text,
    MemorySeedException,
    create_compression_artifact,
    invalidate_compression_artifact,
    list_compression_derived_memory,
    promote_compression_artifact,
    seed_memory_from_compression,
    supersede_compression_artifact,
)
from memory.governance import (
    COMPRESSION_MEMORY_CANDIDATE_WARNING_DAYS,
    build_governance_report,
    detect_unreviewed_compression_derived_memory,
)
from memory.service import add_memory_event, init_db


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------

_asm_counter = 0


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'mem7b.db')
    init_db(path)
    return path


def _add_event(db_path: str, *, status: str = 'active') -> int:
    ev = add_memory_event(
        db_path=db_path,
        event_type='hypothesis',
        title='Source event',
        summary='Source summary',
        source='test',
        confidence=3,
        status=status,
        created_by='tester',
    )
    return ev.id


def _insert_assembly(db_path: str, memory_ids=None, link_ids=None) -> int:
    global _asm_counter
    _asm_counter += 1
    now = datetime.now(timezone.utc).isoformat()
    unique_hash = f'testhash_{_asm_counter:08d}'

    governance_ctx = []
    if memory_ids:
        for mid in memory_ids:
            governance_ctx.append({'memory_id': mid, 'confidence': 3})
    conflicting_pairs = []
    if link_ids:
        for lid in link_ids:
            conflicting_pairs.append({'link_id': lid})

    snapshot = {
        'governance_context': governance_ctx,
        'unresolved_items': [],
        'active_investigations': [],
        'relevant_memory': [],
        'conflicting_pairs': conflicting_pairs,
        'workflow_events': [],
        'runtime_snapshots': [],
        'total_chars': 0,
        'total_entries': 0,
        'char_budget': 12000,
        'entry_budget': 60,
        'included_entries': 0,
        'total_candidates': 0,
        'chars_used': 0,
        'session_id': 'test',
    }

    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO context_assembly_log
           (assembly_hash, session_id, assembly_version, assembled_at, db_path,
            policy_json, entries_accepted, entries_rejected_budget, entries_rejected_filter,
            char_budget_used, char_budget_limit, compression_mode, assembly_snapshot_json)
           VALUES (?, 'test', '1.0.0', ?, ?, '{}', 0, 0, 0, 0, 12000, 'none', ?)""",
        (unique_hash, now, db_path, json.dumps(snapshot)),
    )
    conn.commit()
    asm_id = cur.lastrowid
    conn.close()
    return asm_id


def _make_active_artifact(
    db_path: str,
    *,
    artifact_text: str = 'Artifact body text.',
    compression_confidence: int = 4,
    memory_ids=None,
) -> int:
    """Create a candidate artifact and promote it to active. Returns artifact id."""
    asm_id = _insert_assembly(db_path, memory_ids=memory_ids)
    art = create_compression_artifact(
        db_path=db_path,
        source_assembly_id=asm_id,
        compression_method='test-method',
        producer_version='0.1.0',
        artifact_text=artifact_text,
        created_by='tester',
        compression_confidence=compression_confidence,
    )
    promoted = promote_compression_artifact(
        db_path=db_path,
        artifact_id=art.id,
        promoted_by='quant',
        promotion_notes='approved',
    )
    assert promoted.status == 'active'
    return promoted.id


def _make_candidate_artifact(db_path: str) -> int:
    """Create a candidate artifact (not promoted). Returns artifact id."""
    asm_id = _insert_assembly(db_path)
    art = create_compression_artifact(
        db_path=db_path,
        source_assembly_id=asm_id,
        compression_method='test-method',
        producer_version='0.1.0',
        artifact_text='Candidate artifact.',
        created_by='tester',
    )
    return art.id


def _row_count(db_path, table):
    conn = sqlite3.connect(db_path)
    n = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    conn.close()
    return n


def _backdate_memory_event(db_path: str, event_id: int, days: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memory_events SET created_at = ?, updated_at = ? WHERE id = ?",
                 (ts, ts, event_id))
    conn.commit()
    conn.close()


def _run_cli(args_list):
    parser = build_parser()
    args = parser.parse_args(args_list)
    _COMMANDS[args.command](args)


# ---------------------------------------------------------------------------
# TestSeedMemoryFromCompression
# ---------------------------------------------------------------------------

class TestSeedMemoryFromCompression:
    def test_guard_empty_operator(self, db):
        art_id = _make_active_artifact(db)
        with pytest.raises(ValueError, match="operator"):
            seed_memory_from_compression(
                db, art_id, '', 'reason', event_type='hypothesis', title='T'
            )

    def test_guard_whitespace_operator(self, db):
        art_id = _make_active_artifact(db)
        with pytest.raises(ValueError, match="operator"):
            seed_memory_from_compression(
                db, art_id, '   ', 'reason', event_type='hypothesis', title='T'
            )

    def test_guard_empty_reason(self, db):
        art_id = _make_active_artifact(db)
        with pytest.raises(ValueError, match="reason"):
            seed_memory_from_compression(
                db, art_id, 'op', '', event_type='hypothesis', title='T'
            )

    def test_guard_empty_title(self, db):
        art_id = _make_active_artifact(db)
        with pytest.raises(ValueError, match="title"):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title=''
            )

    def test_guard_invalid_event_type(self, db):
        art_id = _make_active_artifact(db)
        with pytest.raises(ValueError, match="Invalid event_type"):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='not_a_type', title='T'
            )

    def test_guard_artifact_not_found(self, db):
        with pytest.raises(ValueError, match="not found"):
            seed_memory_from_compression(
                db, 99999, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_returns_memory_event(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='My Title'
        )
        assert ev.id is not None
        assert ev.id > 0

    def test_field_title(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='My Title'
        )
        assert ev.title == 'My Title'

    def test_field_status_is_proposed(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.status == 'proposed'

    def test_field_source_format(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.source == f'compression_artifact:{art_id}'

    def test_field_event_type(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='governance_rule', title='T'
        )
        assert ev.event_type == 'governance_rule'

    def test_field_created_by(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'quant-op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.created_by == 'quant-op'

    def test_tags_always_includes_compression_derived(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert 'compression-derived' in ev.tags

    def test_extra_tags_merged_and_sorted(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T',
            tags=['zzz-tag', 'aaa-tag']
        )
        assert 'compression-derived' in ev.tags
        assert 'zzz-tag' in ev.tags
        assert 'aaa-tag' in ev.tags
        assert ev.tags == sorted(ev.tags)

    def test_compression_derived_not_duplicated_in_tags(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T',
            tags=['compression-derived']
        )
        assert ev.tags.count('compression-derived') == 1

    def test_confidence_default_from_artifact(self, db):
        art_id = _make_active_artifact(db, compression_confidence=5)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.confidence == 5

    def test_confidence_default_fallback_3_when_none(self, db):
        # Create artifact without compression_confidence
        asm_id = _insert_assembly(db)
        art = create_compression_artifact(
            db_path=db,
            source_assembly_id=asm_id,
            compression_method='m',
            producer_version='0.0',
            artifact_text='text',
            created_by='tester',
            compression_confidence=None,
        )
        promote_compression_artifact(db, art.id, promoted_by='q', promotion_notes='ok')
        ev = seed_memory_from_compression(
            db, art.id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.confidence == 3

    def test_confidence_override(self, db):
        art_id = _make_active_artifact(db, compression_confidence=5)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T',
            confidence=2
        )
        assert ev.confidence == 2

    def test_summary_is_artifact_text(self, db):
        art_id = _make_active_artifact(db, artifact_text='My artifact body.')
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.summary == 'My artifact body.'


# ---------------------------------------------------------------------------
# TestSeedMemoryTruncation
# ---------------------------------------------------------------------------

class TestSeedMemoryTruncation:
    def test_short_text_verbatim(self, db):
        short = 'x' * 100
        assert _truncate_artifact_text(short) == short

    def test_exactly_at_limit_verbatim(self, db):
        text = 'a' * _SUMMARY_TRUNCATION_LIMIT
        assert _truncate_artifact_text(text) == text

    def test_long_text_truncated_at_limit(self, db):
        text = 'b' * (_SUMMARY_TRUNCATION_LIMIT + 500)
        result = _truncate_artifact_text(text)
        # First _SUMMARY_TRUNCATION_LIMIT chars come from original
        assert result[:_SUMMARY_TRUNCATION_LIMIT] == text[:_SUMMARY_TRUNCATION_LIMIT]

    def test_long_text_has_suffix(self, db):
        text = 'c' * (_SUMMARY_TRUNCATION_LIMIT + 1)
        result = _truncate_artifact_text(text)
        assert result.endswith(_TRUNCATION_SUFFIX)

    def test_suffix_appears_exactly_once(self, db):
        text = 'd' * (_SUMMARY_TRUNCATION_LIMIT + 100)
        result = _truncate_artifact_text(text)
        assert result.count(_TRUNCATION_SUFFIX) == 1

    def test_truncated_length_is_limit_plus_suffix(self, db):
        text = 'e' * (_SUMMARY_TRUNCATION_LIMIT + 999)
        result = _truncate_artifact_text(text)
        assert len(result) == _SUMMARY_TRUNCATION_LIMIT + len(_TRUNCATION_SUFFIX)

    def test_source_artifact_not_mutated(self, db):
        long_text = 'f' * (_SUMMARY_TRUNCATION_LIMIT + 200)
        art_id = _make_active_artifact(db, artifact_text=long_text)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        # Re-fetch artifact from DB
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT artifact_text FROM compression_artifacts WHERE id = ?",
                           (art_id,)).fetchone()
        conn.close()
        assert row['artifact_text'] == long_text

    def test_long_text_summary_stored_in_memory_event(self, db):
        long_text = 'g' * (_SUMMARY_TRUNCATION_LIMIT + 50)
        art_id = _make_active_artifact(db, artifact_text=long_text)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.summary.endswith(_TRUNCATION_SUFFIX)
        assert len(ev.summary) == _SUMMARY_TRUNCATION_LIMIT + len(_TRUNCATION_SUFFIX)


# ---------------------------------------------------------------------------
# TestSeedMemoryEvidenceJSON
# ---------------------------------------------------------------------------

class TestSeedMemoryEvidenceJSON:
    def test_evidence_is_valid_json(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        assert isinstance(parsed, dict)

    def test_evidence_byte_stable(self, db):
        # _build_evidence_json produces identical output on repeated calls with same inputs
        from memory.compression import get_compression_artifact
        art_id = _make_active_artifact(db)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        from memory.compression import _row_to_artifact
        row = conn.execute("SELECT * FROM compression_artifacts WHERE id = ?",
                           (art_id,)).fetchone()
        conn.close()
        artifact = _row_to_artifact(row)

        result1 = _build_evidence_json(artifact, 'op', 'reason')
        result2 = _build_evidence_json(artifact, 'op', 'reason')
        assert result1 == result2

    def test_evidence_source_memory_event_ids_sorted(self, db):
        ev1 = _add_event(db)
        ev2 = _add_event(db)
        ev3 = _add_event(db)
        # Pass in reverse order to verify sorting
        art_id = _make_active_artifact(db, memory_ids=[ev3, ev1, ev2])
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        ids = parsed['source_memory_event_ids']
        assert ids == sorted(ids)

    def test_evidence_contradiction_link_ids_sorted(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        ids = parsed['source_contradiction_link_ids']
        assert ids == sorted(ids)

    def test_evidence_contains_required_fields(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'my-reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        required = {
            'compression_artifact_id', 'compression_method', 'producer_version',
            'source_assembly_id', 'source_assembly_hash', 'source_memory_event_ids',
            'source_memory_event_count', 'source_contradiction_link_ids',
            'seeded_by', 'seeded_reason',
        }
        assert required.issubset(set(parsed.keys()))

    def test_evidence_seeded_by_equals_operator(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'quant-analyst', 'reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        assert parsed['seeded_by'] == 'quant-analyst'

    def test_evidence_seeded_reason_stored(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'my seeding reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        assert parsed['seeded_reason'] == 'my seeding reason'

    def test_evidence_artifact_id_matches(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        assert parsed['compression_artifact_id'] == art_id

    def test_evidence_source_memory_event_count_matches_ids(self, db):
        ev1 = _add_event(db)
        ev2 = _add_event(db)
        art_id = _make_active_artifact(db, memory_ids=[ev1, ev2])
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        parsed = json.loads(ev.evidence)
        assert parsed['source_memory_event_count'] == len(parsed['source_memory_event_ids'])


# ---------------------------------------------------------------------------
# TestSeedMemoryDerivedFromLinks
# ---------------------------------------------------------------------------

class TestSeedMemoryDerivedFromLinks:
    def test_links_created_for_existing_source_events(self, db):
        ev1 = _add_event(db)
        ev2 = _add_event(db)
        art_id = _make_active_artifact(db, memory_ids=[ev1, ev2])
        new_ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        links = conn.execute(
            "SELECT * FROM memory_links WHERE source_id = ? AND relationship = 'derived_from'",
            (new_ev.id,)
        ).fetchall()
        conn.close()
        linked_targets = {r['target_id'] for r in links}
        assert ev1 in linked_targets
        assert ev2 in linked_targets

    def test_missing_source_events_skipped_gracefully(self, db):
        # Artifact references source event ids that don't exist — should not raise
        asm_id = _insert_assembly(db, memory_ids=[9001, 9002])
        art = create_compression_artifact(
            db_path=db,
            source_assembly_id=asm_id,
            compression_method='m',
            producer_version='0.0',
            artifact_text='text',
            created_by='tester',
        )
        promote_compression_artifact(db, art.id, promoted_by='q', promotion_notes='ok')
        # Force source_memory_event_ids to include nonexistent ids
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE compression_artifacts SET source_memory_event_ids_json = ? WHERE id = ?",
            (json.dumps([9001, 9002]), art.id)
        )
        conn.commit()
        conn.close()

        new_ev = seed_memory_from_compression(
            db, art.id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        # No links created (source events don't exist) — no error raised
        conn = sqlite3.connect(db)
        link_count = conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE source_id = ?", (new_ev.id,)
        ).fetchone()[0]
        conn.close()
        assert link_count == 0

    def test_links_have_derived_from_relationship(self, db):
        src = _add_event(db)
        art_id = _make_active_artifact(db, memory_ids=[src])
        new_ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        link = conn.execute(
            "SELECT relationship FROM memory_links WHERE source_id = ? AND target_id = ?",
            (new_ev.id, src)
        ).fetchone()
        conn.close()
        assert link is not None
        assert link['relationship'] == 'derived_from'


# ---------------------------------------------------------------------------
# TestSeedMemoryIdempotency
# ---------------------------------------------------------------------------

class TestSeedMemoryIdempotency:
    def test_second_call_raises_memory_seed_exception(self, db):
        art_id = _make_active_artifact(db)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        with pytest.raises(MemorySeedException):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_exception_carries_correct_existing_event_id(self, db):
        art_id = _make_active_artifact(db)
        first_ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        with pytest.raises(MemorySeedException) as exc_info:
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )
        assert exc_info.value.existing_memory_event_id == first_ev.id

    def test_no_extra_row_created_on_second_call(self, db):
        art_id = _make_active_artifact(db)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        count_before = _row_count(db, 'memory_events')
        with pytest.raises(MemorySeedException):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )
        assert _row_count(db, 'memory_events') == count_before

    def test_memory_seed_exception_is_subclass_of_value_error(self, db):
        art_id = _make_active_artifact(db)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        with pytest.raises(ValueError):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_source_key_is_idempotency_anchor(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.source == _compression_source_key(art_id)


# ---------------------------------------------------------------------------
# TestSeedMemoryStatusGuards
# ---------------------------------------------------------------------------

class TestSeedMemoryStatusGuards:
    def test_active_artifact_allowed(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        assert ev.status == 'proposed'

    def test_candidate_artifact_rejected(self, db):
        art_id = _make_candidate_artifact(db)
        with pytest.raises(ValueError, match="candidate"):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_candidate_rejection_message_mentions_promote(self, db):
        art_id = _make_candidate_artifact(db)
        with pytest.raises(ValueError, match="promote_compression_artifact"):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_superseded_artifact_rejected(self, db):
        """Explicit test: superseded compression artifact cannot seed memory."""
        # Create two active artifacts; supersede the first with the second
        art1_id = _make_active_artifact(db)
        art2_id = _make_active_artifact(db)
        supersede_compression_artifact(
            db_path=db,
            artifact_id=art1_id,
            superseded_by_id=art2_id,
            reason='replaced by newer',
            superseded_by_operator='quant',
        )
        # Verify artifact1 is now superseded
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM compression_artifacts WHERE id = ?",
                           (art1_id,)).fetchone()
        conn.close()
        assert row['status'] == 'superseded'

        with pytest.raises(ValueError, match="superseded"):
            seed_memory_from_compression(
                db, art1_id, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_superseded_rejection_message_is_explicit(self, db):
        art1_id = _make_active_artifact(db)
        art2_id = _make_active_artifact(db)
        supersede_compression_artifact(
            db=db, artifact_id=art1_id, superseded_by_id=art2_id,
            reason='superseded', superseded_by_operator='quant',
        ) if False else supersede_compression_artifact(
            db_path=db, artifact_id=art1_id, superseded_by_id=art2_id,
            reason='superseded', superseded_by_operator='quant',
        )
        with pytest.raises(ValueError, match="superseded artifacts cannot seed"):
            seed_memory_from_compression(
                db, art1_id, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_invalidated_artifact_rejected(self, db):
        art_id = _make_candidate_artifact(db)
        invalidate_compression_artifact(
            db_path=db,
            artifact_id=art_id,
            reason='stale',
            invalidated_by='tester',
        )
        with pytest.raises(ValueError, match="invalidated"):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )

    def test_non_active_status_rejection_includes_artifact_id(self, db):
        art_id = _make_candidate_artifact(db)
        with pytest.raises(ValueError, match=str(art_id)):
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )


# ---------------------------------------------------------------------------
# TestListCompressionDerivedMemory
# ---------------------------------------------------------------------------

class TestListCompressionDerivedMemory:
    def test_empty_db_returns_empty_list(self, db):
        result = list_compression_derived_memory(db)
        assert result == []

    def test_returns_seeded_events(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        events = list_compression_derived_memory(db)
        assert len(events) == 1
        assert events[0].id == ev.id

    def test_excludes_non_compression_events(self, db):
        # Add a regular memory event
        _add_event(db)
        events = list_compression_derived_memory(db)
        assert events == []

    def test_status_filter_proposed(self, db):
        art_id = _make_active_artifact(db)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        events = list_compression_derived_memory(db, status='proposed')
        assert len(events) == 1

    def test_status_filter_excludes_other(self, db):
        art_id = _make_active_artifact(db)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        events = list_compression_derived_memory(db, status='active')
        assert events == []

    def test_limit_honored(self, db):
        for _ in range(5):
            art_id = _make_active_artifact(db)
            seed_memory_from_compression(
                db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
            )
        events = list_compression_derived_memory(db, limit=3)
        assert len(events) == 3

    def test_ordered_most_recent_first(self, db):
        art1 = _make_active_artifact(db)
        ev1 = seed_memory_from_compression(
            db, art1, 'op', 'reason', event_type='hypothesis', title='T1'
        )
        art2 = _make_active_artifact(db)
        ev2 = seed_memory_from_compression(
            db, art2, 'op', 'reason', event_type='hypothesis', title='T2'
        )
        events = list_compression_derived_memory(db)
        assert events[0].id > events[1].id


# ---------------------------------------------------------------------------
# TestSeedMemoryCLI
# ---------------------------------------------------------------------------

class TestSeedMemoryCLI:
    def test_cli_create_prints_event_id(self, db, capsys):
        art_id = _make_active_artifact(db)
        _run_cli([
            'seed-memory-from-compression', '--db', db,
            '--artifact-id', str(art_id),
            '--operator', 'cli-op',
            '--reason', 'cli reason',
            '--event-type', 'hypothesis',
            '--title', 'CLI Title',
        ])
        out = capsys.readouterr().out
        assert 'created memory event id=' in out
        assert 'status=proposed' in out

    def test_cli_create_reports_id_in_output(self, db, capsys):
        art_id = _make_active_artifact(db)
        _run_cli([
            'seed-memory-from-compression', '--db', db,
            '--artifact-id', str(art_id),
            '--operator', 'op',
            '--reason', 'reason',
            '--event-type', 'hypothesis',
            '--title', 'T',
        ])
        out = capsys.readouterr().out
        import re
        match = re.search(r'id=(\d+)', out)
        assert match is not None
        event_id = int(match.group(1))
        assert event_id > 0

    def test_cli_non_active_artifact_exits(self, db, capsys):
        art_id = _make_candidate_artifact(db)
        with pytest.raises(SystemExit) as exc_info:
            _run_cli([
                'seed-memory-from-compression', '--db', db,
                '--artifact-id', str(art_id),
                '--operator', 'op',
                '--reason', 'reason',
                '--event-type', 'hypothesis',
                '--title', 'T',
            ])
        assert exc_info.value.code != 0

    def test_cli_already_seeded_exits(self, db, capsys):
        art_id = _make_active_artifact(db)
        _run_cli([
            'seed-memory-from-compression', '--db', db,
            '--artifact-id', str(art_id),
            '--operator', 'op',
            '--reason', 'reason',
            '--event-type', 'hypothesis',
            '--title', 'T',
        ])
        capsys.readouterr()  # consume first output
        with pytest.raises(SystemExit) as exc_info:
            _run_cli([
                'seed-memory-from-compression', '--db', db,
                '--artifact-id', str(art_id),
                '--operator', 'op',
                '--reason', 'reason',
                '--event-type', 'hypothesis',
                '--title', 'T',
            ])
        assert exc_info.value.code != 0

    def test_cli_list_compression_derived_empty(self, db, capsys):
        _run_cli(['list-compression-derived-memory', '--db', db])
        out = capsys.readouterr().out
        assert 'No compression-derived memory events found.' in out

    def test_cli_list_compression_derived_with_results(self, db, capsys):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='CLI Listed Title'
        )
        _run_cli(['list-compression-derived-memory', '--db', db])
        out = capsys.readouterr().out
        assert str(ev.id) in out
        assert 'CLI Listed Title' in out

    def test_cli_list_status_filter(self, db, capsys):
        art_id = _make_active_artifact(db)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _run_cli(['list-compression-derived-memory', '--db', db, '--status', 'active'])
        out = capsys.readouterr().out
        assert 'No compression-derived memory events found.' in out


# ---------------------------------------------------------------------------
# TestGovernanceDetectorCompressionDerived
# ---------------------------------------------------------------------------

class TestGovernanceDetectorCompressionDerived:
    def test_empty_db_no_issues(self, db):
        issues = detect_unreviewed_compression_derived_memory(db)
        assert issues == []

    def test_recent_proposed_not_flagged(self, db):
        art_id = _make_active_artifact(db)
        seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert issues == []

    def test_stale_proposed_flagged(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='Stale Candidate'
        )
        _backdate_memory_event(db, ev.id, days=15)  # one day past 14-day threshold
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert len(issues) == 1
        assert issues[0].memory_id == ev.id

    def test_stale_proposed_issue_type(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=15)
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert issues[0].issue_type == 'unreviewed_compression_derived_memory'

    def test_stale_proposed_issue_severity_is_warning(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=15)
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert issues[0].severity == 'warning'

    def test_non_proposed_status_not_flagged(self, db):
        # Add a manual memory event with source like compression_artifact but non-proposed status
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
        conn.execute(
            """INSERT INTO memory_events
               (event_type, title, summary, evidence, source, confidence, status,
                tags_json, related_ids_json, created_by, created_at, updated_at, version)
               VALUES ('hypothesis','T','s',NULL,'compression_artifact:999',3,'active',
                       '[]','[]','tester',?,?,1)""",
            (old, now),
        )
        conn.commit()
        conn.close()
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert issues == []

    def test_non_compression_source_not_flagged(self, db):
        # A proposed event with a non-compression source should not be flagged
        conn = sqlite3.connect(db)
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
        conn.execute(
            """INSERT INTO memory_events
               (event_type, title, summary, evidence, source, confidence, status,
                tags_json, related_ids_json, created_by, created_at, updated_at, version)
               VALUES ('hypothesis','T','s',NULL,'manual-source',3,'proposed',
                       '[]','[]','tester',?,?,1)""",
            (old, old),
        )
        conn.commit()
        conn.close()
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert issues == []

    def test_at_threshold_boundary_not_flagged(self, db):
        """Exactly at the warning_days threshold: not yet flagged (strict <)."""
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=14)  # exactly at threshold
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert issues == []

    def test_one_past_threshold_flagged(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=15)  # one day past threshold
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert len(issues) == 1

    def test_metadata_contains_expected_fields(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=15)
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        meta = issues[0].metadata
        assert 'memory_event_id' in meta
        assert 'source_compression_artifact_id' in meta
        assert 'source_key' in meta
        assert 'days_old' in meta

    def test_metadata_source_compression_artifact_id_matches(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=15)
        issues = detect_unreviewed_compression_derived_memory(db, warning_days=14)
        assert issues[0].metadata['source_compression_artifact_id'] == art_id

    def test_wired_into_governance_report(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=15)
        report = build_governance_report(db, compression_memory_warning_days=14)
        issue_types = [i.issue_type for i in report.issues]
        assert 'unreviewed_compression_derived_memory' in issue_types

    def test_detect_compression_memory_issues_false_skips_detector(self, db):
        art_id = _make_active_artifact(db)
        ev = seed_memory_from_compression(
            db, art_id, 'op', 'reason', event_type='hypothesis', title='T'
        )
        _backdate_memory_event(db, ev.id, days=15)
        report = build_governance_report(
            db,
            compression_memory_warning_days=14,
            detect_compression_memory_issues=False,
        )
        issue_types = [i.issue_type for i in report.issues]
        assert 'unreviewed_compression_derived_memory' not in issue_types
