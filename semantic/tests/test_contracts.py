"""Tests for semantic/contracts.py."""
import json

import pytest

from semantic.contracts import make_result, make_task, result_to_candidate
from semantic.models import (
    ExtractedClaim,
    ExtractedEntity,
    ExtractedRelation,
    SemanticLabel,
    SemanticProvenance,
    SemanticSpan,
)
from semantic.validators import SemanticValidationError


# ---------------------------------------------------------------------------
# make_task
# ---------------------------------------------------------------------------

class TestMakeTask:
    def test_valid_task(self):
        t = make_task('tagging', 'Some input text.')
        assert t.task_type == 'tagging'
        assert t.input_text == 'Some input text.'

    def test_all_approved_task_types(self):
        from semantic.models import SEMANTIC_TASK_TYPES
        for tt in SEMANTIC_TASK_TYPES:
            t = make_task(tt, 'input')
            assert t.task_type == tt

    def test_task_id_deterministic(self):
        t1 = make_task('tagging', 'hello', source_id='src1')
        t2 = make_task('tagging', 'hello', source_id='src1')
        assert t1.task_id == t2.task_id

    def test_different_inputs_different_id(self):
        t1 = make_task('tagging', 'hello')
        t2 = make_task('tagging', 'world')
        assert t1.task_id != t2.task_id

    def test_invalid_task_type_raises(self):
        with pytest.raises(SemanticValidationError, match='task_type'):
            make_task('rate_prediction', 'some text')

    def test_empty_input_raises(self):
        with pytest.raises(SemanticValidationError, match='input_text'):
            make_task('tagging', '')

    def test_whitespace_only_input_raises(self):
        with pytest.raises(SemanticValidationError, match='input_text'):
            make_task('tagging', '   ')

    def test_source_span_optional(self):
        span = SemanticSpan(start=0, end=4)
        t = make_task('tagging', 'hello world', source_span=span)
        assert t.source_span.start == 0

    def test_source_span_out_of_range_raises(self):
        with pytest.raises(SemanticValidationError, match='exceeds text length'):
            make_task('tagging', 'hi', source_span=SemanticSpan(0, 100))

    def test_is_source_bound_with_source_id(self):
        t = make_task('entity_extraction', 'text', source_id='abc123')
        assert t.is_source_bound is True

    def test_is_source_bound_without_source_id(self):
        t = make_task('entity_extraction', 'text')
        assert t.is_source_bound is False

    def test_metadata_stored(self):
        t = make_task('tagging', 'text', metadata={'key': 'val'})
        assert t.metadata == {'key': 'val'}

    def test_created_at_set(self):
        t = make_task('tagging', 'text')
        assert t.created_at and 'T' in t.created_at


# ---------------------------------------------------------------------------
# make_result
# ---------------------------------------------------------------------------

class TestMakeResult:
    def test_valid_result(self):
        task = make_task('tagging', 'some text')
        result = make_result(task, overall_confidence=3)
        assert result.task_id == task.task_id
        assert result.task_type == 'tagging'

    def test_extraction_method_preserved(self):
        task = make_task('entity_extraction', 'some text')
        result = make_result(task, overall_confidence=4, extraction_method='pattern')
        assert result.extraction_method == 'pattern'

    def test_provenance_auto_generated(self):
        task = make_task('tagging', 'text', source_id='s1')
        result = make_result(task, overall_confidence=3)
        assert result.provenance.source_id == 's1'
        assert result.provenance.extraction_method == 'rule_based'

    def test_bad_confidence_raises(self):
        task = make_task('tagging', 'text')
        with pytest.raises(SemanticValidationError):
            make_result(task, overall_confidence=0)

    def test_source_bound_without_source_id_in_provenance_raises(self):
        task = make_task('tagging', 'text', source_id='src')
        bad_prov = SemanticProvenance(extraction_method='rule_based')  # no source_id
        with pytest.raises(SemanticValidationError, match='source_id'):
            make_result(task, overall_confidence=3, provenance=bad_prov)

    def test_labels_in_result(self):
        task = make_task('tagging', 'text')
        lb = SemanticLabel(label='usd', confidence=3)
        result = make_result(task, overall_confidence=3, labels=[lb])
        assert result.labels[0].label == 'usd'

    def test_result_serialisation_deterministic(self):
        task = make_task('tagging', 'some stable text')
        r1 = make_result(task, overall_confidence=4)
        r2 = make_result(task, overall_confidence=4)
        # Content sections must match (extracted_at may differ by seconds)
        d1, d2 = r1.to_dict(), r2.to_dict()
        assert d1['task_id'] == d2['task_id']
        assert d1['labels'] == d2['labels']
        assert d1['entities'] == d2['entities']


# ---------------------------------------------------------------------------
# result_to_candidate
# ---------------------------------------------------------------------------

