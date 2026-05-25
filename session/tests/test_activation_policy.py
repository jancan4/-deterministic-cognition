"""
Phase 7A-core: Governed Activation Policy Substrate tests.

Coverage:
- Schema v15: fresh DB, v14→v15 migration, idempotency
- VALID_TRIGGER_CLASSES constant
- VALID_CONDITION_KEYS per-trigger-class allowlist
- ActivationPolicy validation (unknown trigger_class, unknown condition keys)
- evaluate_trigger() purity (no DB writes, no DB reads)
- Disabled/candidate/superseded policies return non-firing result
- Active operator_request, governance_escalation, contradiction_change, confidence_revision
- Reserved trigger classes return non-firing result
- create_activation_policy(), get_activation_policy()
- activate_activation_policy() lifecycle
- supersede_activation_policy() lifecycle (supersession columns, not invalidation columns)
- log_activation_decision() writes exactly one row
- replay_activation_decision() uses policy_snapshot_json; policy supersession does not change replay
- Interaction boundaries: evaluate_trigger writes no memory_events, no context_assembly_log, no retrieval_log
"""
import json
import sqlite3
from pathlib import Path

import pytest

from memory.service import init_db
from session.activation_policy import (
    VALID_CONDITION_KEYS,
    ActivationPolicy,
    ActivationPolicyLifecycleError,
    ActivationPolicyValidationError,
    ActivationTriggerResult,
    activate_activation_policy,
    create_activation_policy,
    evaluate_trigger,
    get_activation_decision,
    get_activation_policy,
    list_activation_decisions,
    list_activation_policies,
    log_activation_decision,
    replay_activation_decision,
    supersede_activation_policy,
)
from session.models import VALID_TRIGGER_CLASSES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'memory.db')
    init_db(path)
    return path


def _make_policy(
    db,
    *,
    trigger_class='operator_request',
    conditions=None,
    name='test-policy',
    created_by='tester',
    reason='test',
) -> ActivationPolicy:
    return create_activation_policy(
        db,
        name=name,
        trigger_class=trigger_class,
        trigger_conditions=conditions or {},
        created_by=created_by,
        reason=reason,
    )


def _make_active_policy(db, **kwargs) -> ActivationPolicy:
    policy = _make_policy(db, **kwargs)
    return activate_activation_policy(db, policy.id, activated_by='tester', reason='activate for test')


# ---------------------------------------------------------------------------
# Schema v15
# ---------------------------------------------------------------------------

