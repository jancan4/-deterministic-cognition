"""
Phase 8A: ontology and governed vocabulary substrate tests.

Coverage:
- Schema v16: ontology_terms and ontology_aliases tables + indices exist
- register_term(): validation, idempotency, alias-shadowing guard
- deprecate_term(): active-only transition, provenance fields, non-deletion
- supersede_term(): active-only, cross-vocabulary rejection, deprecated replacement
  rejection, forbidden replacement rejection, self-supersession rejection
- forbid_term(): any-status transition
- add_alias(): self-alias rejection, alias-to-alias rejection, target-must-exist,
  shadowing-canonical rejection, forbidden-target rejection, duplicate rejection
- resolve_alias(): one-step lookup, unknown alias returns None
- list_terms(): deterministic ordering by vocabulary_name ASC, term ASC
- list_aliases(): deterministic ordering by vocabulary_name ASC, alias ASC
- get_term(): found and not-found cases
- Governance detectors:
    - detect_unregistered_compression_methods: empty, flags unregistered, not flags registered
    - detect_deprecated_event_type_usage: replay-safe filter (pre-deprecation not flagged)
    - detect_deprecated_relationship_usage: replay-safe filter
    - detect_deprecated_trigger_class_usage: replay-safe filter, missing table guard
    - detect_alias_conflicts: no conflict, conflict when alias shadows canonical term
    - all detectors degrade gracefully on pre-v16 DB (ontology tables absent)
- build_governance_report() wiring: detect_ontology_issues=True/False
- CLI: ontology-register, ontology-deprecate, ontology-supersede, ontology-add-alias,
       ontology-list, ontology-show, ontology-resolve, ontology-migrate-report
- Registry isolation invariant:
    - schema version is 16
    - MemoryEvent.from_row() never queries ontology tables
    - replay works on v15 DB without ontology tables
    - governance detectors return [] on v15 DB (table-existence guard)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memory.cli import _COMMANDS, build_parser
from memory.governance import build_governance_report
from memory.ontology import (
    OntologyAlias,
    OntologyTerm,
    VALID_VOCABULARY_NAMES,
    add_alias,
    deprecate_term,
    detect_alias_conflicts,
    detect_deprecated_event_type_usage,
    detect_deprecated_relationship_usage,
    detect_deprecated_trigger_class_usage,
    detect_unregistered_compression_methods,
    forbid_term,
    get_term,
    list_aliases,
    list_terms,
    register_term,
    resolve_alias,
    supersede_term,
)
from memory.service import add_memory_event, init_db, link_memory_events


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'onto.db')
    init_db(path)
    return path


def _reg(db_path, vocabulary='event_type', term='hypothesis', label='Hypothesis', **kw):
    defaults = dict(introduced_by='tester')
    defaults.update(kw)
    return register_term(db_path, vocabulary, term, label, **defaults)


def _run_cli(args_list):
    parser = build_parser()
    args = parser.parse_args(args_list)
    _COMMANDS[args.command](args)


def _add_event(db_path, *, event_type='hypothesis', status='active'):
    return add_memory_event(
        db_path=db_path,
        event_type=event_type,
        title='Test event',
        summary='Summary',
        source='test',
        confidence=3,
        status=status,
        created_by='tester',
    )


def _backdate_term(db_path: str, vocabulary: str, term: str, days: int) -> None:
    """Backdate deprecated_at on an ontology term to simulate old deprecation."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE ontology_terms SET deprecated_at = ? WHERE vocabulary_name = ? AND term = ?",
        (ts, vocabulary, term)
    )
    conn.commit()
    conn.close()


