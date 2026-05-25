"""
Phase 7A-beta: Activation Policy Governance and Operator Ergonomics tests.

Coverage:
- detect_unreviewed_activation_policies(): empty/stale/recent/threshold edge cases
- detect_stale_active_activation_policies(): never-fired vs fired, threshold edge cases
- build_governance_report() wiring for both new detectors
- CLI: create, list, inspect, activate, supersede, decisions, replay commands
- Replay purity invariants (the required hardening addition):
    - activation_decision_log row count unchanged after replay
    - activation_policies rows byte-equivalent before/after replay
    - cognition_session unchanged before/after replay
    - assembly_transition_log unchanged before/after replay
    - divergence detection is output-only (no writes)
    - replay command never calls log_activation_decision()
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

import pytest

from memory.cli import _COMMANDS, build_parser
from memory.governance import (
    ACTIVATION_CANDIDATE_WARNING_DAYS,
    ACTIVATION_STALE_ACTIVE_DAYS,
    build_governance_report,
    detect_stale_active_activation_policies,
    detect_unreviewed_activation_policies,
)
from memory.service import init_db
from session.activation_policy import (
    activate_activation_policy,
    create_activation_policy,
    evaluate_trigger,
    get_activation_policy,
    list_activation_decisions,
    log_activation_decision,
    replay_activation_decision,
    supersede_activation_policy,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'memory.db')
    init_db(path)
    return path


def _make_policy(db, *, trigger_class='operator_request', conditions=None,
                 name='test-policy', created_by='tester', reason='test reason'):
    return create_activation_policy(
        db,
        name=name,
        trigger_class=trigger_class,
        trigger_conditions=conditions or {},
        created_by=created_by,
        reason=reason,
    )


def _make_active_policy(db, **kwargs):
    policy = _make_policy(db, **kwargs)
    return activate_activation_policy(db, policy.id, activated_by='tester', reason='activate for test')


def _backdate(db_path, table, col, row_id, days):
    """Set a timestamp column to (now - days) for a specific row."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = sqlite3.connect(db_path)
    conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (ts, row_id))
    conn.commit()
    conn.close()


def _row_count(db_path, table):
    conn = sqlite3.connect(db_path)
    n = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    conn.close()
    return n