class TestSchemaV15:
    def test_fresh_db_schema_version_16(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_activation_policies_table_exists(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='activation_policies'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_activation_decision_log_table_exists(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='activation_decision_log'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_activation_policies_columns(self, db):
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(activation_policies)')}
        conn.close()
        expected = {
            'id', 'name', 'trigger_class', 'trigger_conditions_json', 'status',
            'priority', 'policy_version', 'created_by', 'reason', 'created_at',
            'activated_at', 'activated_by',
            'superseded_at', 'superseded_by_policy_id', 'superseded_by_operator', 'superseded_reason',
            'invalidated_at', 'invalidated_reason',
            'provenance_json',
        }
        assert expected.issubset(cols)

    def test_activation_decision_log_columns(self, db):
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(activation_decision_log)')}
        conn.close()
        expected = {
            'id', 'policy_id', 'policy_snapshot_json', 'trigger_class', 'trigger_event_json',
            'fired', 'detection_reason', 'triggering_artifact_ids_json',
            'triggering_workflow_execution_id', 'triggering_session_id',
            'resulting_retrieval_id', 'resulting_assembly_id', 'resulting_transition_id',
            'detected_at',
        }
        assert expected.issubset(cols)

    def test_activation_policies_status_index(self, db):
        conn = sqlite3.connect(db)
        idxs = {row[1] for row in conn.execute("PRAGMA index_list(activation_policies)")}
        conn.close()
        assert 'idx_activation_policies_status' in idxs

    def test_activation_policies_trigger_class_index(self, db):
        conn = sqlite3.connect(db)
        idxs = {row[1] for row in conn.execute("PRAGMA index_list(activation_policies)")}
        conn.close()
        assert 'idx_activation_policies_trigger_class' in idxs

    def test_activation_decision_log_policy_id_index(self, db):
        conn = sqlite3.connect(db)
        idxs = {row[1] for row in conn.execute("PRAGMA index_list(activation_decision_log)")}
        conn.close()
        assert 'idx_activation_decisions_policy_id' in idxs

    def test_activation_decision_log_assembly_id_index(self, db):
        conn = sqlite3.connect(db)
        idxs = {row[1] for row in conn.execute("PRAGMA index_list(activation_decision_log)")}
        conn.close()
        assert 'idx_activation_decisions_assembly_id' in idxs

    def test_activation_decision_log_detected_at_index(self, db):
        conn = sqlite3.connect(db)
        idxs = {row[1] for row in conn.execute("PRAGMA index_list(activation_decision_log)")}
        conn.close()
        assert 'idx_activation_decisions_detected_at' in idxs

    def test_v14_db_migrates_to_v15_and_v16(self, tmp_path):
        db_path = str(tmp_path / 'old.db')

        # Build a v14 DB by patching the version before init
        import memory.service as svc
        original = svc._MEMORY_SCHEMA_VERSION
        try:
            svc._MEMORY_SCHEMA_VERSION = 14
            init_db(db_path)
        finally:
            svc._MEMORY_SCHEMA_VERSION = original

        # Now run the real init to trigger the v15 migration
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()

        assert version == 16
        assert 'activation_policies' in tables
        assert 'activation_decision_log' in tables

    def test_v15_migration_is_idempotent(self, db):
        from memory.service import _migrate_to_v15
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        # Calling twice must not raise
        _migrate_to_v15(conn)
        _migrate_to_v15(conn)
        conn.close()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestValidTriggerClasses:
    def test_ten_trigger_classes_defined(self):
        assert len(VALID_TRIGGER_CLASSES) == 10

    def test_core_four_in_valid_classes(self):
        assert 'operator_request' in VALID_TRIGGER_CLASSES
        assert 'governance_escalation' in VALID_TRIGGER_CLASSES
        assert 'contradiction_change' in VALID_TRIGGER_CLASSES
        assert 'confidence_revision' in VALID_TRIGGER_CLASSES

    def test_reserved_six_in_valid_classes(self):
        reserved = {
            'retrieval_refresh', 'continuity_refresh', 'workflow_checkpoint',
            'stale_session_recovery', 'embedding_invalidation', 'semantic_candidate_arrival',
        }
        assert reserved.issubset(VALID_TRIGGER_CLASSES)

    def test_condition_keys_cover_all_trigger_classes(self):
        assert set(VALID_CONDITION_KEYS.keys()) == set(VALID_TRIGGER_CLASSES)


# ---------------------------------------------------------------------------
# ActivationPolicy validation
# ---------------------------------------------------------------------------

class TestActivationPolicyValidation:
    def test_rejects_unknown_trigger_class(self):
        with pytest.raises(ActivationPolicyValidationError, match='Unknown trigger_class'):
            ActivationPolicy(
                id=None, name='x', trigger_class='not_a_real_class',
                trigger_conditions_json='{}', status='candidate', priority=100,
                policy_version='1.0.0', created_by='tester', reason='test',
                created_at='2026-01-01T00:00:00Z',
                activated_at=None, activated_by=None,
                superseded_at=None, superseded_by_policy_id=None,
                superseded_by_operator=None, superseded_reason=None,
                invalidated_at=None, invalidated_reason=None,
                provenance_json='{}',
            )

    def test_rejects_unknown_condition_keys(self):
        with pytest.raises(ActivationPolicyValidationError, match='Unknown condition keys'):
            ActivationPolicy(
                id=None, name='x', trigger_class='operator_request',
                trigger_conditions_json='{"forbidden_key": true}',
                status='candidate', priority=100, policy_version='1.0.0',
                created_by='tester', reason='test',
                created_at='2026-01-01T00:00:00Z',
                activated_at=None, activated_by=None,
                superseded_at=None, superseded_by_policy_id=None,
                superseded_by_operator=None, superseded_reason=None,
                invalidated_at=None, invalidated_reason=None,
                provenance_json='{}',
            )

    def test_rejects_unknown_condition_keys_governance_escalation(self):
        with pytest.raises(ActivationPolicyValidationError, match='Unknown condition keys'):
            ActivationPolicy(
                id=None, name='x', trigger_class='governance_escalation',
                trigger_conditions_json='{"unknown_key": "val"}',
                status='candidate', priority=100, policy_version='1.0.0',
                created_by='tester', reason='test',
                created_at='2026-01-01T00:00:00Z',
                activated_at=None, activated_by=None,
                superseded_at=None, superseded_by_policy_id=None,
                superseded_by_operator=None, superseded_reason=None,
                invalidated_at=None, invalidated_reason=None,
                provenance_json='{}',
            )

    def test_accepts_known_trigger_class_with_empty_conditions(self):
        policy = ActivationPolicy(
            id=None, name='x', trigger_class='operator_request',
            trigger_conditions_json='{}', status='candidate', priority=100,
            policy_version='1.0.0', created_by='tester', reason='test',
            created_at='2026-01-01T00:00:00Z',
            activated_at=None, activated_by=None,
            superseded_at=None, superseded_by_policy_id=None,
            superseded_by_operator=None, superseded_reason=None,
            invalidated_at=None, invalidated_reason=None,
            provenance_json='{}',
        )
        assert policy.trigger_class == 'operator_request'

    def test_accepts_known_condition_keys(self):
        policy = ActivationPolicy(
            id=None, name='x', trigger_class='governance_escalation',
            trigger_conditions_json='{"min_severity": "critical"}',
            status='candidate', priority=100, policy_version='1.0.0',
            created_by='tester', reason='test',
            created_at='2026-01-01T00:00:00Z',
            activated_at=None, activated_by=None,
            superseded_at=None, superseded_by_policy_id=None,
            superseded_by_operator=None, superseded_reason=None,
            invalidated_at=None, invalidated_reason=None,
            provenance_json='{}',
        )
        assert policy.conditions == {'min_severity': 'critical'}

    def test_rejects_invalid_json_in_conditions(self):
        with pytest.raises(ActivationPolicyValidationError, match='not valid JSON'):
            ActivationPolicy(
                id=None, name='x', trigger_class='operator_request',
                trigger_conditions_json='not-json',
                status='candidate', priority=100, policy_version='1.0.0',
                created_by='tester', reason='test',
                created_at='2026-01-01T00:00:00Z',
                activated_at=None, activated_by=None,
                superseded_at=None, superseded_by_policy_id=None,
                superseded_by_operator=None, superseded_reason=None,
                invalidated_at=None, invalidated_reason=None,
                provenance_json='{}',
            )

    def test_create_activation_policy_rejects_unknown_trigger_class(self, db):
        with pytest.raises(ActivationPolicyValidationError):
            create_activation_policy(
                db, name='x', trigger_class='not_real',
                trigger_conditions={}, created_by='tester', reason='test',
            )

    def test_create_activation_policy_requires_name(self, db):
        with pytest.raises(ValueError, match='name'):
            create_activation_policy(
                db, name='', trigger_class='operator_request',
                trigger_conditions={}, created_by='tester', reason='test',
            )

    def test_create_activation_policy_requires_created_by(self, db):
        with pytest.raises(ValueError, match='created_by'):
            create_activation_policy(
                db, name='p', trigger_class='operator_request',
                trigger_conditions={}, created_by='', reason='test',
            )

    def test_create_activation_policy_requires_reason(self, db):
        with pytest.raises(ValueError, match='reason'):
            create_activation_policy(
                db, name='p', trigger_class='operator_request',
                trigger_conditions={}, created_by='tester', reason='',
            )


# ---------------------------------------------------------------------------
# evaluate_trigger purity
# ---------------------------------------------------------------------------

class TestEvaluateTriggerPurity:
    def test_returns_activation_trigger_result(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        assert isinstance(result, ActivationTriggerResult)

    def test_writes_no_activation_decision_rows(self, db):
        policy = _make_active_policy(db)
        evaluate_trigger(policy, {'operator_id': 'jan'})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM activation_decision_log').fetchone()[0]
        conn.close()
        assert count == 0

    def test_writes_no_memory_events_rows(self, db):
        policy = _make_active_policy(db)
        evaluate_trigger(policy, {'operator_id': 'jan'})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM memory_events').fetchone()[0]
        conn.close()
        assert count == 0

    def test_writes_no_context_assembly_log_rows(self, db):
        policy = _make_active_policy(db)
        evaluate_trigger(policy, {'operator_id': 'jan'})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM context_assembly_log').fetchone()[0]
        conn.close()
        assert count == 0

    def test_writes_no_retrieval_log_rows(self, db):
        policy = _make_active_policy(db)
        evaluate_trigger(policy, {'operator_id': 'jan'})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM retrieval_log').fetchone()[0]
        conn.close()
        assert count == 0

    def test_deterministic_same_inputs_same_output(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change',
                                     conditions={'min_new_links': 2})
        event = {'new_link_ids': [10, 20, 30]}
        r1 = evaluate_trigger(policy, event)
        r2 = evaluate_trigger(policy, event)
        assert r1.fired == r2.fired
        assert r1.detection_reason == r2.detection_reason
        assert r1.triggering_artifact_ids == r2.triggering_artifact_ids


# ---------------------------------------------------------------------------
# Disabled / non-active policy semantics
# ---------------------------------------------------------------------------

class TestDisabledPolicySemantics:
    def test_candidate_policy_does_not_fire(self, db):
        policy = _make_policy(db)  # status='candidate'
        assert policy.status == 'candidate'
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        assert result.fired is False
        assert 'policy_disabled' in result.detection_reason
        assert 'candidate' in result.detection_reason

    def test_superseded_policy_does_not_fire(self, db):
        policy = _make_active_policy(db)
        superseded = supersede_activation_policy(db, policy.id, 'tester', 'replaced')
        assert superseded.status == 'superseded'
        result = evaluate_trigger(superseded, {'operator_id': 'jan'})
        assert result.fired is False
        assert 'policy_disabled' in result.detection_reason
        assert 'superseded' in result.detection_reason

    def test_trigger_class_matches_policy_on_non_active(self, db):
        policy = _make_policy(db, trigger_class='contradiction_change')
        result = evaluate_trigger(policy, {'new_link_ids': [1, 2, 3]})
        assert result.trigger_class == 'contradiction_change'
        assert result.fired is False


# ---------------------------------------------------------------------------
# operator_request trigger
# ---------------------------------------------------------------------------

class TestOperatorRequestTrigger:
    def test_fires_with_operator_id(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        assert result.fired is True
        assert result.trigger_class == 'operator_request'
        assert 'jan' in result.detection_reason

    def test_does_not_fire_without_operator_id(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {})
        assert result.fired is False
        assert 'missing operator_id' in result.detection_reason

    def test_does_not_fire_with_empty_operator_id(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': ''})
        assert result.fired is False

    def test_no_conditions_required(self, db):
        # operator_request accepts no condition keys
        policy = _make_active_policy(db, conditions={})
        result = evaluate_trigger(policy, {'operator_id': 'alice'})
        assert result.fired is True


# ---------------------------------------------------------------------------
# governance_escalation trigger
# ---------------------------------------------------------------------------

class TestGovernanceEscalationTrigger:
    def test_fires_on_critical_issue_default_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation')
        result = evaluate_trigger(policy, {
            'issue_types': ['stale_memory'],
            'severities': ['critical'],
        })
        assert result.fired is True

    def test_fires_on_warning_issue_default_threshold(self, db):
        # Default min_severity='warning', so warning qualifies
        policy = _make_active_policy(db, trigger_class='governance_escalation')
        result = evaluate_trigger(policy, {
            'issue_types': ['orphaned_event'],
            'severities': ['warning'],
        })
        assert result.fired is True

    def test_does_not_fire_on_info_at_warning_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation',
                                     conditions={'min_severity': 'warning'})
        result = evaluate_trigger(policy, {
            'issue_types': ['low_confidence_active'],
            'severities': ['info'],
        })
        assert result.fired is False

    def test_fires_only_at_critical_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation',
                                     conditions={'min_severity': 'critical'})
        # warning should not qualify at critical threshold
        result = evaluate_trigger(policy, {
            'issue_types': ['orphaned_event'],
            'severities': ['warning'],
        })
        assert result.fired is False

    def test_fires_at_critical_threshold_with_critical_issue(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation',
                                     conditions={'min_severity': 'critical'})
        result = evaluate_trigger(policy, {
            'issue_types': ['stale_memory', 'orphaned_event'],
            'severities': ['critical', 'warning'],
        })
        assert result.fired is True

    def test_does_not_fire_with_no_issues(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation')
        result = evaluate_trigger(policy, {'issue_types': [], 'severities': []})
        assert result.fired is False

    def test_fires_with_detector_names_filter(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation',
                                     conditions={'detector_names': ['stale_memory']})
        result = evaluate_trigger(policy, {
            'issue_types': ['stale_memory', 'orphaned_event'],
            'severities': ['critical', 'warning'],
        })
        assert result.fired is True

    def test_does_not_fire_when_detector_names_not_matching(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation',
                                     conditions={'detector_names': ['specific_detector']})
        result = evaluate_trigger(policy, {
            'issue_types': ['stale_memory'],
            'severities': ['critical'],
        })
        assert result.fired is False

    def test_require_all_fires_when_all_present(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation', conditions={
            'detector_names': ['stale_memory', 'orphaned_event'],
            'require_all': True,
        })
        result = evaluate_trigger(policy, {
            'issue_types': ['stale_memory', 'orphaned_event'],
            'severities': ['critical', 'warning'],
        })
        assert result.fired is True

    def test_require_all_does_not_fire_when_one_missing(self, db):
        policy = _make_active_policy(db, trigger_class='governance_escalation', conditions={
            'detector_names': ['stale_memory', 'missing_detector'],
            'require_all': True,
        })
        result = evaluate_trigger(policy, {
            'issue_types': ['stale_memory'],
            'severities': ['critical'],
        })
        assert result.fired is False
        assert 'missing_detector' in result.detection_reason


# ---------------------------------------------------------------------------
# contradiction_change trigger
# ---------------------------------------------------------------------------

class TestContradictionChangeTrigger:
    def test_fires_with_one_link_default_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change')
        result = evaluate_trigger(policy, {'new_link_ids': [42]})
        assert result.fired is True
        assert result.triggering_artifact_ids == [42]

    def test_fires_at_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change',
                                     conditions={'min_new_links': 3})
        result = evaluate_trigger(policy, {'new_link_ids': [1, 2, 3]})
        assert result.fired is True
        assert result.triggering_artifact_ids == [1, 2, 3]

    def test_does_not_fire_below_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change',
                                     conditions={'min_new_links': 3})
        result = evaluate_trigger(policy, {'new_link_ids': [1, 2]})
        assert result.fired is False
        assert '2' in result.detection_reason
        assert 'threshold=3' in result.detection_reason

    def test_does_not_fire_with_no_links(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change')
        result = evaluate_trigger(policy, {'new_link_ids': []})
        assert result.fired is False

    def test_triggering_artifact_ids_are_sorted(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change')
        result = evaluate_trigger(policy, {'new_link_ids': [30, 10, 20]})
        assert result.triggering_artifact_ids == [10, 20, 30]


# ---------------------------------------------------------------------------
# confidence_revision trigger
# ---------------------------------------------------------------------------

class TestConfidenceRevisionTrigger:
    def test_fires_with_one_revision_default_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='confidence_revision')
        result = evaluate_trigger(policy, {
            'revision_ids': [5],
            'revision_types': ['operator'],
        })
        assert result.fired is True
        assert result.triggering_artifact_ids == [5]

    def test_fires_at_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='confidence_revision',
                                     conditions={'min_new_revisions': 2})
        result = evaluate_trigger(policy, {
            'revision_ids': [1, 2],
            'revision_types': ['operator', 'operator'],
        })
        assert result.fired is True

    def test_does_not_fire_below_threshold(self, db):
        policy = _make_active_policy(db, trigger_class='confidence_revision',
                                     conditions={'min_new_revisions': 3})
        result = evaluate_trigger(policy, {
            'revision_ids': [1, 2],
            'revision_types': ['operator', 'operator'],
        })
        assert result.fired is False

    def test_fires_only_for_required_revision_type(self, db):
        policy = _make_active_policy(db, trigger_class='confidence_revision',
                                     conditions={'require_revision_type': 'governance'})
        result = evaluate_trigger(policy, {
            'revision_ids': [1, 2, 3],
            'revision_types': ['operator', 'governance', 'operator'],
        })
        assert result.fired is True
        assert result.triggering_artifact_ids == [2]

    def test_does_not_fire_when_required_type_absent(self, db):
        policy = _make_active_policy(db, trigger_class='confidence_revision',
                                     conditions={'require_revision_type': 'governance'})
        result = evaluate_trigger(policy, {
            'revision_ids': [1, 2],
            'revision_types': ['operator', 'operator'],
        })
        assert result.fired is False
        assert 'governance' in result.detection_reason

    def test_triggering_artifact_ids_are_sorted(self, db):
        policy = _make_active_policy(db, trigger_class='confidence_revision')
        result = evaluate_trigger(policy, {
            'revision_ids': [30, 10, 20],
            'revision_types': ['operator', 'operator', 'operator'],
        })
        assert result.triggering_artifact_ids == [10, 20, 30]


# ---------------------------------------------------------------------------
# Reserved trigger classes
# ---------------------------------------------------------------------------

class TestReservedTriggerClasses:
    @pytest.mark.parametrize('tc', [
        'retrieval_refresh',
        'continuity_refresh',
        'workflow_checkpoint',
        'stale_session_recovery',
        'embedding_invalidation',
        'semantic_candidate_arrival',
    ])
    def test_reserved_trigger_class_does_not_fire(self, db, tc):
        policy = _make_active_policy(db, trigger_class=tc)
        result = evaluate_trigger(policy, {})
        assert result.fired is False
        assert 'reserved' in result.detection_reason

    def test_reserved_trigger_class_creates_no_db_writes(self, db):
        policy = _make_active_policy(db, trigger_class='retrieval_refresh',
                                     conditions={'min_new_retrievals': 5})
        evaluate_trigger(policy, {'new_retrieval_count': 10})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM activation_decision_log').fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# create_activation_policy + get_activation_policy
# ---------------------------------------------------------------------------

class TestCreateAndGetPolicy:
    def test_creates_candidate_policy(self, db):
        policy = _make_policy(db)
        assert policy.id is not None
        assert policy.status == 'candidate'

    def test_round_trips_through_get(self, db):
        created = _make_policy(db, name='round-trip', trigger_class='contradiction_change',
                               conditions={'min_new_links': 2})
        fetched = get_activation_policy(db, created.id)
        assert fetched.name == 'round-trip'
        assert fetched.trigger_class == 'contradiction_change'
        assert fetched.conditions == {'min_new_links': 2}
        assert fetched.status == 'candidate'

    def test_default_priority_100(self, db):
        policy = _make_policy(db)
        assert policy.priority == 100

    def test_activated_at_is_null_on_creation(self, db):
        policy = _make_policy(db)
        assert policy.activated_at is None
        assert policy.activated_by is None

    def test_get_raises_on_missing_id(self, db):
        with pytest.raises(ValueError, match='not found'):
            get_activation_policy(db, 9999)

    def test_list_returns_created_policy(self, db):
        _make_policy(db, name='p1')
        _make_policy(db, name='p2')
        policies = list_activation_policies(db)
        names = [p.name for p in policies]
        assert 'p1' in names
        assert 'p2' in names

    def test_list_filters_by_status(self, db):
        candidate = _make_policy(db, name='cand')
        active = _make_active_policy(db, name='act')
        candidates = list_activation_policies(db, status='candidate')
        actives = list_activation_policies(db, status='active')
        assert any(p.id == candidate.id for p in candidates)
        assert any(p.id == active.id for p in actives)
        assert not any(p.id == candidate.id for p in actives)


# ---------------------------------------------------------------------------
# activate_activation_policy
# ---------------------------------------------------------------------------

class TestActivatePolicy:
    def test_candidate_becomes_active(self, db):
        policy = _make_policy(db)
        activated = activate_activation_policy(db, policy.id, 'jan', 'ready to fire')
        assert activated.status == 'active'
        assert activated.activated_by == 'jan'
        assert activated.activated_at is not None

    def test_rejects_already_active_policy(self, db):
        policy = _make_active_policy(db)
        with pytest.raises(ActivationPolicyLifecycleError, match='active'):
            activate_activation_policy(db, policy.id, 'jan', 'again')

    def test_rejects_superseded_policy(self, db):
        policy = _make_active_policy(db)
        supersede_activation_policy(db, policy.id, 'jan', 'replaced')
        with pytest.raises(ActivationPolicyLifecycleError, match='superseded'):
            activate_activation_policy(db, policy.id, 'jan', 'try again')

    def test_requires_activated_by(self, db):
        policy = _make_policy(db)
        with pytest.raises(ValueError, match='activated_by'):
            activate_activation_policy(db, policy.id, '', 'reason')

    def test_requires_reason(self, db):
        policy = _make_policy(db)
        with pytest.raises(ValueError, match='reason'):
            activate_activation_policy(db, policy.id, 'jan', '')

    def test_raises_on_missing_policy_id(self, db):
        with pytest.raises(ValueError, match='not found'):
            activate_activation_policy(db, 9999, 'jan', 'test')


# ---------------------------------------------------------------------------
# supersede_activation_policy
# ---------------------------------------------------------------------------

class TestSupersedePolicyr:
    def test_active_becomes_superseded(self, db):
        policy = _make_active_policy(db)
        superseded = supersede_activation_policy(db, policy.id, 'jan', 'replaced by v2')
        assert superseded.status == 'superseded'
        assert superseded.superseded_by_operator == 'jan'
        assert superseded.superseded_reason == 'replaced by v2'
        assert superseded.superseded_at is not None

    def test_supersession_does_not_write_invalidation_columns(self, db):
        policy = _make_active_policy(db)
        supersede_activation_policy(db, policy.id, 'jan', 'replaced')
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM activation_policies WHERE id = ?', (policy.id,)).fetchone()
        conn.close()
        assert row['invalidated_at'] is None
        assert row['invalidated_reason'] is None
        assert row['superseded_at'] is not None

    def test_optional_superseded_by_policy_id(self, db):
        p1 = _make_active_policy(db, name='v1')
        p2 = _make_policy(db, name='v2')
        superseded = supersede_activation_policy(db, p1.id, 'jan', 'replaced',
                                                  superseded_by_policy_id=p2.id)
        assert superseded.superseded_by_policy_id == p2.id

    def test_superseded_by_policy_id_defaults_to_none(self, db):
        policy = _make_active_policy(db)
        superseded = supersede_activation_policy(db, policy.id, 'jan', 'done')
        assert superseded.superseded_by_policy_id is None

    def test_rejects_candidate_policy(self, db):
        policy = _make_policy(db)
        with pytest.raises(ActivationPolicyLifecycleError, match='candidate'):
            supersede_activation_policy(db, policy.id, 'jan', 'reason')

    def test_rejects_already_superseded_policy(self, db):
        policy = _make_active_policy(db)
        supersede_activation_policy(db, policy.id, 'jan', 'first')
        with pytest.raises(ActivationPolicyLifecycleError, match='superseded'):
            supersede_activation_policy(db, policy.id, 'jan', 'second')

    def test_requires_operator(self, db):
        policy = _make_active_policy(db)
        with pytest.raises(ValueError, match='superseded_by_operator'):
            supersede_activation_policy(db, policy.id, '', 'reason')

    def test_requires_reason(self, db):
        policy = _make_active_policy(db)
        with pytest.raises(ValueError, match='reason'):
            supersede_activation_policy(db, policy.id, 'jan', '')

    def test_raises_on_missing_policy_id(self, db):
        with pytest.raises(ValueError, match='not found'):
            supersede_activation_policy(db, 9999, 'jan', 'test')


# ---------------------------------------------------------------------------
# log_activation_decision
# ---------------------------------------------------------------------------

class TestLogActivationDecision:
    def test_writes_exactly_one_row(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM activation_decision_log').fetchone()[0]
        conn.close()
        assert count == 1

    def test_row_has_correct_fields(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        row = get_activation_decision(db, decision_id)
        assert row['policy_id'] == policy.id
        assert row['trigger_class'] == 'operator_request'
        assert row['fired'] == 1
        assert 'jan' in row['detection_reason']
        assert row['policy_snapshot_json'] is not None
        assert row['detected_at'] is not None

    def test_fired_false_row_is_logged(self, db):
        policy = _make_policy(db)  # candidate — won't fire
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        assert result.fired is False
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        row = get_activation_decision(db, decision_id)
        assert row['fired'] == 0

    def test_records_resulting_assembly_id(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(
            db, policy, result, {'operator_id': 'jan'},
            resulting_assembly_id=42,
        )
        row = get_activation_decision(db, decision_id)
        assert row['resulting_assembly_id'] == 42

    def test_records_triggering_artifact_ids(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change')
        result = evaluate_trigger(policy, {'new_link_ids': [7, 8, 9]})
        decision_id = log_activation_decision(db, policy, result, {'new_link_ids': [7, 8, 9]})
        row = get_activation_decision(db, decision_id)
        assert json.loads(row['triggering_artifact_ids_json']) == [7, 8, 9]

    def test_policy_snapshot_json_captures_current_state(self, db):
        policy = _make_active_policy(db, name='snapshot-test')
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        row = get_activation_decision(db, decision_id)
        snapshot = json.loads(row['policy_snapshot_json'])
        assert snapshot['name'] == 'snapshot-test'
        assert snapshot['status'] == 'active'

    def test_requires_persisted_policy(self, db):
        policy = ActivationPolicy(
            id=None, name='x', trigger_class='operator_request',
            trigger_conditions_json='{}', status='active', priority=100,
            policy_version='1.0.0', created_by='tester', reason='test',
            created_at='2026-01-01T00:00:00Z',
            activated_at=None, activated_by=None,
            superseded_at=None, superseded_by_policy_id=None,
            superseded_by_operator=None, superseded_reason=None,
            invalidated_at=None, invalidated_reason=None,
            provenance_json='{}',
        )
        result = ActivationTriggerResult(
            trigger_class='operator_request', fired=True, detection_reason='test'
        )
        with pytest.raises(ValueError, match='persisted'):
            log_activation_decision(db, policy, result, {})

    def test_second_call_writes_second_row(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM activation_decision_log').fetchone()[0]
        conn.close()
        assert count == 2


# ---------------------------------------------------------------------------
# replay_activation_decision
# ---------------------------------------------------------------------------

class TestReplayActivationDecision:
    def test_replay_uses_policy_snapshot_json(self, db):
        policy = _make_active_policy(db, name='pre-supersession')
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})

        # Supersede the policy after the decision was logged
        supersede_activation_policy(db, policy.id, 'jan', 'replaced by v2')

        replayed = replay_activation_decision(db, decision_id)
        # Snapshot must reflect the policy state at decision time (active), not current (superseded)
        assert replayed.policy_snapshot.status == 'active'
        assert replayed.policy_snapshot.name == 'pre-supersession'
        assert replayed.replayed is True

    def test_replay_does_not_change_fired_status(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        replayed = replay_activation_decision(db, decision_id)
        assert replayed.fired is True
        assert 'jan' in replayed.detection_reason

    def test_replay_non_firing_decision(self, db):
        policy = _make_policy(db)  # candidate
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        assert result.fired is False
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})
        replayed = replay_activation_decision(db, decision_id)
        assert replayed.fired is False
        assert replayed.policy_snapshot.status == 'candidate'

    def test_replay_restores_triggering_artifact_ids(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change')
        result = evaluate_trigger(policy, {'new_link_ids': [11, 22]})
        decision_id = log_activation_decision(db, policy, result, {'new_link_ids': [11, 22]})
        replayed = replay_activation_decision(db, decision_id)
        assert replayed.triggering_artifact_ids == [11, 22]

    def test_replay_restores_resulting_assembly_id(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(
            db, policy, result, {'operator_id': 'jan'},
            resulting_assembly_id=99,
        )
        replayed = replay_activation_decision(db, decision_id)
        assert replayed.resulting_assembly_id == 99

    def test_replay_raises_on_missing_decision(self, db):
        with pytest.raises(ValueError, match='not found'):
            replay_activation_decision(db, 9999)

    def test_policy_supersession_does_not_alter_replayed_decision(self, db):
        policy = _make_active_policy(db, name='original')
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})

        # Supersede the original policy and activate a new one
        supersede_activation_policy(db, policy.id, 'jan', 'new version')
        new_policy = _make_active_policy(db, name='replacement')

        # Replay must still show the original policy from the snapshot
        replayed = replay_activation_decision(db, decision_id)
        assert replayed.policy_snapshot.name == 'original'
        assert replayed.policy_snapshot.id == policy.id
        assert replayed.policy_snapshot.status == 'active'


# ---------------------------------------------------------------------------
# Interaction boundaries — no canonical table mutations
# ---------------------------------------------------------------------------

class TestInteractionBoundaries:
    def test_evaluate_trigger_makes_no_db_writes_contradiction(self, db):
        policy = _make_active_policy(db, trigger_class='contradiction_change',
                                     conditions={'min_new_links': 1})
        evaluate_trigger(policy, {'new_link_ids': [1, 2]})
        conn = sqlite3.connect(db)
        counts = {
            'memory_events': conn.execute('SELECT COUNT(*) FROM memory_events').fetchone()[0],
            'memory_links': conn.execute('SELECT COUNT(*) FROM memory_links').fetchone()[0],
            'context_assembly_log': conn.execute('SELECT COUNT(*) FROM context_assembly_log').fetchone()[0],
            'activation_decision_log': conn.execute('SELECT COUNT(*) FROM activation_decision_log').fetchone()[0],
        }
        conn.close()
        for table, count in counts.items():
            assert count == 0, f"{table} should be empty after evaluate_trigger(); got {count}"

    def test_evaluate_trigger_makes_no_db_writes_confidence(self, db):
        policy = _make_active_policy(db, trigger_class='confidence_revision')
        evaluate_trigger(policy, {'revision_ids': [5], 'revision_types': ['operator']})
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM confidence_revisions').fetchone()[0]
        conn.close()
        assert count == 0

    def test_replay_makes_no_db_writes(self, db):
        policy = _make_active_policy(db)
        result = evaluate_trigger(policy, {'operator_id': 'jan'})
        decision_id = log_activation_decision(db, policy, result, {'operator_id': 'jan'})

        # Record counts before replay
        conn = sqlite3.connect(db)
        before = conn.execute('SELECT COUNT(*) FROM activation_decision_log').fetchone()[0]
        conn.close()

        replay_activation_decision(db, decision_id)

        conn = sqlite3.connect(db)
        after = conn.execute('SELECT COUNT(*) FROM activation_decision_log').fetchone()[0]
        conn.close()
        assert after == before  # replay adds no rows