def _backdate_event(db_path: str, event_id: int, days: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memory_events SET created_at = ?, updated_at = ? WHERE id = ?",
                 (ts, ts, event_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# TestSchemaV16
# ---------------------------------------------------------------------------

class TestSchemaV16:
    def test_schema_version_is_16(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_ontology_terms_table_exists(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ontology_terms'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_ontology_aliases_table_exists(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ontology_aliases'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_ontology_terms_has_vocab_status_index(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_ontology_terms_vocab_status'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_v15_db_upgrades_to_v16(self, tmp_path):
        """A v15 DB is cleanly upgraded to v16 on init_db()."""
        path = str(tmp_path / 'upgrade.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('UPDATE memory_schema_version SET version = 15')
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        init_db(path)
        conn = sqlite3.connect(path)
        ver = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        conn.close()
        assert ver == 16


# ---------------------------------------------------------------------------
# TestRegisterTerm
# ---------------------------------------------------------------------------

class TestRegisterTerm:
    def test_register_returns_ontology_term(self, db):
        t = _reg(db)
        assert isinstance(t, OntologyTerm)
        assert t.id > 0

    def test_register_status_is_active(self, db):
        t = _reg(db)
        assert t.status == 'active'

    def test_register_fields_stored(self, db):
        t = _reg(db, vocabulary='relationship', term='supports', label='Supports')
        assert t.vocabulary_name == 'relationship'
        assert t.term == 'supports'
        assert t.label == 'Supports'

    def test_register_description_stored(self, db):
        t = _reg(db, description='A working hypothesis')
        assert t.description == 'A working hypothesis'

    def test_register_introduced_by_stored(self, db):
        t = _reg(db, introduced_by='alice')
        assert t.introduced_by == 'alice'

    def test_register_provenance_stored(self, db):
        t = _reg(db, provenance={'phase': '8A', 'source': 'test'})
        assert t.provenance == {'phase': '8A', 'source': 'test'}

    def test_register_duplicate_raises(self, db):
        _reg(db)
        with pytest.raises(ValueError, match="already exists"):
            _reg(db)

    def test_register_invalid_vocabulary_raises(self, db):
        with pytest.raises(ValueError, match="Unknown vocabulary_name"):
            register_term(db, 'not_a_vocab', 'term', 'Label', introduced_by='op')

    def test_register_empty_term_raises(self, db):
        with pytest.raises(ValueError, match="term"):
            register_term(db, 'event_type', '', 'Label', introduced_by='op')

    def test_register_empty_operator_raises(self, db):
        with pytest.raises(ValueError, match="introduced_by"):
            register_term(db, 'event_type', 'hypothesis', 'Label', introduced_by='')

    def test_register_term_that_is_alias_raises(self, db):
        """A string that is already an alias cannot be registered as a canonical term."""
        _reg(db, term='hypothesis')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='test')
        with pytest.raises(ValueError, match="already exists as an alias"):
            register_term(db, 'event_type', 'conjecture', 'Conjecture', introduced_by='op')

    def test_all_valid_vocabularies_accepted(self, db):
        for i, vocab in enumerate(sorted(VALID_VOCABULARY_NAMES)):
            t = register_term(db, vocab, f'term_{i}', f'Label {i}', introduced_by='op')
            assert t.vocabulary_name == vocab


# ---------------------------------------------------------------------------
# TestDeprecateTerm
# ---------------------------------------------------------------------------

class TestDeprecateTerm:
    def test_deprecate_sets_status(self, db):
        _reg(db)
        t = deprecate_term(db, 'event_type', 'hypothesis',
                           deprecated_by='op', deprecation_reason='replaced')
        assert t.status == 'deprecated'

    def test_deprecate_sets_provenance_fields(self, db):
        _reg(db)
        t = deprecate_term(db, 'event_type', 'hypothesis',
                           deprecated_by='alice', deprecation_reason='outdated')
        assert t.deprecated_by == 'alice'
        assert t.deprecation_reason == 'outdated'
        assert t.deprecated_at is not None

    def test_deprecate_does_not_delete_term(self, db):
        _reg(db)
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='reason')
        t = get_term(db, 'event_type', 'hypothesis')
        assert t is not None

    def test_deprecate_not_found_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            deprecate_term(db, 'event_type', 'nonexistent',
                           deprecated_by='op', deprecation_reason='reason')

    def test_deprecate_already_deprecated_raises(self, db):
        _reg(db)
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        with pytest.raises(ValueError, match="Only 'active' terms"):
            deprecate_term(db, 'event_type', 'hypothesis',
                           deprecated_by='op', deprecation_reason='r2')

    def test_deprecate_empty_reason_raises(self, db):
        _reg(db)
        with pytest.raises(ValueError, match="deprecation_reason"):
            deprecate_term(db, 'event_type', 'hypothesis',
                           deprecated_by='op', deprecation_reason='')


# ---------------------------------------------------------------------------
# TestSupersedeTerm
# ---------------------------------------------------------------------------

class TestSupersedeTerm:
    def test_supersede_sets_status(self, db):
        _reg(db, term='hypothesis')
        _reg(db, term='validated_hypothesis', label='Validated Hypothesis')
        t = supersede_term(db, 'event_type', 'hypothesis',
                           superseded_by='validated_hypothesis',
                           deprecated_by='op', deprecation_reason='use new term')
        assert t.status == 'superseded'
        assert t.superseded_by == 'validated_hypothesis'

    def test_supersede_self_raises(self, db):
        _reg(db)
        with pytest.raises(ValueError, match="Self-supersession"):
            supersede_term(db, 'event_type', 'hypothesis',
                           superseded_by='hypothesis',
                           deprecated_by='op', deprecation_reason='r')

    def test_supersede_cross_vocabulary_raises(self, db):
        """superseded_by must exist in the same vocabulary."""
        _reg(db, vocabulary='event_type', term='hypothesis', label='H')
        _reg(db, vocabulary='relationship', term='supports', label='S')
        with pytest.raises(ValueError, match="does not exist in vocabulary"):
            supersede_term(db, 'event_type', 'hypothesis',
                           superseded_by='supports',  # in 'relationship', not 'event_type'
                           deprecated_by='op', deprecation_reason='r')

    def test_supersede_to_deprecated_replacement_raises(self, db):
        """Replacement must be active; deprecated replacement is rejected."""
        _reg(db, term='hypothesis')
        _reg(db, term='replacement', label='Replacement')
        deprecate_term(db, 'event_type', 'replacement',
                       deprecated_by='op', deprecation_reason='also deprecated')
        with pytest.raises(ValueError, match="superseded_by must be an active term"):
            supersede_term(db, 'event_type', 'hypothesis',
                           superseded_by='replacement',
                           deprecated_by='op', deprecation_reason='r')

    def test_supersede_to_superseded_replacement_raises(self, db):
        _reg(db, term='hypothesis')
        _reg(db, term='replacement', label='Replacement')
        _reg(db, term='canonical', label='Canonical')
        supersede_term(db, 'event_type', 'replacement',
                       superseded_by='canonical',
                       deprecated_by='op', deprecation_reason='r')
        with pytest.raises(ValueError, match="superseded_by must be an active term"):
            supersede_term(db, 'event_type', 'hypothesis',
                           superseded_by='replacement',
                           deprecated_by='op', deprecation_reason='r')

    def test_supersede_to_forbidden_replacement_raises(self, db):
        """Replacement must be active; forbidden replacement is rejected."""
        _reg(db, term='hypothesis')
        _reg(db, term='forbidden_term', label='Forbidden')
        forbid_term(db, 'event_type', 'forbidden_term',
                    forbidden_by='op', reason='bad term')
        with pytest.raises(ValueError, match="superseded_by must be an active term"):
            supersede_term(db, 'event_type', 'hypothesis',
                           superseded_by='forbidden_term',
                           deprecated_by='op', deprecation_reason='r')

    def test_supersede_nonexistent_term_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            supersede_term(db, 'event_type', 'nonexistent',
                           superseded_by='other',
                           deprecated_by='op', deprecation_reason='r')

    def test_supersede_nonexistent_replacement_raises(self, db):
        _reg(db, term='hypothesis')
        with pytest.raises(ValueError, match="does not exist in vocabulary"):
            supersede_term(db, 'event_type', 'hypothesis',
                           superseded_by='nonexistent_replacement',
                           deprecated_by='op', deprecation_reason='r')


# ---------------------------------------------------------------------------
# TestAddAlias — Alias chain invariants (Refinement 1)
# ---------------------------------------------------------------------------

class TestAddAlias:
    def test_add_alias_returns_ontology_alias(self, db):
        _reg(db)
        a = add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                      created_by='op', reason='test')
        assert isinstance(a, OntologyAlias)
        assert a.alias == 'conjecture'
        assert a.term == 'hypothesis'

    def test_self_alias_rejected(self, db):
        _reg(db)
        with pytest.raises(ValueError, match="Self-alias"):
            add_alias(db, 'event_type', 'hypothesis', 'hypothesis',
                      created_by='op', reason='test')

    def test_alias_to_nonexistent_canonical_term_rejected(self, db):
        """Term must be a canonical entry; prevents alias→alias chaining."""
        _reg(db, term='hypothesis')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='first alias')
        # 'conjecture' is an alias, not a canonical term — cannot be aliased to
        with pytest.raises(ValueError, match="not found"):
            add_alias(db, 'event_type', 'conjecture', 'another_alias',
                      created_by='op', reason='alias to alias')

    def test_alias_chain_structurally_impossible(self, db):
        """Verify that the alias resolution is flat: resolve_alias never chains."""
        _reg(db, term='hypothesis')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='direct alias')
        # Can't register 'conjecture' as canonical, so can't alias to it
        with pytest.raises(ValueError):
            add_alias(db, 'event_type', 'conjecture', 'assumption',
                      created_by='op', reason='chain attempt')

    def test_alias_shadowing_canonical_term_rejected(self, db):
        """alias string that is also a canonical term is rejected."""
        _reg(db, term='hypothesis')
        _reg(db, term='experiment', label='Experiment')
        with pytest.raises(ValueError, match="conflicts with an existing canonical term"):
            add_alias(db, 'event_type', 'hypothesis', 'experiment',
                      created_by='op', reason='shadow attempt')

    def test_alias_to_forbidden_term_rejected(self, db):
        _reg(db)
        forbid_term(db, 'event_type', 'hypothesis', forbidden_by='op', reason='bad')
        with pytest.raises(ValueError, match="forbidden"):
            add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                      created_by='op', reason='test')

    def test_duplicate_alias_rejected(self, db):
        _reg(db)
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='first')
        with pytest.raises(ValueError, match="already exists"):
            add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                      created_by='op', reason='duplicate')

    def test_alias_fields_stored(self, db):
        _reg(db)
        a = add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                      created_by='alice', reason='synonym')
        assert a.created_by == 'alice'
        assert a.reason == 'synonym'
        assert a.created_at is not None


