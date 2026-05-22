"""
Hermetic tests for memory/review.py.

Same timing trick as test_governance.py: negative day thresholds produce a
future cutoff so all current events count as "old enough" without sleeps.
"""

import pytest
from memory import service
from memory.review import (
    ReviewQueue,
    get_review_queue,
    review_conflicts,
    review_deprecated_linked,
    review_low_confidence_active,
    review_stale,
    review_unresolved,
)


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
# review_unresolved
# ---------------------------------------------------------------------------

class TestReviewUnresolved:
    def test_returns_unresolved_events(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        events = review_unresolved(db, aging_days=-1)
        assert len(events) == 1
        assert events[0].status == 'unresolved'

    def test_excludes_accepted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted')
        events = review_unresolved(db, aging_days=-1)
        assert events == []

    def test_excludes_fresh_within_threshold(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        events = review_unresolved(db, aging_days=36500)
        assert events == []

    def test_ordered_by_created_at_ascending(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved', title='First')
        _add(db, status='unresolved', title='Second')
        events = review_unresolved(db, aging_days=-1)
        # Both created in same second — stable order by id
        ids = [e.id for e in events]
        assert ids == sorted(ids)

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db, status='unresolved')
        events = review_unresolved(db, aging_days=-1, limit=3)
        assert len(events) == 3

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert review_unresolved(db) == []


# ---------------------------------------------------------------------------
# review_stale
# ---------------------------------------------------------------------------

class TestReviewStale:
    def test_returns_stale_active(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active')
        events = review_stale(db, stale_days=-1)
        assert len(events) == 1

    def test_returns_stale_proposed(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed')
        events = review_stale(db, stale_days=-1)
        assert len(events) == 1

    def test_excludes_accepted(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted')
        events = review_stale(db, stale_days=-1)
        assert events == []

    def test_excludes_fresh_within_threshold(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active')
        events = review_stale(db, stale_days=36500)
        assert events == []

    def test_ordered_by_updated_at_ascending(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='active')
        e2 = _add(db, status='active')
        events = review_stale(db, stale_days=-1)
        # Same second → stable by id ascending
        ids = [e.id for e in events]
        assert ids == sorted(ids)

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db, status='active')
        events = review_stale(db, stale_days=-1, limit=2)
        assert len(events) == 2

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert review_stale(db) == []


# ---------------------------------------------------------------------------
# review_conflicts
# ---------------------------------------------------------------------------

class TestReviewConflicts:
    def test_no_conflicts_returns_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted')
        assert review_conflicts(db) == []

    def test_returns_conflict_issues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.link_memory_events(db, e1.id, e2.id, 'contradicts')
        issues = review_conflicts(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'conflicting_active'

    def test_ordered_by_memory_id(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        e3 = _add(db, status='accepted')
        service.link_memory_events(db, e2.id, e3.id, 'contradicts')
        service.link_memory_events(db, e1.id, e3.id, 'contradicts')
        issues = review_conflicts(db)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert review_conflicts(db) == []


# ---------------------------------------------------------------------------
# review_low_confidence_active
# ---------------------------------------------------------------------------

class TestReviewLowConfidenceActive:
    def test_returns_active_low_confidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=1)
        events = review_low_confidence_active(db, max_confidence=2)
        assert len(events) == 1

    def test_returns_accepted_low_confidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='accepted', confidence=2)
        events = review_low_confidence_active(db, max_confidence=2)
        assert len(events) == 1

    def test_excludes_high_confidence(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=3)
        events = review_low_confidence_active(db, max_confidence=2)
        assert events == []

    def test_excludes_proposed_status(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='proposed', confidence=1)
        events = review_low_confidence_active(db, max_confidence=2)
        assert events == []

    def test_ordered_confidence_asc_then_id_asc(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='active', confidence=2)
        _add(db, status='active', confidence=1)
        events = review_low_confidence_active(db, max_confidence=2)
        confs = [e.confidence for e in events]
        assert confs == sorted(confs)

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db, status='active', confidence=1)
        events = review_low_confidence_active(db, max_confidence=2, limit=3)
        assert len(events) == 3

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert review_low_confidence_active(db) == []


# ---------------------------------------------------------------------------
# review_deprecated_linked
# ---------------------------------------------------------------------------

class TestReviewDeprecatedLinked:
    def test_returns_deprecated_linked_events(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep = _add(db, status='deprecated')
        active = _add(db, status='active')
        service.link_memory_events(db, active.id, dep.id, 'supports')
        events = review_deprecated_linked(db)
        assert len(events) == 1
        assert events[0].id == dep.id

    def test_excludes_unlinked_deprecated(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='deprecated')
        assert review_deprecated_linked(db) == []

    def test_excludes_deprecated_linked_only_from_deprecated(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep1 = _add(db, status='deprecated')
        dep2 = _add(db, status='deprecated')
        service.link_memory_events(db, dep1.id, dep2.id, 'supports')
        assert review_deprecated_linked(db) == []

    def test_ordered_by_id_ascending(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        dep1 = _add(db, status='deprecated')
        dep2 = _add(db, status='deprecated')
        active = _add(db, status='active')
        service.link_memory_events(db, active.id, dep1.id, 'supports')
        service.link_memory_events(db, active.id, dep2.id, 'refines')
        events = review_deprecated_linked(db)
        ids = [e.id for e in events]
        assert ids == sorted(ids)

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        deps = [_add(db, status='deprecated') for _ in range(4)]
        active = _add(db, status='active')
        for dep in deps:
            service.link_memory_events(db, active.id, dep.id, 'supports')
        events = review_deprecated_linked(db, limit=2)
        assert len(events) == 2

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        assert review_deprecated_linked(db) == []


# ---------------------------------------------------------------------------
# ReviewQueue and get_review_queue
# ---------------------------------------------------------------------------

class TestReviewQueue:
    def test_is_empty_when_nothing_pending(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        queue = ReviewQueue()
        assert queue.is_empty()

    def test_not_empty_when_unresolved_present(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        queue = get_review_queue(db, unresolved_aging_days=-1)
        assert not queue.is_empty()

    def test_total_counts_unique_events(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        _add(db, status='active', confidence=1)
        queue = get_review_queue(
            db,
            stale_days=-1,
            unresolved_aging_days=-1,
            max_confidence=2,
        )
        # unresolved event + low-conf-active event are distinct
        assert queue.total >= 2

    def test_to_dict_has_all_queues(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        queue = get_review_queue(db)
        d = queue.to_dict()
        assert 'total' in d
        assert 'unresolved' in d
        assert 'stale' in d
        assert 'low_confidence_active' in d
        assert 'deprecated_linked' in d
        assert 'conflicts' in d

    def test_conflicts_included(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        service.link_memory_events(db, e1.id, e2.id, 'contradicts')
        queue = get_review_queue(db)
        assert len(queue.conflicts) == 1

    def test_deterministic_repeated_calls(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        _add(db, status='unresolved')
        _add(db, status='active', confidence=1)
        q1 = get_review_queue(db, stale_days=-1, unresolved_aging_days=-1, max_confidence=2)
        q2 = get_review_queue(db, stale_days=-1, unresolved_aging_days=-1, max_confidence=2)
        assert q1.to_dict() == q2.to_dict()

    def test_empty_db_all_queues_empty(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        queue = get_review_queue(db)
        assert queue.is_empty()
        assert queue.total == 0


# ---------------------------------------------------------------------------
# Deterministic review ordering
# ---------------------------------------------------------------------------

class TestDeterministicOrdering:
    def test_review_unresolved_deterministic(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db, status='unresolved')
        r1 = [e.id for e in review_unresolved(db, aging_days=-1)]
        r2 = [e.id for e in review_unresolved(db, aging_days=-1)]
        assert r1 == r2

    def test_review_stale_deterministic(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db, status='active')
        r1 = [e.id for e in review_stale(db, stale_days=-1)]
        r2 = [e.id for e in review_stale(db, stale_days=-1)]
        assert r1 == r2

    def test_review_low_confidence_deterministic(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        for _ in range(5):
            _add(db, status='active', confidence=1)
        r1 = [e.id for e in review_low_confidence_active(db, max_confidence=2)]
        r2 = [e.id for e in review_low_confidence_active(db, max_confidence=2)]
        assert r1 == r2

    def test_review_conflicts_deterministic(self, tmp_path):
        db = str(tmp_path / 'mem.db')
        service.init_db(db)
        e1 = _add(db, status='accepted')
        e2 = _add(db, status='accepted')
        e3 = _add(db, status='accepted')
        service.link_memory_events(db, e1.id, e2.id, 'contradicts')
        service.link_memory_events(db, e2.id, e3.id, 'contradicts')
        r1 = [i.memory_id for i in review_conflicts(db)]
        r2 = [i.memory_id for i in review_conflicts(db)]
        assert r1 == r2
