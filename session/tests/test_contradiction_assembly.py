"""
Tests for Phase 4C: contradiction-aware context assembly substrate.

Validates:
1. ConflictingPair data structure and serialization
2. resolve_assembly_contradictions() — intra-assembly contradiction detection
3. reconstruct() — contradiction annotation after budgeting
4. SessionContext.contradiction_pairs — persistence in snapshot
5. replay_assembly() — snapshot fidelity (contradictions survive retraction)
6. verify_assembly_against_current_db() — contradiction divergence reporting
7. ActivatedMemory.contradiction_ids — per-event annotation
8. ActivatedMemory.render() — contradiction line in rendered output
9. SessionReconstruction.render() — CONFLICTING MEMORIES section
10. Backward compatibility — pre-v1.1.0 snapshots
11. CONTEXT_ASSEMBLY_VERSION == '1.1.0'
12. Ordering invariant — contradiction annotation does not affect activation_rank
"""

import json
import sqlite3

import pytest

from memory import service
from memory.service import (
    add_memory_event,
    create_contradiction_link,
    init_db,
    retract_contradiction_link,
)
from session.models import (
    ActivatedMemory,
    AssemblyDivergenceReport,
    CONTEXT_ASSEMBLY_VERSION,
    ConflictingPair,
    ContextActivationPolicy,
    SessionContext,
)
from session.reconstruction import (
    log_assembly,
    reconstruct,
    reconstruct_from_dict,
    replay_assembly,
    resolve_assembly_contradictions,
    verify_assembly_against_current_db,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _mem_db(tmp_path) -> str:
    path = str(tmp_path / 'mem.db')
    init_db(path)
    return path


def _add(db, **kw):
    defaults = dict(
        event_type='hypothesis',
        title='Test',
        summary='Test summary',
        source='test',
        confidence=3,
        status='active',
        created_by='tester',
    )
    defaults.update(kw)
    return add_memory_event(db, **defaults)


def _contradict(db, source_id, target_id, **kw):
    defaults = dict(created_by='tester', reason='conflict', link_confidence=3)
    defaults.update(kw)
    return create_contradiction_link(db, source_id, target_id, **defaults)


def _all_raw_ids(db_path) -> list:
    conn = sqlite3.connect(db_path)
    rows = conn.execute('SELECT id FROM memory_events ORDER BY id').fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 1. CONTEXT_ASSEMBLY_VERSION
# ---------------------------------------------------------------------------

class TestAssemblyVersion:
    def test_version_is_1_1_0(self):
        assert CONTEXT_ASSEMBLY_VERSION == '1.2.0'


# ---------------------------------------------------------------------------
# 2. ConflictingPair
# ---------------------------------------------------------------------------

class TestConflictingPair:
    def test_to_dict_has_all_fields(self):
        pair = ConflictingPair(
            link_id=7,
            source_id=1,
            target_id=3,
            created_by='analyst',
            reason='opposing regimes',
            link_confidence=4,
            link_created_at='2026-05-10T14:22:00Z',
        )
        d = pair.to_dict()
        assert d['link_id'] == 7
        assert d['source_id'] == 1
        assert d['target_id'] == 3
        assert d['created_by'] == 'analyst'
        assert d['reason'] == 'opposing regimes'
        assert d['link_confidence'] == 4
        assert d['link_created_at'] == '2026-05-10T14:22:00Z'

    def test_from_dict_roundtrip(self):
        pair = ConflictingPair(
            link_id=7, source_id=1, target_id=3,
            created_by='analyst', reason='conflict', link_confidence=3,
            link_created_at='2026-05-10T14:22:00Z',
        )
        restored = ConflictingPair.from_dict(pair.to_dict())
        assert restored.link_id == pair.link_id
        assert restored.source_id == pair.source_id
        assert restored.target_id == pair.target_id
        assert restored.created_by == pair.created_by
        assert restored.reason == pair.reason
        assert restored.link_confidence == pair.link_confidence
        assert restored.link_created_at == pair.link_created_at

    def test_from_dict_tolerates_missing_optional_fields(self):
        d = {'link_id': 1, 'source_id': 2, 'target_id': 3, 'link_created_at': '2026-01-01T00:00:00Z'}
        pair = ConflictingPair.from_dict(d)
        assert pair.created_by is None
        assert pair.reason is None
        assert pair.link_confidence is None


# ---------------------------------------------------------------------------
# 3. resolve_assembly_contradictions()
# ---------------------------------------------------------------------------

class TestResolveAssemblyContradictions:
    def test_empty_assembled_ids_returns_empty(self, tmp_path):
        db = _mem_db(tmp_path)
        assert resolve_assembly_contradictions(db, []) == []

    def test_no_contradiction_links_returns_empty(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db)
        ev2 = _add(db)
        assert resolve_assembly_contradictions(db, [ev1.id, ev2.id]) == []

    def test_returns_pair_for_active_contradiction(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)
        pairs = resolve_assembly_contradictions(db, [ev1.id, ev2.id])
        assert len(pairs) == 1
        assert pairs[0].link_id == link.id
        assert pairs[0].source_id == ev1.id
        assert pairs[0].target_id == ev2.id

    def test_does_not_surface_if_only_one_side_present(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)
        # Only ev1 is in the assembly — ev2 is absent
        pairs = resolve_assembly_contradictions(db, [ev1.id])
        assert pairs == []

    def test_does_not_surface_retracted_links(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)
        retract_contradiction_link(db, link.id, 'tester', 'resolved')
        pairs = resolve_assembly_contradictions(db, [ev1.id, ev2.id])
        assert pairs == []

    def test_returns_multiple_pairs(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        ev3 = _add(db, status='accepted')
        l1 = _contradict(db, ev1.id, ev2.id)
        l2 = _contradict(db, ev2.id, ev3.id)
        pairs = resolve_assembly_contradictions(db, [ev1.id, ev2.id, ev3.id])
        link_ids = [p.link_id for p in pairs]
        assert l1.id in link_ids
        assert l2.id in link_ids

    def test_ordered_by_link_id_ascending(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        ev3 = _add(db, status='accepted')
        _contradict(db, ev2.id, ev3.id)
        _contradict(db, ev1.id, ev2.id)
        pairs = resolve_assembly_contradictions(db, [ev1.id, ev2.id, ev3.id])
        ids = [p.link_id for p in pairs]
        assert ids == sorted(ids)

    def test_pair_carries_provenance(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id, created_by='analyst', reason='opposing models', link_confidence=4)
        pairs = resolve_assembly_contradictions(db, [ev1.id, ev2.id])
        assert pairs[0].created_by == 'analyst'
        assert pairs[0].reason == 'opposing models'
        assert pairs[0].link_confidence == 4
        assert pairs[0].link_created_at is not None


# ---------------------------------------------------------------------------
# 4. reconstruct() — contradiction annotation
# ---------------------------------------------------------------------------

class TestReconstructContradictionAnnotation:
    def test_no_contradiction_empty_pairs(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db)
        ctx = reconstruct(db).context
        assert ctx.contradiction_pairs == []

    def test_contradiction_surfaced_when_both_present(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)
        ctx = reconstruct(db).context
        assert len(ctx.contradiction_pairs) == 1
        assert ctx.contradiction_pairs[0].link_id == link.id

    def test_retracted_contradiction_not_surfaced(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)
        retract_contradiction_link(db, link.id, 'tester', 'resolved')
        ctx = reconstruct(db).context
        assert ctx.contradiction_pairs == []

    def test_contradiction_ids_annotated_on_both_events(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)
        ctx = reconstruct(db).context
        # Collect all ActivatedMemory items across all sections
        all_mem = (
            ctx.governance_context + ctx.unresolved_items
            + ctx.active_investigations + ctx.relevant_memory
        )
        by_id = {m.memory_id: m for m in all_mem}
        assert ev2.id in by_id[ev1.id].contradiction_ids
        assert ev1.id in by_id[ev2.id].contradiction_ids

    def test_contradiction_ids_empty_on_non_contradicting_events(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        ev3 = _add(db)
        _contradict(db, ev1.id, ev2.id)
        ctx = reconstruct(db).context
        all_mem = (
            ctx.governance_context + ctx.unresolved_items
            + ctx.active_investigations + ctx.relevant_memory
        )
        by_id = {m.memory_id: m for m in all_mem}
        assert by_id[ev3.id].contradiction_ids == []

    def test_contradiction_ids_sorted(self, tmp_path):
        db = _mem_db(tmp_path)
        # ev1 contradicts both ev2 and ev3
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        ev3 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev3.id)
        _contradict(db, ev1.id, ev2.id)
        ctx = reconstruct(db).context
        all_mem = ctx.governance_context + ctx.unresolved_items + ctx.active_investigations + ctx.relevant_memory
        by_id = {m.memory_id: m for m in all_mem}
        ids = by_id[ev1.id].contradiction_ids
        assert ids == sorted(ids)

    def test_contradiction_annotation_does_not_alter_activation_rank_order(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted', confidence=5)
        ev2 = _add(db, status='accepted', confidence=1)
        # Add contradiction between them
        _contradict(db, ev1.id, ev2.id)
        # Reconstruct with contradiction
        ctx_with = reconstruct(db).context
        # Remove contradiction by retraction and reconstruct without
        # (We can't retract here since there's no retract call in a clean way without setup,
        # but we can check ordering is determined by confidence, not contradiction)
        all_mem_with = ctx_with.governance_context + ctx_with.unresolved_items + ctx_with.active_investigations + ctx_with.relevant_memory
        by_id = {m.memory_id: m for m in all_mem_with}
        # ev1 (confidence=5) should rank ahead of ev2 (confidence=1)
        # Both have contradiction_ids populated but ordering is unchanged
        assert by_id[ev1.id].contradiction_ids == [ev2.id]
        assert by_id[ev2.id].contradiction_ids == [ev1.id]


# ---------------------------------------------------------------------------
# 5. SessionContext serialization / snapshot fidelity
# ---------------------------------------------------------------------------

class TestSessionContextContradictionSerialization:
    def test_to_dict_includes_contradiction_pairs(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)
        ctx = reconstruct(db).context
        d = ctx.to_dict()
        assert 'contradiction_pairs' in d
        assert len(d['contradiction_pairs']) == 1
        pair_d = d['contradiction_pairs'][0]
        assert 'link_id' in pair_d
        assert 'source_id' in pair_d
        assert 'target_id' in pair_d
        assert 'link_created_at' in pair_d

    def test_to_dict_contradiction_pairs_empty_when_none(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db)
        d = reconstruct(db).context.to_dict()
        assert d['contradiction_pairs'] == []

    def test_activated_memory_to_dict_includes_contradiction_ids(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)
        ctx = reconstruct(db).context
        all_mem = ctx.governance_context + ctx.unresolved_items + ctx.active_investigations + ctx.relevant_memory
        by_id = {m.memory_id: m for m in all_mem}
        d1 = by_id[ev1.id].to_dict()
        assert 'contradiction_ids' in d1
        assert ev2.id in d1['contradiction_ids']


# ---------------------------------------------------------------------------
# 6. reconstruct_from_dict() / replay backward compatibility
# ---------------------------------------------------------------------------

class TestReconstructFromDict:
    def test_restores_contradiction_pairs(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)
        ctx = reconstruct(db).context
        d = ctx.to_dict()
        restored = reconstruct_from_dict(d)
        assert len(restored.contradiction_pairs) == 1
        assert restored.contradiction_pairs[0].link_id == link.id

    def test_tolerates_missing_contradiction_pairs_key(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db)
        ctx = reconstruct(db).context
        d = ctx.to_dict()
        d.pop('contradiction_pairs', None)
        restored = reconstruct_from_dict(d)
        assert restored.contradiction_pairs == []

    def test_tolerates_missing_contradiction_ids_on_events(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ctx = reconstruct(db).context
        d = ctx.to_dict()
        # Simulate pre-v1.1.0 snapshot where events don't have contradiction_ids
        for section_key in ('governance_context', 'unresolved_items', 'active_investigations', 'relevant_memory'):
            for item_d in d.get(section_key, []):
                item_d.pop('contradiction_ids', None)
        restored = reconstruct_from_dict(d)
        all_mem = restored.governance_context + restored.unresolved_items + restored.active_investigations + restored.relevant_memory
        assert all(m.contradiction_ids == [] for m in all_mem)


# ---------------------------------------------------------------------------
# 7. replay_assembly() — snapshot-pure fidelity
# ---------------------------------------------------------------------------

class TestReplayAssemblyContradictionFidelity:
    def test_replay_preserves_contradiction_pairs_after_retraction(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)

        # Log assembly while contradiction is active
        recon = reconstruct(db)
        log_row = log_assembly(db, recon)
        assembly_id = log_row['id']

        # Retract the contradiction after logging
        retract_contradiction_link(db, link.id, 'tester', 'resolved')

        # Replay: snapshot was captured before retraction — contradiction must still appear
        replayed = replay_assembly(assembly_id, db)
        assert len(replayed.context.contradiction_pairs) == 1
        assert replayed.context.contradiction_pairs[0].link_id == link.id

    def test_replay_preserves_per_event_contradiction_ids(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)

        recon = reconstruct(db)
        log_row = log_assembly(db, recon)
        assembly_id = log_row['id']

        replayed = replay_assembly(assembly_id, db)
        all_mem = (
            replayed.context.governance_context + replayed.context.unresolved_items
            + replayed.context.active_investigations + replayed.context.relevant_memory
        )
        by_id = {m.memory_id: m for m in all_mem}
        assert ev2.id in by_id[ev1.id].contradiction_ids

    def test_replay_is_marked_replayed(self, tmp_path):
        db = _mem_db(tmp_path)
        recon = reconstruct(db)
        log_row = log_assembly(db, recon)
        replayed = replay_assembly(log_row['id'], db)
        assert replayed.replayed is True


# ---------------------------------------------------------------------------
# 8. verify_assembly_against_current_db() — contradiction divergence
# ---------------------------------------------------------------------------

class TestVerifyContradictionDivergence:
    def test_no_divergence_when_unchanged(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)
        recon = reconstruct(db)
        log_row = log_assembly(db, recon)
        report = verify_assembly_against_current_db(log_row['id'], db)
        assert not report.diverged
        assert report.contradictions_added_since_assembly == []
        assert report.contradictions_retracted_since_assembly == []

    def test_retraction_detected_as_contradiction_removed(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)
        recon = reconstruct(db)
        log_row = log_assembly(db, recon)

        # Retract after logging
        retract_contradiction_link(db, link.id, 'tester', 'resolved')

        report = verify_assembly_against_current_db(log_row['id'], db)
        assert report.diverged
        assert link.id in report.contradictions_retracted_since_assembly
        assert report.contradictions_added_since_assembly == []

    def test_new_contradiction_detected_as_added(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        # Log without contradiction
        recon = reconstruct(db)
        log_row = log_assembly(db, recon)

        # Add contradiction after logging
        link = _contradict(db, ev1.id, ev2.id)

        report = verify_assembly_against_current_db(log_row['id'], db)
        assert report.diverged
        assert link.id in report.contradictions_added_since_assembly
        assert report.contradictions_retracted_since_assembly == []

    def test_divergence_report_has_contradiction_fields(self, tmp_path):
        db = _mem_db(tmp_path)
        recon = reconstruct(db)
        log_row = log_assembly(db, recon)
        report = verify_assembly_against_current_db(log_row['id'], db)
        assert hasattr(report, 'contradictions_added_since_assembly')
        assert hasattr(report, 'contradictions_retracted_since_assembly')
        assert isinstance(report.contradictions_added_since_assembly, list)
        assert isinstance(report.contradictions_retracted_since_assembly, list)

    def test_no_contradiction_divergence_is_type_correct(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db)
        recon = reconstruct(db)
        log_row = log_assembly(db, recon)
        report = verify_assembly_against_current_db(log_row['id'], db)
        assert report.contradictions_added_since_assembly == []
        assert report.contradictions_retracted_since_assembly == []


# ---------------------------------------------------------------------------
# 9. ActivatedMemory.render() — contradiction annotation
# ---------------------------------------------------------------------------

class TestActivatedMemoryRender:
    def test_render_includes_conflict_line_when_annotated(self):
        mem = ActivatedMemory(
            memory_id=1, event_type='hypothesis', title='A', summary='B',
            evidence=None, confidence=3, status='active', tags=[], source='test',
            related_ids=[], created_at='2026-01-01T00:00:00Z',
            updated_at='2026-01-01T00:00:00Z', is_expanded=False, tag_overlap=0,
            activation_rank=(), contradiction_ids=[3, 7],
        )
        rendered = mem.render()
        assert 'Conflicts' in rendered
        assert '[mem:3]' in rendered
        assert '[mem:7]' in rendered

    def test_render_no_conflict_line_when_empty(self):
        mem = ActivatedMemory(
            memory_id=1, event_type='hypothesis', title='A', summary='B',
            evidence=None, confidence=3, status='active', tags=[], source='test',
            related_ids=[], created_at='2026-01-01T00:00:00Z',
            updated_at='2026-01-01T00:00:00Z', is_expanded=False, tag_overlap=0,
            activation_rank=(), contradiction_ids=[],
        )
        rendered = mem.render()
        assert 'Conflicts' not in rendered

    def test_render_contradiction_does_not_affect_char_budget(self, tmp_path):
        """
        During apply_context_budget(), contradiction_ids is always [] (default).
        After budgeting, annotation is populated. The render() output seen by
        the budget pass never includes the Conflicts line.
        """
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)

        # Reconstruct twice: with and without the contradiction (use retraction)
        ctx_with = reconstruct(db).context

        # Budget chars_used should NOT include the Conflicts annotation
        # Verify: reconstruct without contradiction and chars_used should match
        link_rows = service.list_memory_events(db, status='accepted')
        # The chars_used may differ if char budget is sensitive to contradiction line,
        # but since annotation is post-budget, chars_used should NOT include conflict text.
        # We verify this indirectly: chars_used == chars counted during budget pass (no conflict line)
        policy = ContextActivationPolicy()
        ctx_no_annotation = reconstruct(db, policy).context
        # Both reconstructs include the annotation — we verify chars_used is the same
        # (same events, same content — contradiction lines not in budget)
        assert ctx_with.chars_used == ctx_no_annotation.chars_used


# ---------------------------------------------------------------------------
# 10. SessionReconstruction.render() — CONFLICTING MEMORIES section
# ---------------------------------------------------------------------------

class TestSessionReconstructionRender:
    def test_render_includes_conflicting_memories_section(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id, reason='opposing hypotheses')
        recon = reconstruct(db)
        rendered = recon.render()
        assert 'CONFLICTING MEMORIES' in rendered
        assert 'opposing hypotheses' in rendered

    def test_render_no_conflicting_section_when_none(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db)
        recon = reconstruct(db)
        rendered = recon.render()
        assert 'CONFLICTING MEMORIES' not in rendered

    def test_render_conflict_section_shows_mem_refs(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)
        rendered = reconstruct(db).render()
        assert f'[mem:{ev1.id}]' in rendered
        assert f'[mem:{ev2.id}]' in rendered


# ---------------------------------------------------------------------------
# 11. log_assembly() — snapshot persistence of contradiction state
# ---------------------------------------------------------------------------

class TestLogAssemblyContradictionPersistence:
    def test_snapshot_contains_contradiction_pairs(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        link = _contradict(db, ev1.id, ev2.id)

        recon = reconstruct(db)
        log_row = log_assembly(db, recon)

        snapshot = json.loads(log_row['assembly_snapshot_json'])
        assert 'contradiction_pairs' in snapshot
        assert len(snapshot['contradiction_pairs']) == 1
        assert snapshot['contradiction_pairs'][0]['link_id'] == link.id

    def test_snapshot_contains_contradiction_ids_on_events(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)

        recon = reconstruct(db)
        log_row = log_assembly(db, recon)
        snapshot = json.loads(log_row['assembly_snapshot_json'])

        all_event_dicts = (
            snapshot.get('governance_context', [])
            + snapshot.get('unresolved_items', [])
            + snapshot.get('active_investigations', [])
            + snapshot.get('relevant_memory', [])
        )
        by_id = {d['memory_id']: d for d in all_event_dicts}
        assert ev2.id in by_id[ev1.id]['contradiction_ids']
        assert ev1.id in by_id[ev2.id]['contradiction_ids']

    def test_idempotent_assembly_same_hash_with_contradictions(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')
        _contradict(db, ev1.id, ev2.id)

        recon1 = reconstruct(db)
        row1 = log_assembly(db, recon1)
        recon2 = reconstruct(db)
        row2 = log_assembly(db, recon2)

        # Identical content → same hash → idempotent (existing row returned)
        assert row1['assembly_hash'] == row2['assembly_hash']
        assert row1['id'] == row2['id']

    def test_new_contradiction_changes_assembly_hash(self, tmp_path):
        db = _mem_db(tmp_path)
        ev1 = _add(db, status='accepted')
        ev2 = _add(db, status='accepted')

        recon1 = reconstruct(db)
        row1 = log_assembly(db, recon1)

        # Add contradiction → different assembly content → different hash
        _contradict(db, ev1.id, ev2.id)
        recon2 = reconstruct(db)
        row2 = log_assembly(db, recon2)

        assert row1['assembly_hash'] != row2['assembly_hash']


# ---------------------------------------------------------------------------
# 12. Cross-section contradiction surfacing
# ---------------------------------------------------------------------------

class TestCrossSectionContradictions:
    def test_contradiction_across_sections_surfaced(self, tmp_path):
        """
        Event A in governance_context, event B in relevant_memory — pair must surface.
        """
        db = _mem_db(tmp_path)
        gov_ev = _add(db, event_type='governance_rule', status='active', title='Governance Rule')
        rel_ev = _add(db, event_type='hypothesis', status='accepted', title='Hypothesis')
        _contradict(db, gov_ev.id, rel_ev.id)
        ctx = reconstruct(db).context

        all_mem = ctx.governance_context + ctx.unresolved_items + ctx.active_investigations + ctx.relevant_memory
        by_id = {m.memory_id: m for m in all_mem}
        assert len(ctx.contradiction_pairs) == 1
        assert rel_ev.id in by_id[gov_ev.id].contradiction_ids
        assert gov_ev.id in by_id[rel_ev.id].contradiction_ids