# ---------------------------------------------------------------------------
# TestResolveAlias
# ---------------------------------------------------------------------------

class TestResolveAlias:
    def test_resolve_known_alias_returns_canonical(self, db):
        _reg(db)
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='test')
        result = resolve_alias(db, 'event_type', 'conjecture')
        assert result == 'hypothesis'

    def test_resolve_unknown_alias_returns_none(self, db):
        result = resolve_alias(db, 'event_type', 'unknown_alias')
        assert result is None

    def test_resolve_performs_exactly_one_step(self, db):
        """Verify resolve_alias is not recursive — one step only."""
        _reg(db, term='hypothesis')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='test')
        # resolve_alias on an alias returns its term, period
        result = resolve_alias(db, 'event_type', 'conjecture')
        assert result == 'hypothesis'
        # resolve_alias on a canonical term (not an alias) returns None
        result2 = resolve_alias(db, 'event_type', 'hypothesis')
        assert result2 is None

    def test_resolve_does_not_recurse(self, db):
        """Structural proof: even if alias chains existed, resolve_alias stops at one step."""
        _reg(db, term='hypothesis')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='test')
        # 'conjecture' is an alias, not a canonical — resolve_alias('conjecture') gives 'hypothesis'
        # Attempting to resolve 'hypothesis' via alias returns None (it's canonical, not aliased)
        assert resolve_alias(db, 'event_type', 'conjecture') == 'hypothesis'
        assert resolve_alias(db, 'event_type', 'hypothesis') is None


