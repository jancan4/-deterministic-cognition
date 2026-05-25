"""
Phase 8B-core: Activation policy execution substrate tests.

Coverage (17 tests):
  1.  operator_request fires → assembly created, decision logged, resulting_assembly_id non-null
  2.  fires with session_id → transition logged, resulting_transition_id non-null, type='policy_update'
  3.  operator_request with missing operator_id → fired=False, no assembly
  4.  log_non_firing=False + not fired → zero DB writes
  5.  log_non_firing=True + not fired → one decision row, resulting_assembly_id NULL
  6.  reserved trigger class → fired=False, no assembly, decision optionally logged
  7.  non-active (candidate) policy → fired=False from evaluate, no assembly
  8.  governance_escalation with qualifying issues → fires, assembly created
  9.  contradiction_change threshold met → fires, triggering_artifact_ids in decision
 10.  confidence_revision threshold met → fires, revision IDs in triggering_artifact_ids
 11.  same policy executed twice, identical DB → two decision rows, same resulting_assembly_id
 12.  replay_activation_decision(decision_id) after execution → snapshot matches, assembly_id readable
 13.  replay_assembly(resulting_assembly_id) restores full session context
 14.  execute_activation_policy never writes to memory_events or memory_links
 15.  closed cognition session → transition fails, decision still logged, transition_id=None, no re-raise
 16.  activation-policy-evaluate dry-run → zero rows in any table
 17.  partial transition failure: assembly row exists, decision row exists, resulting_assembly_id
      populated, resulting_transition_id NULL, no memory_events mutation
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from memory.service import init_db
from session.activation_policy import (
    activate_activation_policy,
    create_activation_policy,
    get_activation_decision,
    list_activation_decisions,
    replay_activation_decision,
)
from session.execution import execute_activation_policy, PolicyExecutionResult
from session.models import ContextActivationPolicy
from session.reconstruction import (
    log_assembly_transition,
    open_cognition_session,
    close_cognition_session,
    replay_assembly,
)


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'memory.db')
    init_db(path)
    return path


def _make_active_policy(
    db,
    *,
    trigger_class='operator_request',
    conditions=None,
    name='test-policy',
):
    policy = create_activation_policy(
        db,
        name=name,
        trigger_class=trigger_class,
        trigger_conditions=conditions or {},
        created_by='tester',
        reason='test',
    )
    return activate_activation_policy(db, policy.id, activated_by='tester', reason='activate')


def _operator_trigger(operator_id='alice'):
    return {'operator_id': operator_id}


def _count_rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    finally:
        conn.close()


def _run_cli(args):
    """Run the CLI in-process; returns (stdout, stderr, exit_code)."""
    import io
    from memory.cli import build_parser, _COMMANDS
    parser = build_parser()
    parsed = parser.parse_args(args)
    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    _real_stdout = sys.stdout
    _real_stderr = sys.stderr
    exit_code = 0
    try:
        sys.stdout = stdout_cap
        sys.stderr = stderr_cap
        _COMMANDS[parsed.command](parsed)
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0
    finally:
        sys.stdout = _real_stdout
        sys.stderr = _real_stderr
    return stdout_cap.getvalue(), stderr_cap.getvalue(), exit_code


# ---------------------------------------------------------------------------
# Test 1: operator_request fires → assembly + decision
# ---------------------------------------------------------------------------

class TestOperatorRequestFires:
    def test_fires_creates_assembly_and_decision(self, db):
        policy = _make_active_policy(db)
        result = execute_activation_policy(
            db, policy.id, _operator_trigger(),
            triggered_by='alice',
        )
        assert result.fired is True
        assert result.resulting_assembly_id is not None
        assert result.decision_id is not None
        assert result.resulting_transition_id is None
        assert result.transition_error is None

        decision = get_activation_decision(db, result.decision_id)
        assert decision['fired'] == 1
        assert decision['resulting_assembly_id'] == result.resulting_assembly_id
        assert decision['resulting_transition_id'] is None


# ---------------------------------------------------------------------------
# Test 2: fires with session_id → transition logged
# ---------------------------------------------------------------------------

class TestFiresWithSession:
    def test_fires_with_session_logs_transition(self, db):
        policy = _make_active_policy(db)
        cap = ContextActivationPolicy()
        session = open_cognition_session(db, cap, triggered_by='alice')

        result = execute_activation_policy(
            db, policy.id, _operator_trigger(),
            cap,
            cognition_session_id=session.id,
            triggered_by='alice',
            transition_reason='weekly refresh',
        )
        assert result.fired is True
        assert result.resulting_assembly_id is not None
        assert result.resulting_transition_id is not None
        assert result.transition_error is None

        decision = get_activation_decision(db, result.decision_id)
        assert decision['resulting_transition_id'] == result.resulting_transition_id

        # Verify transition_type is 'policy_update'
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                'SELECT * FROM assembly_transition_log WHERE id = ?',
                (result.resulting_transition_id,),
            ).fetchone()
        finally:
            conn.close()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                'SELECT * FROM assembly_transition_log WHERE id = ?',
                (result.resulting_transition_id,),
            ).fetchone()
            assert row['transition_type'] == 'policy_update'
            assert row['cognition_session_id'] == session.id
            assert row['to_assembly_id'] == result.resulting_assembly_id
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test 3: missing operator_id → fired=False, no assembly
# ---------------------------------------------------------------------------

class TestMissingOperatorId:
    def test_missing_operator_id_not_fired(self, db):
        policy = _make_active_policy(db)
        asm_before = _count_rows(db, 'context_assembly_log')
        result = execute_activation_policy(
            db, policy.id, {},  # no operator_id
            triggered_by='alice',
        )
        assert result.fired is False
        assert result.resulting_assembly_id is None
        assert result.decision_id is None
        assert _count_rows(db, 'context_assembly_log') == asm_before


# ---------------------------------------------------------------------------
# Test 4: log_non_firing=False + not fired → zero DB writes
# ---------------------------------------------------------------------------

class TestZeroWritesOnNonFiring:
    def test_no_writes_when_not_fired_and_log_disabled(self, db):
        policy = _make_active_policy(db)
        dec_before = _count_rows(db, 'activation_decision_log')
        asm_before = _count_rows(db, 'context_assembly_log')

        result = execute_activation_policy(
            db, policy.id, {},
            triggered_by='alice',
            log_non_firing=False,
        )
        assert result.fired is False
        assert result.decision_id is None
        assert _count_rows(db, 'activation_decision_log') == dec_before
        assert _count_rows(db, 'context_assembly_log') == asm_before


# ---------------------------------------------------------------------------
# Test 5: log_non_firing=True + not fired → decision logged, no assembly
# ---------------------------------------------------------------------------

class TestLogNonFiring:
    def test_decision_logged_no_assembly_when_non_firing(self, db):
        policy = _make_active_policy(db)
        asm_before = _count_rows(db, 'context_assembly_log')

        result = execute_activation_policy(
            db, policy.id, {},
            triggered_by='alice',
            log_non_firing=True,
        )
        assert result.fired is False
        assert result.decision_id is not None
        assert result.resulting_assembly_id is None
        assert _count_rows(db, 'context_assembly_log') == asm_before

        decision = get_activation_decision(db, result.decision_id)
        assert decision['fired'] == 0
        assert decision['resulting_assembly_id'] is None


# ---------------------------------------------------------------------------
# Test 6: reserved trigger class → fired=False
# ---------------------------------------------------------------------------

class TestReservedTriggerClass:
    def test_reserved_class_returns_not_fired(self, db):
        policy = _make_active_policy(db, trigger_class='retrieval_refresh', name='reserved-pol')
        result = execute_activation_policy(
            db, policy.id,
            {'min_new_retrievals': 1},
            triggered_by='alice',
            log_non_firing=True,
        )
        assert result.fired is False
        assert 'reserved' in result.detection_reason
        assert result.resulting_assembly_id is None


# ---------------------------------------------------------------------------
# Test 7: candidate policy → fired=False
# ---------------------------------------------------------------------------

class TestCandidatePolicy:
    def test_candidate_policy_not_fired(self, db):
        # candidate — not activated
        policy = create_activation_policy(
            db, name='candidate-pol', trigger_class='operator_request',
            trigger_conditions={}, created_by='tester', reason='test',
        )
        result = execute_activation_policy(
            db, policy.id, _operator_trigger(),
            triggered_by='alice',
        )
        assert result.fired is False
        assert 'candidate' in result.detection_reason


# ---------------------------------------------------------------------------
# Test 8: governance_escalation → fires
# ---------------------------------------------------------------------------

class TestGovernanceEscalationFires:
    def test_governance_escalation_fires_with_qualifying_issues(self, db):
        policy = _make_active_policy(
            db,
            trigger_class='governance_escalation',
            conditions={'min_severity': 'warning'},
            name='gov-esc-pol',
        )
        trigger = {
            'issue_types': ['orphaned_link', 'stale_event'],
            'severities': ['critical', 'warning'],
        }
        result = execute_activation_policy(
            db, policy.id, trigger, triggered_by='alice',
        )
        assert result.fired is True
        assert result.resulting_assembly_id is not None


# ---------------------------------------------------------------------------
# Test 9: contradiction_change threshold → fires, artifact IDs in decision
# ---------------------------------------------------------------------------

class TestContradictionChangeFires:
    def test_contradiction_change_fires_and_logs_artifact_ids(self, db):
        policy = _make_active_policy(
            db,
            trigger_class='contradiction_change',
            conditions={'min_new_links': 2},
            name='contra-pol',
        )
        trigger = {'new_link_ids': [10, 20, 30]}
        result = execute_activation_policy(
            db, policy.id, trigger, triggered_by='alice',
        )
        assert result.fired is True
        assert sorted(result.triggering_artifact_ids) == [10, 20, 30]

        decision = get_activation_decision(db, result.decision_id)
        stored_ids = json.loads(decision['triggering_artifact_ids_json'])
        assert sorted(stored_ids) == [10, 20, 30]


# ---------------------------------------------------------------------------
# Test 10: confidence_revision threshold → fires, IDs in decision
# ---------------------------------------------------------------------------

class TestConfidenceRevisionFires:
    def test_confidence_revision_fires_and_logs_revision_ids(self, db):
        policy = _make_active_policy(
            db,
            trigger_class='confidence_revision',
            conditions={'min_new_revisions': 1},
            name='conf-rev-pol',
        )
        trigger = {'revision_ids': [5, 6], 'revision_types': ['operator', 'operator']}
        result = execute_activation_policy(
            db, policy.id, trigger, triggered_by='alice',
        )
        assert result.fired is True
        assert sorted(result.triggering_artifact_ids) == [5, 6]


# ---------------------------------------------------------------------------
# Test 11: same policy executed twice → two decisions, same assembly_id
# ---------------------------------------------------------------------------

class TestIdempotentAssembly:
    def test_two_executions_two_decisions_same_assembly(self, db):
        policy = _make_active_policy(db)
        r1 = execute_activation_policy(
            db, policy.id, _operator_trigger(), triggered_by='alice',
        )
        r2 = execute_activation_policy(
            db, policy.id, _operator_trigger(), triggered_by='alice',
        )
        assert r1.fired is True
        assert r2.fired is True
        assert r1.decision_id != r2.decision_id
        # Same DB state → same assembly_hash → same assembly row
        assert r1.resulting_assembly_id == r2.resulting_assembly_id


# ---------------------------------------------------------------------------
# Test 12: replay_activation_decision after execution
# ---------------------------------------------------------------------------

class TestReplayDecision:
    def test_replay_decision_matches_execution(self, db):
        policy = _make_active_policy(db)
        trigger = _operator_trigger()
        result = execute_activation_policy(
            db, policy.id, trigger, triggered_by='alice',
        )
        replayed = replay_activation_decision(db, result.decision_id)
        assert replayed.fired is True
        assert replayed.resulting_assembly_id == result.resulting_assembly_id
        assert replayed.policy_snapshot.id == policy.id


# ---------------------------------------------------------------------------
# Test 13: replay_assembly restores context
# ---------------------------------------------------------------------------

class TestReplayAssembly:
    def test_replay_assembly_restores_context(self, db):
        policy = _make_active_policy(db)
        result = execute_activation_policy(
            db, policy.id, _operator_trigger(), triggered_by='alice',
        )
        replayed = replay_assembly(result.resulting_assembly_id, db)
        assert replayed.replayed is True
        assert replayed.context.session_id is not None


# ---------------------------------------------------------------------------
# Test 14: no writes to memory_events or memory_links
# ---------------------------------------------------------------------------

class TestNoCanonicalMutation:
    def test_execute_does_not_write_memory_events_or_links(self, db):
        policy = _make_active_policy(db)
        me_before = _count_rows(db, 'memory_events')
        ml_before = _count_rows(db, 'memory_links')

        execute_activation_policy(
            db, policy.id, _operator_trigger(), triggered_by='alice',
        )
        assert _count_rows(db, 'memory_events') == me_before
        assert _count_rows(db, 'memory_links') == ml_before


# ---------------------------------------------------------------------------
# Test 15: closed session → transition fails, decision still logged
# ---------------------------------------------------------------------------

class TestClosedSessionTransitionFails:
    def test_closed_session_soft_fail_decision_logged(self, db):
        policy = _make_active_policy(db)
        cap = ContextActivationPolicy()
        session = open_cognition_session(db, cap, triggered_by='alice')
        close_cognition_session(db, session.id, reason='test close', triggered_by='alice')

        dec_before = _count_rows(db, 'activation_decision_log')
        asm_before = _count_rows(db, 'context_assembly_log')

        result = execute_activation_policy(
            db, policy.id, _operator_trigger(),
            cap,
            cognition_session_id=session.id,
            triggered_by='alice',
        )

        assert result.fired is True
        assert result.resulting_assembly_id is not None
        assert result.resulting_transition_id is None
        assert result.transition_error is not None
        assert result.decision_id is not None

        # Assembly and decision were written
        assert _count_rows(db, 'context_assembly_log') > asm_before
        assert _count_rows(db, 'activation_decision_log') > dec_before

        # Decision row has assembly_id but no transition_id
        decision = get_activation_decision(db, result.decision_id)
        assert decision['resulting_assembly_id'] == result.resulting_assembly_id
        assert decision['resulting_transition_id'] is None


# ---------------------------------------------------------------------------
# Test 16: activation-policy-evaluate dry-run → zero rows
# ---------------------------------------------------------------------------

class TestEvaluateDryRun:
    def test_evaluate_writes_zero_rows(self, db):
        policy = _make_active_policy(db)
        dec_before = _count_rows(db, 'activation_decision_log')
        asm_before = _count_rows(db, 'context_assembly_log')
        trans_before = _count_rows(db, 'assembly_transition_log')

        stdout, stderr, code = _run_cli([
            'activation-policy-evaluate', '--db', db,
            '--id', str(policy.id),
            '--trigger-event', json.dumps({'operator_id': 'alice'}),
        ])
        assert code == 0
        assert 'fired=True' in stdout
        assert 'dry-run' in stdout.lower()

        assert _count_rows(db, 'activation_decision_log') == dec_before
        assert _count_rows(db, 'context_assembly_log') == asm_before
        assert _count_rows(db, 'assembly_transition_log') == trans_before


# ---------------------------------------------------------------------------
# Test 17: partial transition failure invariants
# ---------------------------------------------------------------------------

class TestPartialTransitionFailure:
    def test_assembly_exists_decision_exists_transition_null_no_memory_mutation(self, db):
        policy = _make_active_policy(db)
        me_before = _count_rows(db, 'memory_events')
        ml_before = _count_rows(db, 'memory_links')

        # non-existent session_id forces transition failure
        nonexistent_session_id = 99999

        result = execute_activation_policy(
            db, policy.id, _operator_trigger(),
            cognition_session_id=nonexistent_session_id,
            triggered_by='alice',
        )

        # Assembly row exists
        assert result.resulting_assembly_id is not None
        conn = sqlite3.connect(db)
        try:
            asm_row = conn.execute(
                'SELECT id FROM context_assembly_log WHERE id = ?',
                (result.resulting_assembly_id,),
            ).fetchone()
        finally:
            conn.close()
        assert asm_row is not None

        # Decision row exists with assembly_id populated
        assert result.decision_id is not None
        decision = get_activation_decision(db, result.decision_id)
        assert decision['resulting_assembly_id'] == result.resulting_assembly_id

        # resulting_transition_id is NULL
        assert result.resulting_transition_id is None
        assert decision['resulting_transition_id'] is None

        # transition_error is set
        assert result.transition_error is not None

        # No memory_events or memory_links mutation
        assert _count_rows(db, 'memory_events') == me_before
        assert _count_rows(db, 'memory_links') == ml_before


# ---------------------------------------------------------------------------
# CLI smoke test: activation-policy-execute
# ---------------------------------------------------------------------------

class TestCLIExecute:
    def test_cli_execute_fires_and_prints_assembly_id(self, db):
        policy = _make_active_policy(db)
        stdout, stderr, code = _run_cli([
            'activation-policy-execute', '--db', db,
            '--id', str(policy.id),
            '--trigger-event', json.dumps({'operator_id': 'alice'}),
            '--triggered-by', 'alice',
        ])
        assert code == 0
        assert 'fired=True' in stdout
        assert 'resulting_assembly_id=' in stdout
        assert 'decision_id=' in stdout

    def test_cli_execute_partial_failure_warns_on_stderr(self, db):
        policy = _make_active_policy(db)
        stdout, stderr, code = _run_cli([
            'activation-policy-execute', '--db', db,
            '--id', str(policy.id),
            '--trigger-event', json.dumps({'operator_id': 'alice'}),
            '--triggered-by', 'alice',
            '--session-id', '99999',
        ])
        # fired successfully; warning on stderr for transition failure
        assert 'fired=True' in stdout
        assert 'WARNING' in stderr

    def test_cli_execute_not_fired_no_assembly_printed(self, db):
        policy = _make_active_policy(db)
        stdout, stderr, code = _run_cli([
            'activation-policy-execute', '--db', db,
            '--id', str(policy.id),
            '--trigger-event', '{}',  # missing operator_id
            '--triggered-by', 'alice',
        ])
        assert code == 0
        assert 'fired=False' in stdout


# ---------------------------------------------------------------------------
# Phase 9A: Replay-after-execution regression test
# ---------------------------------------------------------------------------

class TestReplayAfterExecution:
    """Regression guard: replay_assembly() restores a context consistent with the stored snapshot."""

    def test_replay_assembly_after_execution_matches_original(self, db):
        policy = _make_active_policy(db)
        result = execute_activation_policy(
            db, policy.id, _operator_trigger(), triggered_by='alice'
        )
        assert result.fired is True
        assembly_id = result.resulting_assembly_id

        replayed = replay_assembly(assembly_id, db)
        assert replayed.replayed is True

        # The snapshot stored at assembly time must agree with what replay restores.
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            'SELECT assembly_snapshot_json FROM context_assembly_log WHERE id = ?',
            (assembly_id,),
        ).fetchone()
        conn.close()

        stored = json.loads(row['assembly_snapshot_json'])
        ctx = replayed.context

        assert ctx.session_id == stored['session_id']
        assert ctx.included_entries == stored['included_entries']
        assert ctx.chars_used == stored['chars_used']
        assert ctx.char_budget == stored['char_budget']