class TestResultToCandidate:
    def _make_pair(self, task_type='claim_extraction', input_text='The Fed held rates.',
                   source_id=None):
        task = make_task(task_type, input_text, source_id=source_id)
        prov = SemanticProvenance(
            extraction_method='rule_based',
            source_id=source_id,
        )
        result = make_result(task, overall_confidence=3, provenance=prov)
        return task, result

    def test_returns_candidate(self):
        from ingestion.models import CandidateMemoryEvent
        task, result = self._make_pair()
        candidate = result_to_candidate(
            result, task,
            event_type='regime_observation',
            title='Fed holds rates',
        )
        assert isinstance(candidate, CandidateMemoryEvent)

    def test_candidate_status_is_proposed(self):
        task, result = self._make_pair()
        candidate = result_to_candidate(
            result, task,
            event_type='hypothesis',
            title='Some hypothesis',
        )
        assert candidate.status == 'proposed'

    def test_candidate_committed_id_is_none(self):
        task, result = self._make_pair()
        candidate = result_to_candidate(
            result, task,
            event_type='hypothesis',
            title='Some hypothesis',
        )
        assert candidate.committed_id is None

    def test_candidate_confidence_matches_result(self):
        task = make_task('tagging', 'text')
        result = make_result(task, overall_confidence=4)
        candidate = result_to_candidate(
            result, task,
            event_type='hypothesis',
            title='Test',
        )
        assert candidate.confidence == 4

    def test_labels_become_tags(self):
        task = make_task('tagging', 'EUR/USD rallied')
        lb1 = SemanticLabel(label='eur', confidence=4)
        lb2 = SemanticLabel(label='usd', confidence=3)
        result = make_result(task, overall_confidence=3, labels=[lb1, lb2])
        candidate = result_to_candidate(
            result, task,
            event_type='regime_observation',
            title='Currency move',
        )
        assert 'eur' in candidate.tags
        assert 'usd' in candidate.tags

    def test_extra_tags_appended(self):
        task, result = self._make_pair()
        candidate = result_to_candidate(
            result, task,
            event_type='regime_observation',
            title='Title',
            extra_tags=['manual', 'fx'],
        )
        assert 'manual' in candidate.tags
        assert 'fx' in candidate.tags

    def test_summary_uses_result_summary(self):
        task = make_task('summary_extraction', 'A longer piece of text here.')
        result = make_result(task, overall_confidence=3, summary='Custom summary.')
        candidate = result_to_candidate(
            result, task,
            event_type='implementation_note',
            title='Note',
        )
        assert candidate.summary == 'Custom summary.'

    def test_summary_falls_back_to_first_claim(self):
        task = make_task('claim_extraction', 'Rates held steady by the Fed.')
        claim = ExtractedClaim(text='Rates held steady.', polarity='neutral', confidence=3)
        result = make_result(task, overall_confidence=3, claims=[claim])
        candidate = result_to_candidate(
            result, task,
            event_type='regime_observation',
            title='Rate observation',
        )
        assert 'Rates held steady' in candidate.summary

    def test_evidence_from_entities_and_relations(self):
        task = make_task('relation_extraction', 'The Fed held US rates steady.')
        entity = ExtractedEntity(text='Fed', entity_type='org', confidence=4)
        relation = ExtractedRelation(
            subject='Fed', predicate='holds', object_='rates', confidence=3
        )
        result = make_result(task, overall_confidence=3, entities=[entity], relations=[relation])
        candidate = result_to_candidate(
            result, task,
            event_type='governance_rule',
            title='Monetary policy',
        )
        assert 'Fed' in candidate.evidence
        assert 'holds' in candidate.evidence

    def test_source_span_preserved(self):
        task = make_task('entity_extraction', 'The Federal Reserve held rates.')
        span = SemanticSpan(start=4, end=19)
        prov = SemanticProvenance(extraction_method='rule_based', source_span=span)
        result = make_result(task, overall_confidence=3, provenance=prov)
        candidate = result_to_candidate(
            result, task,
            event_type='hypothesis',
            title='Entity found',
        )
        assert candidate.source_span.start == 4
        assert candidate.source_span.end == 19

    def test_no_direct_memory_write(self):
        """Calling result_to_candidate must not touch any database."""
        import sqlite3
        import tempfile, os
        task, result = self._make_pair()
        db = tempfile.mktemp(suffix='.db')
        try:
            candidate = result_to_candidate(
                result, task,
                event_type='hypothesis',
                title='Test',
            )
            # DB file must not exist — no write was made
            assert not os.path.exists(db)
            assert candidate.committed_id is None
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_invalid_event_type_raises(self):
        task, result = self._make_pair()
        with pytest.raises(Exception):  # ingestion.models.ValidationError
            result_to_candidate(
                result, task,
                event_type='rate_prediction',  # not an EXTRACTABLE_EVENT_TYPE
                title='Bad type',
            )

    def test_invalid_result_raises(self):
        task = make_task('tagging', 'text')
        result = make_result(task, overall_confidence=3)
        result.task_id = 'wrong'  # break task/result linkage
        with pytest.raises(SemanticValidationError, match='task_id'):
            result_to_candidate(
                result, task,
                event_type='hypothesis',
                title='Title',
            )

    def test_created_by_default(self):
        task, result = self._make_pair()
        candidate = result_to_candidate(
            result, task,
            event_type='hypothesis',
            title='Title',
        )
        assert candidate.created_by == 'semantic-extractor'

    def test_created_by_custom(self):
        task, result = self._make_pair()
        candidate = result_to_candidate(
            result, task,
            event_type='hypothesis',
            title='Title',
            created_by='test-suite',
        )
        assert candidate.created_by == 'test-suite'

    def test_extraction_method_propagated(self):
        task = make_task('tagging', 'text')
        result = make_result(task, overall_confidence=3, extraction_method='pattern')
        candidate = result_to_candidate(
            result, task,
            event_type='hypothesis',
            title='Title',
        )
        assert candidate.extraction_method == 'pattern'

    def test_no_memory_mutation(self):
        """A second call with the same result does not change the first candidate."""
        task, result = self._make_pair()
        c1 = result_to_candidate(result, task, event_type='hypothesis', title='T1')
        c2 = result_to_candidate(result, task, event_type='hypothesis', title='T2')
        assert c1.committed_id is None
        assert c2.committed_id is None
        assert c1.title == 'T1'
        assert c2.title == 'T2'