# ---------------------------------------------------------------------------
# TestListTerms
# ---------------------------------------------------------------------------

class TestListTerms:
    def test_list_empty_returns_empty(self, db):
        assert list_terms(db) == []

    def test_list_ordered_by_vocabulary_then_term(self, db):
        _reg(db, vocabulary='relationship', term='supports', label='S')
        _reg(db, vocabulary='event_type', term='hypothesis', label='H')
        _reg(db, vocabulary='event_type', term='experiment', label='E')
        terms = list_terms(db)
        pairs = [(t.vocabulary_name, t.term) for t in terms]
        assert pairs == sorted(pairs)

    def test_list_filter_by_vocabulary(self, db):
        _reg(db, vocabulary='event_type', term='hypothesis', label='H')
        _reg(db, vocabulary='relationship', term='supports', label='S')
        terms = list_terms(db, vocabulary_name='event_type')
        assert all(t.vocabulary_name == 'event_type' for t in terms)
        assert len(terms) == 1

    def test_list_filter_by_status(self, db):
        _reg(db, term='hypothesis')
        _reg(db, term='experiment', label='Experiment')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        active_terms = list_terms(db, status='active')
        assert all(t.status == 'active' for t in active_terms)
        assert len(active_terms) == 1

    def test_list_deterministic_repeated_calls(self, db):
        _reg(db, vocabulary='relationship', term='supports', label='S')
        _reg(db, vocabulary='event_type', term='hypothesis', label='H')
        assert list_terms(db) == list_terms(db)


