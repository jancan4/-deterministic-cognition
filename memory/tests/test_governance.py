"""
Hermetic tests for memory/governance.py.

Timing trick: stale/aging detection compares timestamps to (now - N days).
Passing a negative threshold produces a future cutoff, making all current events
"old enough" to trigger detection without needing sleeps or timestamp mocking.

  warning_days=-1  →  cutoff = now+1d  →  all current events are stale
  critical_days=-2 →  cutoff = now+2d  →  all stale events are also critical
  warning_days=36500 → cutoff ~1926    →  no current events are stale
"""

import sqlite3

import pytest
from memory import service
from memory.governance import (
    GovernanceIssue,
    GovernanceReport,
    RetrievalFilter,
    build_governance_report,
    check_lineage_integrity,
    detect_adaptation_lineage_gap,
    detect_conflicts,
    detect_deprecated_linked,
    detect_duplicate_title,
    detect_excessive_fanout,
    detect_fired_decisions_without_assembly,
    detect_low_confidence_active,
    detect_missing_evidence,
    detect_orphaned_transitions,
    detect_orphans,
    detect_stale_memory,
    detect_unresolved_aging,
    filter_events,
)
from memory.retrieval import RetrievalQuery, ScoredEvent, retrieve


def _add(db, **kw):
    defaults = dict(
        event_type='hypothesis',
        title='Test',
        summary='Test summary',
        source='test',
        confidence=3,
        status='proposed',
        created_by='tester',
    )
    defaults.update(kw)
    return service.add_memory_event(db, **defaults)


# ---------------------------------------------------------------------------
# GovernanceIssue dataclass
# ---------------------------------------------------------------------------

class TestGovernanceIssue:
    def test_to_dict_has_required_fields(self, tmp_path):
        issue = GovernanceIssue(
            issue_type='stale_memory',
            severity='warning',
            memory_id=1,
            title='Some title',
            rationale='Because it is old.',
            recommended_action='Archive it.',
        )
        d = issue.to_dict()
        assert d['issue_type'] == 'stale_memory'
        assert d['severity'] == 'warning'
        assert d['memory_id'] == 1
        assert d['title'] == 'Some title'
        assert d['rationale'] == 'Because it is old.'
        assert d['recommended_action'] == 'Archive it.'

    def test_severity_values(self):
        for sev in ('info', 'warning', 'critical'):
            issue = GovernanceIssue('stale_memory', sev, 1, 'T', 'R', 'A')
            assert issue.to_dict()['severity'] == sev


# ---------------------------------------------------------------------------
# detect_stale_memory
# ---------------------------------------------------------------------------