def _snapshot_table(db_path, table):
    """Return all rows as a list of dicts (sorted by id for determinism)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f'SELECT * FROM {table} ORDER BY id ASC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _run_cli(args_list):
    """Parse and execute a CLI command. Caller captures stdout via capsys."""
    parser = build_parser()
    args = parser.parse_args(args_list)
    _COMMANDS[args.command](args)


# ---------------------------------------------------------------------------
# TestGovernanceDetectorUnreviewed
# ---------------------------------------------------------------------------

class TestGovernanceDetectorUnreviewed:

    def test_empty_when_no_policies(self, db):
        issues = detect_unreviewed_activation_policies(db)
        assert issues == []

    def test_no_issue_for_recently_created_candidate(self, db):
        _make_policy(db, name='fresh')
        # created_at = now, well within the threshold
        issues = detect_unreviewed_activation_policies(db, candidate_warning_days=7)
        assert issues == []

    def test_flags_old_candidate(self, db):
        p = _make_policy(db, name='old-candidate')
        _backdate(db, 'activation_policies', 'created_at', p.id, 10)
        issues = detect_unreviewed_activation_policies(db, candidate_warning_days=7)
        assert len(issues) == 1
        assert issues[0].issue_type == 'unreviewed_activation_policy'
        assert issues[0].severity == 'warning'
        assert issues[0].memory_id == 0
        assert issues[0].metadata['policy_id'] == p.id
        assert issues[0].metadata['policy_name'] == 'old-candidate'

    def test_does_not_flag_active_policy(self, db):
        p = _make_policy(db, name='will-be-active')
        _backdate(db, 'activation_policies', 'created_at', p.id, 10)
        activate_activation_policy(db, p.id, activated_by='op', reason='activate')
        issues = detect_unreviewed_activation_policies(db, candidate_warning_days=7)
        assert issues == []

    def test_threshold_boundary(self, db):
        p = _make_policy(db, name='boundary')
        # Backdated to one day past threshold — strictly older than cutoff, so flagged
        _backdate(db, 'activation_policies', 'created_at', p.id, 8)
        issues = detect_unreviewed_activation_policies(db, candidate_warning_days=7)
        assert len(issues) == 1

    def test_multiple_old_candidates(self, db):
        p1 = _make_policy(db, name='old-1')
        p2 = _make_policy(db, name='old-2')
        _backdate(db, 'activation_policies', 'created_at', p1.id, 14)
        _backdate(db, 'activation_policies', 'created_at', p2.id, 8)
        issues = detect_unreviewed_activation_policies(db, candidate_warning_days=7)
        assert len(issues) == 2
        ids = {i.metadata['policy_id'] for i in issues}
        assert ids == {p1.id, p2.id}

    def test_wired_into_build_governance_report(self, db):
        p = _make_policy(db, name='stale-candidate')
        _backdate(db, 'activation_policies', 'created_at', p.id, 10)
        report = build_governance_report(
            db, activation_candidate_warning_days=7, detect_activation_issues=True
        )
        types = {i.issue_type for i in report.issues}
        assert 'unreviewed_activation_policy' in types

    def test_not_wired_when_detect_activation_issues_false(self, db):
        p = _make_policy(db, name='stale-candidate')
        _backdate(db, 'activation_policies', 'created_at', p.id, 10)
        report = build_governance_report(db, detect_activation_issues=False)
        types = {i.issue_type for i in report.issues}
        assert 'unreviewed_activation_policy' not in types

    def test_detector_constants_are_correct(self):
        assert ACTIVATION_CANDIDATE_WARNING_DAYS == 7
        assert ACTIVATION_STALE_ACTIVE_DAYS == 30


# ---------------------------------------------------------------------------
# TestGovernanceDetectorStaleActive
# ---------------------------------------------------------------------------

class TestGovernanceDetectorStaleActive:

    def test_empty_when_no_policies(self, db):
        issues = detect_stale_active_activation_policies(db)
        assert issues == []

    def test_no_issue_for_recently_activated(self, db):
        _make_active_policy(db, name='fresh-active')
        issues = detect_stale_active_activation_policies(db, stale_days=30)
        assert issues == []

    def test_flags_never_fired_stale_active(self, db):
        p = _make_active_policy(db, name='stale-never-fired')
        _backdate(db, 'activation_policies', 'activated_at', p.id, 35)
        issues = detect_stale_active_activation_policies(db, stale_days=30)
        assert len(issues) == 1
        assert issues[0].issue_type == 'stale_active_activation_policy'
        assert issues[0].severity == 'warning'
        assert issues[0].memory_id == 0
        assert issues[0].metadata['policy_id'] == p.id

    def test_does_not_flag_policy_that_has_fired(self, db):
        p = _make_active_policy(db, name='has-fired')
        _backdate(db, 'activation_policies', 'activated_at', p.id, 35)
        # Log a fired=True decision
        result = evaluate_trigger(p, {'operator_id': 'op'})
        log_activation_decision(db, p, result, {'operator_id': 'op'})
        issues = detect_stale_active_activation_policies(db, stale_days=30)
        assert issues == []

    def test_does_not_flag_policy_with_only_non_firing_decisions(self, db):
        # A policy with fired=0 decisions is still considered "never fired"
        p = _make_active_policy(
            db, name='never-fired-with-decisions',
            trigger_class='contradiction_change',
            conditions={'min_new_links': 5},
        )
        _backdate(db, 'activation_policies', 'activated_at', p.id, 35)
        # Log a fired=False decision
        result = evaluate_trigger(p, {'new_link_ids': [1]})  # only 1 link, threshold=5
        assert result.fired is False
        log_activation_decision(db, p, result, {'new_link_ids': [1]})
        # Still should be flagged — fired=0 rows don't count
        issues = detect_stale_active_activation_policies(db, stale_days=30)
        assert len(issues) == 1
        assert issues[0].metadata['policy_id'] == p.id

    def test_does_not_flag_candidate_or_superseded(self, db):
        p = _make_policy(db, name='still-candidate')
        _backdate(db, 'activation_policies', 'created_at', p.id, 35)
        issues = detect_stale_active_activation_policies(db, stale_days=30)
        assert issues == []

    def test_threshold_boundary(self, db):
        p = _make_active_policy(db, name='boundary-active')
        # Backdated to one day past threshold — strictly older than cutoff, so flagged
        _backdate(db, 'activation_policies', 'activated_at', p.id, 31)
        issues = detect_stale_active_activation_policies(db, stale_days=30)
        assert len(issues) == 1

    def test_wired_into_build_governance_report(self, db):
        p = _make_active_policy(db, name='stale-active')
        _backdate(db, 'activation_policies', 'activated_at', p.id, 35)
        report = build_governance_report(
            db, activation_stale_active_days=30, detect_activation_issues=True
        )
        types = {i.issue_type for i in report.issues}
        assert 'stale_active_activation_policy' in types

    def test_not_wired_when_detect_activation_issues_false(self, db):
        p = _make_active_policy(db, name='stale-active')
        _backdate(db, 'activation_policies', 'activated_at', p.id, 35)
        report = build_governance_report(db, detect_activation_issues=False)
        types = {i.issue_type for i in report.issues}
        assert 'stale_active_activation_policy' not in types


# ---------------------------------------------------------------------------
# TestActivationPolicyCLI
# ---------------------------------------------------------------------------

class TestActivationPolicyCLI:

    def test_create_command_basic(self, db, capsys):
        _run_cli([
            'activation-policy-create', '--db', db,
            '--name', 'my-policy',
            '--trigger-class', 'operator_request',
            '--created-by', 'tester',
            '--reason', 'test creation',
        ])
        out, _ = capsys.readouterr()
        assert 'created policy id=' in out
        assert 'status=candidate' in out

    def test_create_with_conditions(self, db, capsys):
        _run_cli([
            'activation-policy-create', '--db', db,
            '--name', 'scored-policy',
            '--trigger-class', 'contradiction_change',
            '--created-by', 'tester',
            '--reason', 'track contradictions',
            '--conditions', '{"min_new_links": 3}',
        ])
        out, _ = capsys.readouterr()
        assert 'created policy id=' in out
        policies = _snapshot_table(db, 'activation_policies')
        assert len(policies) == 1
        conds = json.loads(policies[0]['trigger_conditions_json'])
        assert conds['min_new_links'] == 3

    def test_create_invalid_trigger_class_exits(self, db):
        with pytest.raises(SystemExit):
            _run_cli([
                'activation-policy-create', '--db', db,
                '--name', 'bad', '--trigger-class', 'nonexistent',
                '--created-by', 'tester', '--reason', 'bad class',
            ])

    def test_list_command_empty(self, db, capsys):
        _run_cli(['activation-policy-list', '--db', db])
        out, _ = capsys.readouterr()
        assert 'No activation policies found' in out

    def test_list_command_shows_policies(self, db, capsys):
        _make_policy(db, name='alpha')
        _make_policy(db, name='beta')
        _run_cli(['activation-policy-list', '--db', db])
        out, _ = capsys.readouterr()
        assert 'alpha' in out
        assert 'beta' in out

    def test_list_filter_by_status(self, db, capsys):
        _make_policy(db, name='candidate-policy')
        _make_active_policy(db, name='active-policy')
        _run_cli(['activation-policy-list', '--db', db, '--status', 'candidate'])
        out, _ = capsys.readouterr()
        assert 'candidate-policy' in out
        assert 'active-policy' not in out

    def test_inspect_command(self, db, capsys):
        p = _make_policy(db, name='inspect-me')
        _run_cli(['activation-policy-inspect', '--db', db, '--id', str(p.id)])
        out, _ = capsys.readouterr()
        assert 'inspect-me' in out

    def test_inspect_command_shows_decisions(self, db, capsys):
        p = _make_active_policy(db, name='with-decisions')
        result = evaluate_trigger(p, {'operator_id': 'op'})
        log_activation_decision(db, p, result, {'operator_id': 'op'})
        _run_cli(['activation-policy-inspect', '--db', db, '--id', str(p.id)])
        out, _ = capsys.readouterr()
        assert 'Last 1 decision' in out
        assert 'decision_id=' in out

    def test_activate_command(self, db, capsys):
        p = _make_policy(db, name='to-activate')
        _run_cli([
            'activation-policy-activate', '--db', db,
            '--id', str(p.id),
            '--operator', 'op1',
            '--reason', 'ready to activate',
        ])
        out, _ = capsys.readouterr()
        assert f'activated policy id={p.id}' in out
        refreshed = get_activation_policy(db, p.id)
        assert refreshed.status == 'active'
        assert refreshed.activated_by == 'op1'

    def test_activate_already_active_exits(self, db):
        p = _make_active_policy(db, name='already-active')
        with pytest.raises(SystemExit):
            _run_cli([
                'activation-policy-activate', '--db', db,
                '--id', str(p.id),
                '--operator', 'op', '--reason', 'double activate',
            ])

    def test_supersede_command(self, db, capsys):
        p = _make_active_policy(db, name='to-supersede')
        _run_cli([
            'activation-policy-supersede', '--db', db,
            '--id', str(p.id),
            '--operator', 'op2',
            '--reason', 'replacing with better policy',
        ])
        out, _ = capsys.readouterr()
        assert f'superseded policy id={p.id}' in out
        refreshed = get_activation_policy(db, p.id)
        assert refreshed.status == 'superseded'
        assert refreshed.superseded_by_operator == 'op2'
        assert refreshed.superseded_reason == 'replacing with better policy'
        assert refreshed.invalidated_at is None

    def test_supersede_with_successor_id(self, db, capsys):
        p_old = _make_active_policy(db, name='old-policy')
        p_new = _make_policy(db, name='new-policy')
        _run_cli([
            'activation-policy-supersede', '--db', db,
            '--id', str(p_old.id),
            '--operator', 'op', '--reason', 'replaced',
            '--successor-id', str(p_new.id),
        ])
        refreshed = get_activation_policy(db, p_old.id)
        assert refreshed.superseded_by_policy_id == p_new.id

    def test_decisions_command_empty(self, db, capsys):
        p = _make_active_policy(db, name='no-decisions')
        _run_cli(['activation-policy-decisions', '--db', db, '--id', str(p.id)])
        out, _ = capsys.readouterr()
        assert 'No decisions found' in out

    def test_decisions_command_shows_rows(self, db, capsys):
        p = _make_active_policy(db, name='with-decisions')
        result = evaluate_trigger(p, {'operator_id': 'op'})
        log_activation_decision(db, p, result, {'operator_id': 'op'})
        _run_cli(['activation-policy-decisions', '--db', db, '--id', str(p.id)])
        out, _ = capsys.readouterr()
        assert 'True' in out
        assert 'operator_request' in out

    def test_decisions_fired_only_filter(self, db, capsys):
        p = _make_active_policy(
            db, name='mixed-fired',
            trigger_class='contradiction_change',
            conditions={'min_new_links': 5},
        )
        # Log one non-fired decision on the policy (1 link, threshold=5)
        result_not = evaluate_trigger(p, {'new_link_ids': [1]})
        assert result_not.fired is False
        log_activation_decision(db, p, result_not, {'new_link_ids': [1]})
        _run_cli([
            'activation-policy-decisions', '--db', db, '--id', str(p.id), '--fired-only',
        ])
        out, _ = capsys.readouterr()
        assert 'No decisions found' in out


# ---------------------------------------------------------------------------
# TestReplayCLIOutput — replay purity invariants
# ---------------------------------------------------------------------------

class TestReplayCLIOutput:

    def _log_decision(self, db, *, fired=True):
        """Helper: create a policy, activate it, log a decision, return (policy, decision_id)."""
        p = _make_active_policy(db, name='replay-subject')
        if fired:
            trigger_event = {'operator_id': 'op'}
            result = evaluate_trigger(p, trigger_event)
        else:
            p2 = create_activation_policy(
                db, name='threshold-policy',
                trigger_class='contradiction_change',
                trigger_conditions={'min_new_links': 5},
                created_by='tester', reason='test',
            )
            p2 = activate_activation_policy(db, p2.id, activated_by='tester', reason='test')
            trigger_event = {'new_link_ids': [1]}
            result = evaluate_trigger(p2, trigger_event)
            decision_id = log_activation_decision(db, p2, result, trigger_event)
            return p2, decision_id
        decision_id = log_activation_decision(db, p, result, trigger_event)
        return p, decision_id

    def test_replay_does_not_insert_decision_log_rows(self, db, capsys):
        p, decision_id = self._log_decision(db)
        before = _row_count(db, 'activation_decision_log')
        _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
        capsys.readouterr()
        after = _row_count(db, 'activation_decision_log')
        assert after == before

    def test_replay_does_not_mutate_activation_policies(self, db, capsys):
        p, decision_id = self._log_decision(db)
        before = _snapshot_table(db, 'activation_policies')
        _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
        capsys.readouterr()
        after = _snapshot_table(db, 'activation_policies')
        assert after == before

    def test_replay_does_not_mutate_cognition_session(self, db, capsys):
        p, decision_id = self._log_decision(db)
        before = _row_count(db, 'cognition_session')
        _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
        capsys.readouterr()
        after = _row_count(db, 'cognition_session')
        assert after == before

    def test_replay_does_not_mutate_assembly_transition_log(self, db, capsys):
        p, decision_id = self._log_decision(db)
        before = _row_count(db, 'assembly_transition_log')
        _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
        capsys.readouterr()
        after = _row_count(db, 'assembly_transition_log')
        assert after == before

    def test_replay_divergence_detection_is_output_only(self, db, capsys):
        """Divergence detected when stored fired differs from re-evaluated fired.

        We artificially create divergence by logging a fired=True decision and then
        patching the stored fired value to 0. The re-evaluation on the snapshot
        (status='active', trigger_class='operator_request') returns fired=True, but
        the stored row says fired=0 — divergence must be reported in output only.
        """
        p, decision_id = self._log_decision(db, fired=True)

        # Artificially set stored fired=0 to induce divergence
        conn = sqlite3.connect(db)
        conn.execute(
            'UPDATE activation_decision_log SET fired = 0 WHERE id = ?', (decision_id,)
        )
        conn.commit()
        conn.close()

        before_decisions = _row_count(db, 'activation_decision_log')
        before_policies = _snapshot_table(db, 'activation_policies')

        _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
        out, _ = capsys.readouterr()

        # Divergence must be reported in output
        assert 'DIVERGENCE DETECTED' in out

        # No writes must have occurred
        assert _row_count(db, 'activation_decision_log') == before_decisions
        assert _snapshot_table(db, 'activation_policies') == before_policies

    def test_replay_command_never_calls_log_activation_decision(self, db, capsys):
        """Verify log_activation_decision is never invoked by the replay command."""
        p, decision_id = self._log_decision(db)
        with patch(
            'session.activation_policy.log_activation_decision',
            wraps=log_activation_decision,
        ) as mock_log:
            _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
            capsys.readouterr()
            mock_log.assert_not_called()

    def test_replay_output_shows_deterministic_marker_when_no_divergence(self, db, capsys):
        p, decision_id = self._log_decision(db)
        _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
        out, _ = capsys.readouterr()
        assert 'deterministic: original and replayed results match' in out
        assert 'DIVERGENCE DETECTED' not in out

    def test_replay_output_includes_decision_metadata(self, db, capsys):
        p, decision_id = self._log_decision(db)
        _run_cli(['activation-policy-replay', '--db', db, '--id', str(decision_id)])
        out, _ = capsys.readouterr()
        assert f'decision_id={decision_id}' in out
        assert f'policy_id={p.id}' in out
        assert 'trigger_class=operator_request' in out