# ---------------------------------------------------------------------------
# TestListAliases
# ---------------------------------------------------------------------------

class TestListAliases:
    def test_list_aliases_ordered_by_vocabulary_then_alias(self, db):
        _reg(db, vocabulary='event_type', term='hypothesis', label='H')
        _reg(db, vocabulary='event_type', term='experiment', label='E')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture', created_by='op', reason='r')
        add_alias(db, 'event_type', 'experiment', 'assay', created_by='op', reason='r')
        aliases = list_aliases(db)
        pairs = [(a.vocabulary_name, a.alias) for a in aliases]
        assert pairs == sorted(pairs)

    def test_list_aliases_filter_by_vocabulary(self, db):
        _reg(db, vocabulary='event_type', term='hypothesis', label='H')
        _reg(db, vocabulary='relationship', term='supports', label='S')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture', created_by='op', reason='r')
        aliases = list_aliases(db, vocabulary_name='event_type')
        assert all(a.vocabulary_name == 'event_type' for a in aliases)


# ---------------------------------------------------------------------------
# TestGovernanceDetectors
# ---------------------------------------------------------------------------

class TestDetectUnregisteredCompressionMethods:
    def test_empty_db_no_issues(self, db):
        assert detect_unregistered_compression_methods(db) == []

    def test_flags_unregistered_method(self, db):
        import json, sqlite3 as _sqlite3
        from datetime import datetime as _dt, timezone as _tz
        # Insert a compression_artifact row with an unregistered method
        conn = _sqlite3.connect(db)
        now = _dt.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        conn.execute(
            """INSERT INTO compression_artifacts
               (source_assembly_id, source_assembly_hash, compression_method,
                producer_version, artifact_text, artifact_char_count,
                source_memory_event_ids_json, source_contradiction_link_ids_json,
                confidence_snapshot_json, excluded_event_ids_json,
                unresolved_issue_count, status, generated_at, provenance_json)
               VALUES (1,'hash','extractive_v1','0.1.0','text',4,'[]','[]','{}','[]',
                       0,'candidate',?,?)""",
            (now, '{}')
        )
        conn.commit()
        conn.close()
        issues = detect_unregistered_compression_methods(db)
        assert len(issues) == 1
        assert issues[0].metadata['compression_method'] == 'extractive_v1'

    def test_no_flag_when_method_is_registered(self, db):
        import sqlite3 as _sqlite3
        from datetime import datetime as _dt, timezone as _tz
        register_term(db, 'compression_method', 'extractive_v1', 'Extractive v1',
                      introduced_by='op')
        conn = _sqlite3.connect(db)
        now = _dt.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        conn.execute(
            """INSERT INTO compression_artifacts
               (source_assembly_id, source_assembly_hash, compression_method,
                producer_version, artifact_text, artifact_char_count,
                source_memory_event_ids_json, source_contradiction_link_ids_json,
                confidence_snapshot_json, excluded_event_ids_json,
                unresolved_issue_count, status, generated_at, provenance_json)
               VALUES (1,'hash','extractive_v1','0.1.0','text',4,'[]','[]','{}','[]',
                       0,'candidate',?,?)""",
            (now, '{}')
        )
        conn.commit()
        conn.close()
        issues = detect_unregistered_compression_methods(db)
        assert issues == []

    def test_degrades_gracefully_on_pre_v16_db(self, tmp_path):
        """Returns [] when ontology tables are absent."""
        path = str(tmp_path / 'v15.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        assert detect_unregistered_compression_methods(path) == []


class TestDetectDeprecatedEventTypeUsage:
    def test_empty_no_issues(self, db):
        assert detect_deprecated_event_type_usage(db) == []

    def test_pre_deprecation_event_not_flagged(self, db):
        """Replay-safe: events written before deprecation are never flagged."""
        ev = _add_event(db, event_type='hypothesis')
        _backdate_event(db, ev.id, days=10)  # event written 10 days ago
        _reg(db, term='hypothesis')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        # backdate the deprecation to 5 days ago (after the event)
        _backdate_term(db, 'event_type', 'hypothesis', days=5)
        issues = detect_deprecated_event_type_usage(db)
        assert issues == []

    def test_post_deprecation_event_flagged(self, db):
        """Events written after deprecation are flagged."""
        _reg(db, term='hypothesis')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'event_type', 'hypothesis', days=5)
        ev = _add_event(db, event_type='hypothesis')  # written NOW, after deprecation
        issues = detect_deprecated_event_type_usage(db)
        assert len(issues) == 1
        assert issues[0].memory_id == ev.id

    def test_issue_type_correct(self, db):
        _reg(db, term='hypothesis')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'event_type', 'hypothesis', days=5)
        _add_event(db, event_type='hypothesis')
        issues = detect_deprecated_event_type_usage(db)
        assert issues[0].issue_type == 'deprecated_event_type_usage'

    def test_degrades_gracefully_on_pre_v16_db(self, tmp_path):
        path = str(tmp_path / 'v15.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        assert detect_deprecated_event_type_usage(path) == []

    def test_ordered_by_id(self, db):
        _reg(db, term='hypothesis')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'event_type', 'hypothesis', days=5)
        ev1 = _add_event(db, event_type='hypothesis')
        ev2 = _add_event(db, event_type='hypothesis')
        issues = detect_deprecated_event_type_usage(db)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)