class TestDetectStaleMemory:
    def test_no_stale_events_when_threshold_far_past(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active')
        issues = detect_stale_memory(db, warning_days=36500)
        assert issues == []

    def test_detects_active_stale(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active')
        issues = detect_stale_memory(db, warning_days=-1, critical_days=36500)
        assert len(issues) == 1
        assert issues[0].issue_type == 'stale_memory'
        assert issues[0].memory_id == 1

    def test_detects_proposed_stale(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed')
        issues = detect_stale_memory(db, warning_days=-1, critical_days=36500)
        assert len(issues) == 1

    def test_excludes_accepted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted')
        issues = detect_stale_memory(db, warning_days=-1, critical_days=36500)
        assert issues == []

    def test_excludes_archived(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='archived')
        issues = detect_stale_memory(db, warning_days=-1, critical_days=36500)
        assert issues == []

    def test_warning_severity(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active')
        # warning_days=-1 makes all events stale; critical_days=36500 (far past) = no critical
        issues = detect_stale_memory(db, warning_days=-1, critical_days=36500)
        assert issues[0].severity == 'warning'

    def test_critical_severity(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active')
        # Both cutoffs in future: warning_cutoff < critical_cutoff → all events are critical
        issues = detect_stale_memory(db, warning_days=-1, critical_days=-2)
        assert issues[0].severity == 'critical'

    def test_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', title='A')
        _add(db, status='active', title='B')
        issues = detect_stale_memory(db, warning_days=-1, critical_days=36500)
        assert [i.memory_id for i in issues] == [1, 2]

    def test_issue_has_title(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', title='My Hypothesis')
        issues = detect_stale_memory(db, warning_days=-1, critical_days=36500)
        assert issues[0].title == 'My Hypothesis'

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_stale_memory(db) == []


# ---------------------------------------------------------------------------
# detect_conflicts
# ---------------------------------------------------------------------------

class TestDetectConflicts:
    def test_no_conflicts_without_links(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted')
        _add(db, status='accepted')
        assert detect_conflicts(db) == []

    def test_detects_contradicts_link_between_active(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.create_contradiction_link(db, e1.id, e2.id, created_by='tester', reason='conflict', link_confidence=3)
        issues = detect_conflicts(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'conflicting_active'
        assert issues[0].severity == 'critical'

    def test_no_conflict_when_one_superseded(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.create_contradiction_link(db, e1.id, e2.id, created_by='tester', reason='conflict', link_confidence=3)
        service.update_status(db, e2.id, 'superseded', reason='test supersession', created_by='tester')
        assert detect_conflicts(db) == []

    def test_no_conflict_for_supports_link(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.link_memory_events(db, e1.id, e2.id, 'supports')
        assert detect_conflicts(db) == []

    def test_conflict_memory_id_is_source(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.create_contradiction_link(db, e1.id, e2.id, created_by='tester', reason='conflict', link_confidence=3)
        issues = detect_conflicts(db)
        assert issues[0].memory_id == e1.id

    def test_ordered_by_source_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        e3 = _add(db, status='accepted')
        service.create_contradiction_link(db, e2.id, e3.id, created_by='tester', reason='conflict', link_confidence=3)
        service.create_contradiction_link(db, e1.id, e3.id, created_by='tester', reason='conflict', link_confidence=3)
        issues = detect_conflicts(db)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_no_conflicts(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_conflicts(db) == []


# ---------------------------------------------------------------------------
# detect_orphans
# ---------------------------------------------------------------------------

class TestDetectOrphans:
    def test_lone_event_is_orphan(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        issues = detect_orphans(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'orphaned_event'
        assert issues[0].severity == 'info'

    def test_linked_event_not_orphan(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.link_memory_events(db, e1.id, e2.id, 'supports')
        issues = detect_orphans(db)
        assert issues == []

    def test_event_in_related_ids_not_orphan(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        _add(db, related_ids=[e1.id], status='accepted')
        issues = detect_orphans(db)
        assert issues == []

    def test_event_with_related_ids_not_orphan(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, related_ids=[e1.id], status='accepted')
        # e2 has related_ids so it has outbound refs — not orphan
        issues = detect_orphans(db)
        assert all(i.memory_id != e2.id for i in issues)

    def test_orphan_id_in_result(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        ev = _add(db)
        issues = detect_orphans(db)
        assert issues[0].memory_id == ev.id

    def test_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        _add(db)
        issues = detect_orphans(db)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_no_orphans(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_orphans(db) == []


# ---------------------------------------------------------------------------
# detect_missing_evidence
# ---------------------------------------------------------------------------

class TestDetectMissingEvidence:
    def test_validation_result_without_evidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='validation_result', status='accepted')
        issues = detect_missing_evidence(db)
        assert any(i.issue_type == 'missing_evidence' for i in issues)

    def test_validation_result_with_evidence_ok(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='validation_result', status='accepted', evidence='Run 42 confirmed')
        issues = detect_missing_evidence(db)
        assert issues == []

    def test_high_confidence_accepted_without_evidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted', confidence=4)
        issues = detect_missing_evidence(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'missing_evidence'

    def test_high_confidence_accepted_with_evidence_ok(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted', confidence=4, evidence='Documented')
        assert detect_missing_evidence(db) == []

    def test_low_confidence_accepted_no_flag(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted', confidence=3)
        issues = detect_missing_evidence(db)
        assert issues == []

    def test_severity_is_warning(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='validation_result', status='accepted')
        issues = detect_missing_evidence(db)
        assert all(i.severity == 'warning' for i in issues)

    def test_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='validation_result', status='accepted')
        _add(db, event_type='validation_result', status='accepted')
        issues = detect_missing_evidence(db)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_missing_evidence(db) == []


# ---------------------------------------------------------------------------
# detect_low_confidence_active
# ---------------------------------------------------------------------------

class TestDetectLowConfidenceActive:
    def test_detects_active_low_confidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=2)
        issues = detect_low_confidence_active(db, threshold=2)
        assert len(issues) == 1
        assert issues[0].issue_type == 'low_confidence_active'

    def test_detects_accepted_low_confidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted', confidence=1)
        issues = detect_low_confidence_active(db, threshold=2)
        assert len(issues) == 1

    def test_confidence_1_is_critical(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=1)
        issues = detect_low_confidence_active(db, threshold=2)
        assert issues[0].severity == 'critical'

    def test_confidence_2_is_warning(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=2)
        issues = detect_low_confidence_active(db, threshold=2)
        assert issues[0].severity == 'warning'

    def test_high_confidence_active_not_flagged(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=3)
        issues = detect_low_confidence_active(db, threshold=2)
        assert issues == []

    def test_proposed_with_low_confidence_not_flagged(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed', confidence=1)
        issues = detect_low_confidence_active(db, threshold=2)
        assert issues == []

    def test_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=1)
        _add(db, status='active', confidence=1)
        issues = detect_low_confidence_active(db, threshold=2)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_low_confidence_active(db) == []


# ---------------------------------------------------------------------------
# detect_unresolved_aging
# ---------------------------------------------------------------------------

class TestDetectUnresolvedAging:
    def test_no_issues_when_threshold_far_past(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        issues = detect_unresolved_aging(db, warning_days=36500)
        assert issues == []

    def test_detects_aged_unresolved(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        issues = detect_unresolved_aging(db, warning_days=-1, critical_days=36500)
        assert len(issues) == 1
        assert issues[0].issue_type == 'unresolved_aging'

    def test_warning_severity(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        issues = detect_unresolved_aging(db, warning_days=-1, critical_days=36500)
        assert issues[0].severity == 'warning'

    def test_critical_severity(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        issues = detect_unresolved_aging(db, warning_days=-1, critical_days=-2)
        assert issues[0].severity == 'critical'

    def test_excludes_resolved_status(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted')
        issues = detect_unresolved_aging(db, warning_days=-1, critical_days=36500)
        assert issues == []

    def test_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        _add(db, status='unresolved')
        issues = detect_unresolved_aging(db, warning_days=-1, critical_days=36500)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_unresolved_aging(db) == []


# ---------------------------------------------------------------------------
# detect_deprecated_linked
# ---------------------------------------------------------------------------

class TestDetectDeprecatedLinked:
    def test_detects_deprecated_target(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep = _add(db, status='deprecated')
        active = _add(db, status='active')
        service.link_memory_events(db, active.id, dep.id, 'supports')
        issues = detect_deprecated_linked(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'deprecated_linked'
        assert issues[0].memory_id == dep.id

    def test_no_issue_when_source_also_deprecated(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep1 = _add(db, status='deprecated')
        dep2 = _add(db, status='deprecated')
        service.link_memory_events(db, dep1.id, dep2.id, 'supports')
        assert detect_deprecated_linked(db) == []

    def test_severity_is_warning(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep = _add(db, status='deprecated')
        active = _add(db, status='active')
        service.link_memory_events(db, active.id, dep.id, 'supports')
        issues = detect_deprecated_linked(db)
        assert issues[0].severity == 'warning'

    def test_deduplicates_multiple_links_to_same_deprecated(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep = _add(db, status='deprecated')
        a1 = _add(db, status='active')
        a2 = _add(db, status='active')
        service.link_memory_events(db, a1.id, dep.id, 'supports')
        service.link_memory_events(db, a2.id, dep.id, 'refines')
        issues = detect_deprecated_linked(db)
        assert len(issues) == 1

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_deprecated_linked(db) == []


# ---------------------------------------------------------------------------
# detect_duplicate_title
# ---------------------------------------------------------------------------

class TestDetectDuplicateTitle:
    def test_no_duplicates_unique_titles(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, title='Alpha')
        _add(db, title='Beta')
        assert detect_duplicate_title(db) == []

    def test_detects_exact_duplicate(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, title='Same Title')
        _add(db, title='Same Title')
        issues = detect_duplicate_title(db)
        assert len(issues) == 2
        assert all(i.issue_type == 'duplicate_title' for i in issues)

    def test_severity_is_warning(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, title='Dup')
        _add(db, title='Dup')
        issues = detect_duplicate_title(db)
        assert all(i.severity == 'warning' for i in issues)

    def test_case_sensitive_no_false_positives(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, title='My Title')
        _add(db, title='my title')
        assert detect_duplicate_title(db) == []

    def test_ordered_by_title_then_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, title='B')
        _add(db, title='A')
        _add(db, title='A')
        _add(db, title='B')
        issues = detect_duplicate_title(db)
        titles = [i.title for i in issues]
        assert titles == sorted(titles)

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_duplicate_title(db) == []


# ---------------------------------------------------------------------------
# detect_excessive_fanout
# ---------------------------------------------------------------------------

class TestDetectExcessiveFanout:
    def test_no_issue_below_limit(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        _add(db, related_ids=[e1.id, e2.id])
        issues = detect_excessive_fanout(db, max_fanout=3)
        assert issues == []

    def test_detects_above_limit(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        others = [_add(db, status='accepted') for _ in range(3)]
        _add(db, related_ids=[e.id for e in others])
        issues = detect_excessive_fanout(db, max_fanout=2)
        assert len(issues) == 1
        assert issues[0].issue_type == 'excessive_fanout'

    def test_severity_is_info(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        others = [_add(db, status='accepted') for _ in range(3)]
        _add(db, related_ids=[e.id for e in others])
        issues = detect_excessive_fanout(db, max_fanout=2)
        assert issues[0].severity == 'info'

    def test_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        targets = [_add(db, status='accepted') for _ in range(3)]
        _add(db, related_ids=[e.id for e in targets])
        _add(db, related_ids=[e.id for e in targets])
        issues = detect_excessive_fanout(db, max_fanout=2)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_excessive_fanout(db) == []


# ---------------------------------------------------------------------------
# detect_adaptation_lineage_gap
# ---------------------------------------------------------------------------

class TestDetectAdaptationLineageGap:
    def test_adaptation_without_validation_flagged(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='adaptation', status='accepted')
        issues = detect_adaptation_lineage_gap(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'adaptation_lineage_gap'

    def test_adaptation_linked_to_validation_not_flagged(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        adapt = _add(db, event_type='adaptation', status='accepted')
        valid = _add(db, event_type='validation_result', status='accepted')
        service.link_memory_events(db, adapt.id, valid.id, 'derived_from')
        issues = detect_adaptation_lineage_gap(db)
        assert issues == []

    def test_validation_linked_to_adaptation_not_flagged(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        adapt = _add(db, event_type='adaptation', status='accepted')
        valid = _add(db, event_type='validation_result', status='accepted')
        service.link_memory_events(db, valid.id, adapt.id, 'supports')
        issues = detect_adaptation_lineage_gap(db)
        assert issues == []

    def test_proposed_adaptation_not_flagged(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='adaptation', status='proposed')
        issues = detect_adaptation_lineage_gap(db)
        assert issues == []

    def test_severity_is_warning(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='adaptation', status='accepted')
        issues = detect_adaptation_lineage_gap(db)
        assert issues[0].severity == 'warning'

    def test_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, event_type='adaptation', status='accepted')
        _add(db, event_type='adaptation', status='accepted')
        issues = detect_adaptation_lineage_gap(db)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert detect_adaptation_lineage_gap(db) == []


# ---------------------------------------------------------------------------
# build_governance_report
# ---------------------------------------------------------------------------

class TestBuildGovernanceReport:
    def test_returns_governance_report(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        report = build_governance_report(db)
        assert isinstance(report, GovernanceReport)

    def test_empty_db_no_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        report = build_governance_report(db)
        assert report.issues == []
        assert report.total_events == 0

    def test_total_events_counted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(3):
            _add(db)
        report = build_governance_report(db)
        assert report.total_events == 3

    def test_generated_at_is_utc(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        report = build_governance_report(db)
        assert report.generated_at.endswith('Z')

    def test_severity_counts(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=1)  # → low_confidence_active critical
        _add(db, status='active', confidence=2)  # → low_confidence_active warning
        report = build_governance_report(
            db,
            stale_warning_days=36500,   # suppress stale
            unresolved_warning_days=36500,  # suppress aging
        )
        assert report.critical_count >= 1
        assert report.warning_count >= 1

    def test_sorted_critical_before_warning_before_info(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        # orphan → info; low-conf active confidence=1 → critical
        _add(db, status='active', confidence=1)
        report = build_governance_report(
            db,
            stale_warning_days=36500,
            unresolved_warning_days=36500,
        )
        severities = [i.severity for i in report.issues]
        from memory.governance import _SEVERITY_ORDER
        ranks = [_SEVERITY_ORDER[s] for s in severities]
        assert ranks == sorted(ranks)

    def test_to_dict_has_required_keys(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        report = build_governance_report(db)
        d = report.to_dict()
        assert 'generated_at' in d
        assert 'total_events' in d
        assert 'critical_count' in d
        assert 'warning_count' in d
        assert 'info_count' in d
        assert 'issues' in d
        assert isinstance(d['issues'], list)

    def test_deterministic_repeated_calls(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=1)
        r1 = build_governance_report(db, stale_warning_days=36500, unresolved_warning_days=36500)
        r2 = build_governance_report(db, stale_warning_days=36500, unresolved_warning_days=36500)
        assert [i.to_dict() for i in r1.issues] == [i.to_dict() for i in r2.issues]


# ---------------------------------------------------------------------------
# Severity assignment
# ---------------------------------------------------------------------------

class TestSeverityAssignment:
    def test_critical_sorts_before_warning(self):
        from memory.governance import _SEVERITY_ORDER
        assert _SEVERITY_ORDER['critical'] < _SEVERITY_ORDER['warning']

    def test_warning_sorts_before_info(self):
        from memory.governance import _SEVERITY_ORDER
        assert _SEVERITY_ORDER['warning'] < _SEVERITY_ORDER['info']

    def test_low_confidence_1_critical(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=1)
        issues = detect_low_confidence_active(db)
        assert issues[0].severity == 'critical'

    def test_low_confidence_2_warning(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=2)
        issues = detect_low_confidence_active(db)
        assert issues[0].severity == 'warning'

    def test_conflict_always_critical(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.create_contradiction_link(db, e1.id, e2.id, created_by='tester', reason='conflict', link_confidence=3)
        issues = detect_conflicts(db)
        assert issues[0].severity == 'critical'

    def test_orphan_always_info(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db)
        issues = detect_orphans(db)
        assert issues[0].severity == 'info'


# ---------------------------------------------------------------------------
# RetrievalFilter (governance-aware filtering)
# ---------------------------------------------------------------------------

class TestRetrievalFilter:
    def _make_scored(self, db, **kw):
        ev = _add(db, **kw)
        from memory.retrieval import RetrievalQuery, retrieve
        results = retrieve(db, RetrievalQuery(expand_related=False, limit=1000))
        by_id = {s.event.id: s for s in results}
        return by_id[ev.id]

    def test_exclude_deprecated(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep_ev = _add(db, status='deprecated')
        normal_ev = _add(db, status='accepted')
        from memory.retrieval import RetrievalQuery, retrieve
        all_scored = retrieve(db, RetrievalQuery(expand_related=False))
        filtered = filter_events(all_scored, RetrievalFilter(exclude_deprecated=True))
        ids = [s.event.id for s in filtered]
        assert dep_ev.id not in ids
        assert normal_ev.id in ids

    def test_suppress_unresolved(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        unres = _add(db, status='unresolved')
        accepted = _add(db, status='accepted')
        from memory.retrieval import RetrievalQuery, retrieve
        all_scored = retrieve(db, RetrievalQuery(expand_related=False))
        filtered = filter_events(all_scored, RetrievalFilter(suppress_unresolved=True))
        ids = [s.event.id for s in filtered]
        assert unres.id not in ids
        assert accepted.id in ids

    def test_min_confidence_active_excludes_low(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        low = _add(db, status='active', confidence=2)
        high = _add(db, status='active', confidence=4)
        from memory.retrieval import RetrievalQuery, retrieve
        all_scored = retrieve(db, RetrievalQuery(expand_related=False))
        filtered = filter_events(all_scored, RetrievalFilter(min_confidence_active=3))
        ids = [s.event.id for s in filtered]
        assert low.id not in ids
        assert high.id in ids

    def test_min_confidence_active_passes_proposed_low_confidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        proposed = _add(db, status='proposed', confidence=1)
        from memory.retrieval import RetrievalQuery, retrieve
        all_scored = retrieve(db, RetrievalQuery(expand_related=False))
        filtered = filter_events(all_scored, RetrievalFilter(min_confidence_active=3))
        ids = [s.event.id for s in filtered]
        assert proposed.id in ids

    def test_no_filter_passes_all(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='deprecated')
        _add(db, status='unresolved')
        _add(db, status='accepted', confidence=1)
        from memory.retrieval import RetrievalQuery, retrieve
        all_scored = retrieve(db, RetrievalQuery(expand_related=False))
        filtered = filter_events(all_scored, RetrievalFilter())
        assert len(filtered) == len(all_scored)

    def test_filter_is_pure_function(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='deprecated')
        _add(db, status='accepted')
        from memory.retrieval import RetrievalQuery, retrieve
        all_scored = retrieve(db, RetrievalQuery(expand_related=False))
        f = RetrievalFilter(exclude_deprecated=True)
        r1 = filter_events(all_scored, f)
        r2 = filter_events(all_scored, f)
        assert [s.event.id for s in r1] == [s.event.id for s in r2]


# ---------------------------------------------------------------------------
# detect_fired_decisions_without_assembly (Phase 9A)
# ---------------------------------------------------------------------------

def _insert_activation_policy(conn):
    """Insert a minimal activation_policies row and return its id."""
    now = '2026-01-01T00:00:00Z'
    conn.execute(
        """INSERT INTO activation_policies
           (name, status, trigger_class, trigger_conditions_json,
            created_at, created_by, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ('test-policy', 'active', 'operator_request', '{}', now, 'tester', 'test'),
    )
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _insert_fired_decision(conn, policy_id, *, resulting_assembly_id=None):
    """Insert a fired=1 activation_decision_log row."""
    conn.execute(
        """INSERT INTO activation_decision_log
           (policy_id, policy_snapshot_json, trigger_class,
            trigger_event_json, fired, detection_reason,
            resulting_assembly_id, detected_at)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
        (policy_id, '{}', 'operator_request', '{}',
         'test fired', resulting_assembly_id, '2026-01-01T00:00:00Z'),
    )
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _insert_assembly(conn):
    """Insert a minimal context_assembly_log row and return its id."""
    import hashlib, json as _json, uuid
    now = '2026-01-01T00:00:00Z'
    snapshot = _json.dumps({'session_id': str(uuid.uuid4()), 'included_entries': 0,
                            'chars_used': 0, 'char_budget': 4000})
    h = hashlib.sha256(snapshot.encode()).hexdigest()
    conn.execute(
        """INSERT INTO context_assembly_log
           (assembly_hash, session_id, assembly_version,
            assembled_at, db_path, policy_json,
            entries_accepted, entries_rejected_budget, entries_rejected_filter,
            char_budget_used, char_budget_limit, assembly_snapshot_json)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 4000, ?)""",
        (h, str(uuid.uuid4()), 'v1', now, ':memory:', '{}', snapshot),
    )
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


class TestDetectFiredDecisionsWithoutAssembly:
    def _db(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        return db

    def test_flags_fired_decision_with_null_assembly(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        policy_id = _insert_activation_policy(conn)
        decision_id = _insert_fired_decision(conn, policy_id, resulting_assembly_id=None)
        conn.commit()
        conn.close()

        issues = detect_fired_decisions_without_assembly(db)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.issue_type == 'fired_decision_without_assembly'
        assert issue.severity == 'warning'
        assert issue.memory_id == decision_id
        assert str(policy_id) in issue.rationale

    def test_no_issue_when_assembly_present(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        policy_id = _insert_activation_policy(conn)
        assembly_id = _insert_assembly(conn)
        _insert_fired_decision(conn, policy_id, resulting_assembly_id=assembly_id)
        conn.commit()
        conn.close()

        issues = detect_fired_decisions_without_assembly(db)
        assert issues == []

    def test_non_firing_decision_not_flagged(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        policy_id = _insert_activation_policy(conn)
        conn.execute(
            """INSERT INTO activation_decision_log
               (policy_id, policy_snapshot_json, trigger_class,
                trigger_event_json, fired, detection_reason, detected_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (policy_id, '{}', 'operator_request', '{}',
             'did not fire', '2026-01-01T00:00:00Z'),
        )
        conn.commit()
        conn.close()

        issues = detect_fired_decisions_without_assembly(db)
        assert issues == []

    def test_wired_into_governance_report(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        policy_id = _insert_activation_policy(conn)
        _insert_fired_decision(conn, policy_id, resulting_assembly_id=None)
        conn.commit()
        conn.close()

        report = build_governance_report(db, detect_execution_lineage_issues=True)
        types = [i.issue_type for i in report.issues]
        assert 'fired_decision_without_assembly' in types

    def test_not_wired_when_flag_false(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        policy_id = _insert_activation_policy(conn)
        _insert_fired_decision(conn, policy_id, resulting_assembly_id=None)
        conn.commit()
        conn.close()

        report = build_governance_report(db, detect_execution_lineage_issues=False)
        types = [i.issue_type for i in report.issues]
        assert 'fired_decision_without_assembly' not in types

    def test_returns_empty_when_table_absent(self, tmp_path):
        # Pre-schema DB with no activation_decision_log table
        db = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE memory_events (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()

        issues = detect_fired_decisions_without_assembly(db)
        assert issues == []


# ---------------------------------------------------------------------------
# detect_orphaned_transitions (Phase 9A)
# ---------------------------------------------------------------------------

def _insert_cognition_session(conn):
    """Insert a minimal cognition_session row and return its id."""
    import uuid
    now = '2026-01-01T00:00:00Z'
    conn.execute(
        """INSERT INTO cognition_session
           (session_key, status, started_at, assembly_count, db_path,
            policy_fingerprint_json)
           VALUES (?, 'active', ?, 0, ':memory:', '{}')""",
        (str(uuid.uuid4()), now),
    )
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _insert_transition(conn, session_id, assembly_id, sequence_index=0):
    """Insert a minimal assembly_transition_log row (FK enforcement bypassed)."""
    now = '2026-01-01T00:00:00Z'
    conn.execute(
        """INSERT INTO assembly_transition_log
           (cognition_session_id, sequence_index, to_assembly_id,
            transition_type, transition_reason, triggered_by, transitioned_at)
           VALUES (?, ?, ?, 'policy_update', 'test', 'tester', ?)""",
        (session_id, sequence_index, assembly_id, now),
    )
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


class TestDetectOrphanedTransitions:
    def _db(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        return db

    def test_flags_transition_with_missing_assembly(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)  # no FK enforcement — lets us insert orphan
        session_id = _insert_cognition_session(conn)
        nonexistent_assembly_id = 99999
        transition_id = _insert_transition(conn, session_id, nonexistent_assembly_id)
        conn.commit()
        conn.close()

        issues = detect_orphaned_transitions(db)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.issue_type == 'orphaned_assembly_transition'
        assert issue.severity == 'warning'
        assert issue.memory_id == transition_id
        assert str(nonexistent_assembly_id) in issue.rationale

    def test_no_issue_for_valid_transition(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        session_id = _insert_cognition_session(conn)
        assembly_id = _insert_assembly(conn)
        _insert_transition(conn, session_id, assembly_id)
        conn.commit()
        conn.close()

        issues = detect_orphaned_transitions(db)
        assert issues == []

    def test_returns_empty_when_table_absent(self, tmp_path):
        db = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE memory_events (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()

        issues = detect_orphaned_transitions(db)
        assert issues == []

    def test_wired_into_governance_report(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        session_id = _insert_cognition_session(conn)
        _insert_transition(conn, session_id, 99999)
        conn.commit()
        conn.close()

        report = build_governance_report(db, detect_execution_lineage_issues=True)
        types = [i.issue_type for i in report.issues]
        assert 'orphaned_assembly_transition' in types


# ---------------------------------------------------------------------------
# check_lineage_integrity (Phase 9A)
# ---------------------------------------------------------------------------

class TestCheckLineageIntegrity:
    def _db(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        return db

    def test_clean_db_is_all_ok(self, tmp_path):
        db = self._db(tmp_path)
        result = check_lineage_integrity(db)
        assert result['all_ok'] is True
        assert result['total_broken'] == 0
        assert len(result['checks']) == 4

    def test_broken_decision_assembly_ref(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        policy_id = _insert_activation_policy(conn)
        _insert_fired_decision(conn, policy_id, resulting_assembly_id=99999)
        conn.commit()
        conn.close()

        result = check_lineage_integrity(db)
        assert result['all_ok'] is False
        assert result['total_broken'] >= 1
        check = next(c for c in result['checks'] if c['name'] == 'decision_resulting_assembly_id')
        assert check['broken_count'] == 1

    def test_broken_transition_to_assembly_ref(self, tmp_path):
        db = self._db(tmp_path)
        conn = sqlite3.connect(db)
        session_id = _insert_cognition_session(conn)
        _insert_transition(conn, session_id, 99999)
        conn.commit()
        conn.close()

        result = check_lineage_integrity(db)
        assert result['all_ok'] is False
        check = next(c for c in result['checks'] if c['name'] == 'transition_to_assembly_id')
        assert check['broken_count'] == 1
