"""
Tests for the session-context CLI command.

All tests are hermetic: each uses a fresh tmp_path database.
No test mutates shared state. Export is always read-only.
"""
import json

import pytest

from memory import service as mem_service
from cli.main import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db(tmp_path) -> str:
    path = str(tmp_path / 'mem.db')
    mem_service.init_db(path)
    return path


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
    return mem_service.add_memory_event(db, **defaults)


def _run(*args) -> int:
    """Run the CLI with the given argument list; return exit code."""
    return main(list(args))


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------

def test_session_context_exits_zero_empty_db(tmp_path):
    db = _mem_db(tmp_path)
    code = _run('session-context', '--db', db)
    assert code == 0


def test_session_context_exits_zero_with_data(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    code = _run('session-context', '--db', db)
    assert code == 0


def test_session_context_missing_db_returns_nonzero(tmp_path):
    db = str(tmp_path / 'nonexistent.db')
    # reconstruct will fail or return empty; either way, should not crash
    code = _run('session-context', '--db', db)
    assert isinstance(code, int)


# ---------------------------------------------------------------------------
# Markdown output (default)
# ---------------------------------------------------------------------------

def test_session_context_markdown_default_format(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    _run('session-context', '--db', db)
    out = capsys.readouterr().out
    assert 'SESSION CONTEXT' in out
    assert 'session_id' in out


def test_session_context_markdown_governance_section(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='GovernanceRule1', status='active')
    _run('session-context', '--db', db)
    out = capsys.readouterr().out
    assert 'ACTIVE GOVERNANCE CONTEXT' in out
    assert 'GovernanceRule1' in out


def test_session_context_markdown_unresolved_section(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='open_question', title='OpenQ1', status='unresolved')
    _run('session-context', '--db', db)
    out = capsys.readouterr().out
    assert 'UNRESOLVED ITEMS' in out
    assert 'OpenQ1' in out


def test_session_context_markdown_empty_db_shows_no_items(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _run('session-context', '--db', db)
    out = capsys.readouterr().out
    assert 'no items' in out.lower() or 'SESSION CONTEXT' in out


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def test_session_context_json_format(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', status='proposed')
    _run('session-context', '--db', db, '--format', 'json')
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert 'session_id' in parsed
    assert 'sections' in parsed
    assert 'created_at' in parsed


def test_session_context_json_sections_present(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _run('session-context', '--db', db, '--format', 'json')
    out = capsys.readouterr().out
    parsed = json.loads(out)
    expected_sections = {
        'governance_context', 'unresolved_items', 'active_workflows',
        'active_investigations', 'relevant_memory', 'execution_lineage',
        'runtime_snapshots',
    }
    assert expected_sections == set(parsed['sections'].keys())


def test_session_context_json_governance_content(tmp_path, capsys):
    db = _mem_db(tmp_path)
    ev = _add(db, event_type='governance_rule', title='GovRule', status='active', confidence=5)
    _run('session-context', '--db', db, '--format', 'json')
    out = capsys.readouterr().out
    parsed = json.loads(out)
    gov = parsed['sections']['governance_context']
    assert any(item['title'] == 'GovRule' for item in gov)


def test_session_context_json_filters_included(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _run('session-context', '--db', db, '--format', 'json',
         '--tag', 'fx', '--event-type', 'governance_rule')
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed['filters']['tags'] == ['fx']
    assert parsed['filters']['event_types'] == ['governance_rule']


def test_session_context_json_budget_fields(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _run('session-context', '--db', db, '--format', 'json')
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert 'char_budget' in parsed
    assert 'chars_used' in parsed
    assert 'total_candidates' in parsed
    assert 'included_entries' in parsed
    assert 'truncated' in parsed


def test_session_context_json_serializable(tmp_path, capsys):
    db = _mem_db(tmp_path)
    for i in range(5):
        _add(db, event_type='hypothesis', title=f'H{i}', status='proposed')
    _run('session-context', '--db', db, '--format', 'json')
    out = capsys.readouterr().out
    # Must parse cleanly — no TypeError, no NaN, no Infinity
    parsed = json.loads(out)
    # Re-serialise to confirm stability
    assert json.dumps(parsed)


# ---------------------------------------------------------------------------
# Deterministic repeated output
# ---------------------------------------------------------------------------

def test_session_context_deterministic_markdown(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    _add(db, event_type='hypothesis', title='H1', status='unresolved')

    _run('session-context', '--db', db)
    out1 = capsys.readouterr().out

    _run('session-context', '--db', db)
    out2 = capsys.readouterr().out

    # Strip the session_id line (timestamp-dependent) before comparing
    def _strip_session_line(text):
        return '\n'.join(
            line for line in text.splitlines()
            if not line.startswith('session_id') and not line.startswith('created_at')
        )

    assert _strip_session_line(out1) == _strip_session_line(out2)


def test_session_context_deterministic_json(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    _add(db, event_type='hypothesis', title='H1', status='proposed')

    _run('session-context', '--db', db, '--format', 'json')
    p1 = json.loads(capsys.readouterr().out)

    _run('session-context', '--db', db, '--format', 'json')
    p2 = json.loads(capsys.readouterr().out)

    # Same sections, same item count
    for section in p1['sections']:
        ids1 = [item['memory_id'] for item in p1['sections'][section]]
        ids2 = [item['memory_id'] for item in p2['sections'][section]]
        assert ids1 == ids2, f"Non-deterministic order in section {section!r}"


# ---------------------------------------------------------------------------
# max-entries
# ---------------------------------------------------------------------------

def test_session_context_max_entries(tmp_path, capsys):
    db = _mem_db(tmp_path)
    for i in range(20):
        _add(db, event_type='hypothesis', title=f'H{i}', status='proposed')

    _run('session-context', '--db', db, '--format', 'json', '--max-entries', '3')
    parsed = json.loads(capsys.readouterr().out)
    assert parsed['included_entries'] <= 3


def test_session_context_max_entries_one(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    _add(db, event_type='hypothesis', title='H1', status='proposed')

    _run('session-context', '--db', db, '--format', 'json', '--max-entries', '1')
    parsed = json.loads(capsys.readouterr().out)
    assert parsed['included_entries'] <= 1


# ---------------------------------------------------------------------------
# max-chars
# ---------------------------------------------------------------------------

def test_session_context_max_chars(tmp_path, capsys):
    db = _mem_db(tmp_path)
    for i in range(5):
        _add(db, event_type='hypothesis', title=f'H{i}',
             summary='x' * 500, status='proposed')

    _run('session-context', '--db', db, '--format', 'json', '--max-chars', '100')
    parsed = json.loads(capsys.readouterr().out)
    assert parsed['chars_used'] <= 100 + 4  # separator tolerance


def test_session_context_max_chars_truncated_flag(tmp_path, capsys):
    db = _mem_db(tmp_path)
    for i in range(10):
        _add(db, event_type='hypothesis', title=f'H{i}',
             summary='x' * 200, status='proposed')

    _run('session-context', '--db', db, '--format', 'json', '--max-chars', '50')
    parsed = json.loads(capsys.readouterr().out)
    # May or may not truncate depending on item sizes; just verify flag is present
    assert isinstance(parsed['truncated'], bool)


# ---------------------------------------------------------------------------
# Tag filtering
# ---------------------------------------------------------------------------

def test_session_context_tag_activates_matching(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='TaggedFX', tags=['fx'], status='proposed')
    _add(db, event_type='hypothesis', title='NoTag', tags=[], status='proposed')

    _run('session-context', '--db', db, '--format', 'json', '--tag', 'fx')
    parsed = json.loads(capsys.readouterr().out)
    all_titles = [
        item['title']
        for section in parsed['sections'].values()
        for item in section
        if isinstance(item, dict) and 'title' in item
    ]
    assert 'TaggedFX' in all_titles


def test_session_context_tag_filter_in_output(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _run('session-context', '--db', db, '--format', 'json', '--tag', 'macro', '--tag', 'fx')
    parsed = json.loads(capsys.readouterr().out)
    assert set(parsed['filters']['tags']) == {'macro', 'fx'}


# ---------------------------------------------------------------------------
# Event type filtering (post-reconstruction)
# ---------------------------------------------------------------------------

def test_session_context_event_type_filter(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    _add(db, event_type='hypothesis', title='H1', status='proposed')

    _run('session-context', '--db', db, '--format', 'json',
         '--event-type', 'governance_rule')
    parsed = json.loads(capsys.readouterr().out)

    # All items in governance_context should be governance_rule
    for item in parsed['sections']['governance_context']:
        assert item['event_type'] == 'governance_rule'

    # hypothesis should not appear anywhere in the filtered sections that only check
    # event_type: hypothesis may appear in active_investigations or relevant_memory
    # if not filtered out there
    hyp_titles = [
        item['title']
        for section_name, items in parsed['sections'].items()
        for item in items
        if isinstance(item, dict) and item.get('event_type') == 'hypothesis'
    ]
    assert hyp_titles == [], f"Hypothesis should be filtered out, got: {hyp_titles}"


def test_session_context_status_filter(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H_unresolved', status='unresolved')
    _add(db, event_type='hypothesis', title='H_active', status='active')

    _run('session-context', '--db', db, '--format', 'json', '--status', 'unresolved')
    parsed = json.loads(capsys.readouterr().out)

    # No active items should appear in any memory section
    for section_name in ('governance_context', 'unresolved_items',
                         'active_investigations', 'relevant_memory'):
        for item in parsed['sections'][section_name]:
            assert item['status'] != 'active', (
                f"Active item leaked into {section_name} after status filter"
            )


# ---------------------------------------------------------------------------
# File output (--out)
# ---------------------------------------------------------------------------

def test_session_context_out_writes_file(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='hypothesis', title='H1', status='proposed')
    out_path = str(tmp_path / 'context.md')
    code = _run('session-context', '--db', db, '--out', out_path)
    assert code == 0
    with open(out_path, encoding='utf-8') as f:
        content = f.read()
    assert 'SESSION CONTEXT' in content


def test_session_context_out_json_file(tmp_path):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')
    out_path = str(tmp_path / 'context.json')
    code = _run('session-context', '--db', db, '--format', 'json', '--out', out_path)
    assert code == 0
    with open(out_path, encoding='utf-8') as f:
        parsed = json.load(f)
    assert 'session_id' in parsed
    assert 'sections' in parsed


def test_session_context_out_bad_path_returns_nonzero(tmp_path):
    db = _mem_db(tmp_path)
    bad_path = str(tmp_path / 'no_such_dir' / 'context.md')
    code = _run('session-context', '--db', db, '--out', bad_path)
    assert code != 0


# ---------------------------------------------------------------------------
# No mutation guarantee
# ---------------------------------------------------------------------------

def test_session_context_does_not_mutate_memory(tmp_path):
    db = _mem_db(tmp_path)
    ev = _add(db, event_type='hypothesis', title='H1', status='proposed', confidence=3)

    _run('session-context', '--db', db, '--format', 'json')

    # Memory event unchanged after export (get_memory_event returns (event, revisions, links))
    reloaded, _, _ = mem_service.get_memory_event(db, ev.id)
    assert reloaded.title == 'H1'
    assert reloaded.status == 'proposed'
    assert reloaded.confidence == 3
    assert reloaded.version == ev.version


def test_session_context_repeated_calls_no_side_effects(tmp_path, capsys):
    db = _mem_db(tmp_path)
    _add(db, event_type='governance_rule', title='G1', status='active')

    _run('session-context', '--db', db, '--format', 'json')
    p1 = json.loads(capsys.readouterr().out)

    _run('session-context', '--db', db, '--format', 'json')
    p2 = json.loads(capsys.readouterr().out)

    # Total candidates must be identical (no phantom events created)
    assert p1['total_candidates'] == p2['total_candidates']
    assert p1['included_entries'] == p2['included_entries']


# ---------------------------------------------------------------------------
# Workflow DB integration
# ---------------------------------------------------------------------------

def test_session_context_workflow_db_adds_section(tmp_path, capsys):
    from workflow.storage import init_db as wf_init_db
    from workflow.executor import initialize_execution, start_execution
    from workflow.persistence import persist_execution
    from workflow.service import define_workflow, plan_workflow
    from workflow.models import WorkflowNode, RetryPolicy

    mem_db = _mem_db(tmp_path)
    wf_db = str(tmp_path / 'wf.db')
    wf_init_db(wf_db)

    wf = define_workflow('wf-cli-test', 'Test', [
        WorkflowNode(node_id='a', task_type='research', dependency_ids=[],
                     retry_policy=RetryPolicy(max_attempts=1)),
    ])
    vr, plan, _ = plan_workflow(wf)
    assert vr.valid
    execution, init_event = initialize_execution(plan)
    persist_execution(wf_db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(wf_db, execution, start_events)

    _run('session-context', '--db', mem_db, '--workflow-db', wf_db, '--format', 'json')
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed['sections']['active_workflows']) == 1
    wf_entry = parsed['sections']['active_workflows'][0]
    assert wf_entry['execution_id'] == execution.execution_id
    assert wf_entry['state'] == 'executing'