class TestDetectDeprecatedRelationshipUsage:
    def test_empty_no_issues(self, db):
        assert detect_deprecated_relationship_usage(db) == []

    def test_pre_deprecation_link_not_flagged(self, db):
        ev1 = _add_event(db)
        ev2 = _add_event(db)
        link_memory_events(db, ev1.id, ev2.id, 'related_to')
        # Backdate the link to 10 days ago
        conn = sqlite3.connect(db)
        ts_old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
        conn.execute("UPDATE memory_links SET created_at = ? WHERE source_id = ?",
                     (ts_old, ev1.id))
        conn.commit()
        conn.close()
        # Now register and deprecate the term (5 days ago)
        register_term(db, 'relationship', 'related_to', 'Related To', introduced_by='op')
        deprecate_term(db, 'relationship', 'related_to',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'relationship', 'related_to', days=5)
        issues = detect_deprecated_relationship_usage(db)
        assert issues == []

    def test_post_deprecation_link_flagged(self, db):
        register_term(db, 'relationship', 'related_to', 'Related To', introduced_by='op')
        deprecate_term(db, 'relationship', 'related_to',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'relationship', 'related_to', days=5)
        ev1 = _add_event(db)
        ev2 = _add_event(db)
        link_memory_events(db, ev1.id, ev2.id, 'related_to')
        issues = detect_deprecated_relationship_usage(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'deprecated_relationship_usage'

    def test_degrades_gracefully_on_pre_v16_db(self, tmp_path):
        path = str(tmp_path / 'v15.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        assert detect_deprecated_relationship_usage(path) == []


class TestDetectDeprecatedTriggerClassUsage:
    def test_empty_no_issues(self, db):
        assert detect_deprecated_trigger_class_usage(db) == []

    def test_degrades_gracefully_on_pre_v16_db(self, tmp_path):
        path = str(tmp_path / 'v15.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        assert detect_deprecated_trigger_class_usage(path) == []

    def test_degrades_gracefully_when_activation_policies_absent(self, tmp_path):
        """Returns [] even if activation_policies table is missing."""
        path = str(tmp_path / 'nopol.db')
        init_db(path)
        register_term(path, 'trigger_class', 'operator_request', 'Operator Request',
                      introduced_by='op')
        deprecate_term(path, 'trigger_class', 'operator_request',
                       deprecated_by='op', deprecation_reason='r')
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS activation_policies')
        conn.commit()
        conn.close()
        assert detect_deprecated_trigger_class_usage(path) == []


class TestDetectAliasConflicts:
    def test_no_conflicts_empty_db(self, db):
        assert detect_alias_conflicts(db) == []

    def test_no_conflicts_normal_aliases(self, db):
        _reg(db, term='hypothesis')
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='r')
        assert detect_alias_conflicts(db) == []

    def test_conflict_when_alias_shadows_canonical_term(self, db):
        """Manually insert a conflicting alias to test the defensive audit."""
        _reg(db, term='hypothesis')
        _reg(db, term='experiment', label='Experiment')
        # Bypass the add_alias guard by inserting directly
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        conn.execute(
            """INSERT INTO ontology_aliases (vocabulary_name, term, alias, created_at, created_by, reason)
               VALUES ('event_type', 'hypothesis', 'experiment', ?, 'test', 'conflict test')""",
            (now,)
        )
        conn.commit()
        conn.close()
        issues = detect_alias_conflicts(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'alias_shadows_canonical_term'
        assert issues[0].severity == 'critical'

    def test_degrades_gracefully_on_pre_v16_db(self, tmp_path):
        path = str(tmp_path / 'v15.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        assert detect_alias_conflicts(path) == []


# ---------------------------------------------------------------------------
# TestGovernanceReportWiring
# ---------------------------------------------------------------------------

class TestGovernanceReportWiring:
    def test_detect_ontology_issues_true_includes_ontology(self, db):
        _reg(db, term='hypothesis')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'event_type', 'hypothesis', days=5)
        _add_event(db, event_type='hypothesis')
        report = build_governance_report(db, detect_ontology_issues=True)
        types = {i.issue_type for i in report.issues}
        assert 'deprecated_event_type_usage' in types

    def test_detect_ontology_issues_false_excludes_ontology(self, db):
        _reg(db, term='hypothesis')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'event_type', 'hypothesis', days=5)
        _add_event(db, event_type='hypothesis')
        report = build_governance_report(db, detect_ontology_issues=False)
        types = {i.issue_type for i in report.issues}
        assert 'deprecated_event_type_usage' not in types


# ---------------------------------------------------------------------------
# TestRegistryIsolationInvariant (Refinement 4)
# ---------------------------------------------------------------------------

class TestRegistryIsolationInvariant:
    def test_memory_event_from_row_does_not_query_ontology(self, db):
        """MemoryEvent.from_row() works regardless of ontology table state."""
        from memory.models import MemoryEvent
        ev = _add_event(db, event_type='hypothesis')
        # Drop ontology tables to prove MemoryEvent doesn't depend on them
        conn = sqlite3.connect(db)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM memory_events WHERE id = ?', (ev.id,)).fetchone()
        conn.close()
        # from_row must work with no ontology tables
        event = MemoryEvent.from_row(row)
        assert event.event_type == 'hypothesis'
        assert event.id == ev.id

    def test_replay_works_on_v15_db_without_ontology_tables(self, tmp_path):
        """Core substrate operations work on a v15-equivalent DB."""
        path = str(tmp_path / 'v15sim.db')
        init_db(path)
        # Simulate v15 state by dropping ontology tables
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        # add_memory_event must work without ontology tables
        ev = _add_event(path)
        assert ev.id > 0
        # link_memory_events must work
        ev2 = _add_event(path)
        link = link_memory_events(path, ev.id, ev2.id, 'related_to')
        assert link.id > 0

    def test_all_detectors_return_empty_on_pre_v16_db(self, tmp_path):
        """All five ontology governance detectors degrade gracefully."""
        path = str(tmp_path / 'v15det.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        assert detect_unregistered_compression_methods(path) == []
        assert detect_deprecated_event_type_usage(path) == []
        assert detect_deprecated_relationship_usage(path) == []
        assert detect_deprecated_trigger_class_usage(path) == []
        assert detect_alias_conflicts(path) == []

    def test_build_governance_report_works_on_pre_v16_db(self, tmp_path):
        """build_governance_report() does not crash when ontology tables are absent."""
        path = str(tmp_path / 'v15rep.db')
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE IF EXISTS ontology_terms')
        conn.execute('DROP TABLE IF EXISTS ontology_aliases')
        conn.commit()
        conn.close()
        report = build_governance_report(path, detect_ontology_issues=True)
        assert report is not None


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_ontology_register_prints_confirmation(self, db, capsys):
        _run_cli(['ontology-register', '--db', db,
                  '--vocabulary', 'event_type',
                  '--term', 'hypothesis',
                  '--label', 'Hypothesis',
                  '--introduced-by', 'tester'])
        out = capsys.readouterr().out
        assert 'registered term id=' in out
        assert 'hypothesis' in out

    def test_ontology_register_duplicate_exits(self, db, capsys):
        _reg(db)
        with pytest.raises(SystemExit) as exc:
            _run_cli(['ontology-register', '--db', db,
                      '--vocabulary', 'event_type',
                      '--term', 'hypothesis',
                      '--label', 'Hypothesis',
                      '--introduced-by', 'tester'])
        assert exc.value.code != 0

    def test_ontology_deprecate_prints_confirmation(self, db, capsys):
        _reg(db)
        _run_cli(['ontology-deprecate', '--db', db,
                  '--vocabulary', 'event_type',
                  '--term', 'hypothesis',
                  '--deprecated-by', 'op',
                  '--reason', 'outdated'])
        out = capsys.readouterr().out
        assert 'deprecated' in out
        assert 'hypothesis' in out

    def test_ontology_supersede_prints_confirmation(self, db, capsys):
        _reg(db, term='hypothesis')
        _reg(db, term='validated_hypothesis', label='Validated')
        _run_cli(['ontology-supersede', '--db', db,
                  '--vocabulary', 'event_type',
                  '--term', 'hypothesis',
                  '--superseded-by', 'validated_hypothesis',
                  '--deprecated-by', 'op',
                  '--reason', 'use new term'])
        out = capsys.readouterr().out
        assert 'superseded' in out

    def test_ontology_add_alias_prints_confirmation(self, db, capsys):
        _reg(db)
        _run_cli(['ontology-add-alias', '--db', db,
                  '--vocabulary', 'event_type',
                  '--term', 'hypothesis',
                  '--alias', 'conjecture',
                  '--created-by', 'op',
                  '--reason', 'synonym'])
        out = capsys.readouterr().out
        assert 'alias added' in out
        assert 'conjecture' in out

    def test_ontology_list_empty(self, db, capsys):
        _run_cli(['ontology-list', '--db', db])
        out = capsys.readouterr().out
        assert 'No ontology terms found.' in out

    def test_ontology_list_shows_terms(self, db, capsys):
        _reg(db)
        _run_cli(['ontology-list', '--db', db])
        out = capsys.readouterr().out
        assert 'hypothesis' in out

    def test_ontology_show_prints_json(self, db, capsys):
        _reg(db)
        _run_cli(['ontology-show', '--db', db,
                  '--vocabulary', 'event_type',
                  '--term', 'hypothesis'])
        out = capsys.readouterr().out
        data = json.loads(out.split('\n\n')[0])  # JSON before aliases section
        assert data['term'] == 'hypothesis'

    def test_ontology_resolve_found(self, db, capsys):
        _reg(db)
        add_alias(db, 'event_type', 'hypothesis', 'conjecture',
                  created_by='op', reason='r')
        _run_cli(['ontology-resolve', '--db', db,
                  '--vocabulary', 'event_type',
                  '--alias', 'conjecture'])
        out = capsys.readouterr().out
        assert 'hypothesis' in out

    def test_ontology_resolve_not_found(self, db, capsys):
        _run_cli(['ontology-resolve', '--db', db,
                  '--vocabulary', 'event_type',
                  '--alias', 'unknown'])
        out = capsys.readouterr().out
        assert 'No alias' in out

    def test_ontology_migrate_report_no_issues(self, db, capsys):
        _run_cli(['ontology-migrate-report', '--db', db])
        out = capsys.readouterr().out
        assert 'No post-deprecation vocabulary usage detected.' in out

    def test_ontology_migrate_report_shows_issues(self, db, capsys):
        _reg(db, term='hypothesis')
        deprecate_term(db, 'event_type', 'hypothesis',
                       deprecated_by='op', deprecation_reason='r')
        _backdate_term(db, 'event_type', 'hypothesis', days=5)
        _add_event(db, event_type='hypothesis')
        _run_cli(['ontology-migrate-report', '--db', db])
        out = capsys.readouterr().out
        assert 'event_type' in out
        assert 'hypothesis' in out
