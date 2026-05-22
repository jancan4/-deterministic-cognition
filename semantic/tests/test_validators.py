"""Tests for semantic/validators.py."""
import pytest

from semantic.models import (
    ExtractedClaim,
    ExtractedEntity,
    ExtractedRelation,
    SemanticExtractionResult,
    SemanticLabel,
    SemanticProvenance,
    SemanticSpan,
    SemanticTask,
    derive_task_id,
)
from semantic.validators import (
    SemanticValidationError,
    validate_claim,
    validate_confidence,
    validate_entity,
    validate_label,
    validate_nonempty_string,
    validate_provenance,
    validate_relation,
    validate_result,
    validate_span,
    validate_task,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_type='tagging', input_text='some input text', source_id=None):
    return SemanticTask(
        task_id=derive_task_id(task_type, input_text, source_id or ''),
        task_type=task_type,
        input_text=input_text,
        source_id=source_id,
    )


def _make_result(task, overall_confidence=3, extraction_method='rule_based',
                 provenance=None, **kwargs):
    if provenance is None:
        provenance = SemanticProvenance(
            extraction_method=extraction_method,
            source_id=task.source_id,
        )
    return SemanticExtractionResult(
        task_id=task.task_id,
        task_type=task.task_type,
        extraction_method=extraction_method,
        provenance=provenance,
        overall_confidence=overall_confidence,
        extracted_at='2026-01-01T00:00:00Z',
        **kwargs,
    )


# ---------------------------------------------------------------------------
# validate_confidence
# ---------------------------------------------------------------------------

class TestValidateConfidence:
    def test_valid_min(self):
        validate_confidence(1)  # no exception

    def test_valid_max(self):
        validate_confidence(5)

    def test_valid_mid(self):
        validate_confidence(3)

    def test_zero_raises(self):
        with pytest.raises(SemanticValidationError, match='1–5'):
            validate_confidence(0)

    def test_six_raises(self):
        with pytest.raises(SemanticValidationError, match='1–5'):
            validate_confidence(6)

    def test_float_raises(self):
        with pytest.raises(SemanticValidationError, match='integer'):
            validate_confidence(3.0)

    def test_bool_raises(self):
        with pytest.raises(SemanticValidationError, match='integer'):
            validate_confidence(True)

    def test_none_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_confidence(None)

    def test_field_name_in_message(self):
        with pytest.raises(SemanticValidationError, match='label.confidence'):
            validate_confidence(0, 'label.confidence')


# ---------------------------------------------------------------------------
# validate_span
# ---------------------------------------------------------------------------

class TestValidateSpan:
    def test_valid_span(self):
        validate_span(SemanticSpan(0, 5), 10)

    def test_span_at_text_end(self):
        validate_span(SemanticSpan(5, 10), 10)

    def test_start_equals_end_raises(self):
        with pytest.raises(SemanticValidationError, match='start < end'):
            validate_span(SemanticSpan(5, 5), 20)

    def test_start_greater_than_end_raises(self):
        with pytest.raises(SemanticValidationError, match='start < end'):
            validate_span(SemanticSpan(8, 3), 20)

    def test_negative_start_raises(self):
        with pytest.raises(SemanticValidationError, match='>= 0'):
            validate_span(SemanticSpan(-1, 5), 20)

    def test_end_exceeds_text_raises(self):
        with pytest.raises(SemanticValidationError, match='exceeds text length'):
            validate_span(SemanticSpan(0, 100), 10)

    def test_end_equal_text_len_ok(self):
        validate_span(SemanticSpan(0, 10), 10)  # no exception

    def test_not_a_span_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_span((0, 5), 10)


# ---------------------------------------------------------------------------
# validate_nonempty_string
# ---------------------------------------------------------------------------

class TestValidateNonemptyString:
    def test_valid(self):
        validate_nonempty_string('hello', 'field')

    def test_empty_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_nonempty_string('', 'field')

    def test_whitespace_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_nonempty_string('   ', 'field')

    def test_none_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_nonempty_string(None, 'field')


# ---------------------------------------------------------------------------
# validate_provenance
# ---------------------------------------------------------------------------

class TestValidateProvenance:
    def test_minimal_valid(self):
        p = SemanticProvenance(extraction_method='rule_based')
        validate_provenance(p)  # no exception

    def test_empty_method_raises(self):
        p = SemanticProvenance(extraction_method='')
        with pytest.raises(SemanticValidationError, match='extraction_method'):
            validate_provenance(p)

    def test_source_bound_without_source_id_raises(self):
        p = SemanticProvenance(extraction_method='rule_based')
        with pytest.raises(SemanticValidationError, match='source_id'):
            validate_provenance(p, source_bound=True)

    def test_source_bound_with_source_id_ok(self):
        p = SemanticProvenance(extraction_method='rule_based', source_id='abc')
        validate_provenance(p, source_bound=True)  # no exception

    def test_span_range_checked(self):
        span = SemanticSpan(start=0, end=100)
        p = SemanticProvenance(extraction_method='rule_based', source_span=span)
        with pytest.raises(SemanticValidationError, match='exceeds text length'):
            validate_provenance(p, input_text_len=10)

    def test_not_a_provenance_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_provenance({'extraction_method': 'rule_based'})


# ---------------------------------------------------------------------------
# validate_label
# ---------------------------------------------------------------------------

class TestValidateLabel:
    def test_valid(self):
        validate_label(SemanticLabel(label='usd', confidence=3))

    def test_empty_label_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_label(SemanticLabel(label='', confidence=3))

    def test_bad_confidence_raises(self):
        with pytest.raises(SemanticValidationError, match='1–5'):
            validate_label(SemanticLabel(label='usd', confidence=0))


# ---------------------------------------------------------------------------
# validate_entity
# ---------------------------------------------------------------------------

class TestValidateEntity:
    def test_valid(self):
        validate_entity(ExtractedEntity(text='Fed', entity_type='org', confidence=4))

    def test_empty_text_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_entity(ExtractedEntity(text='', entity_type='org', confidence=3))

    def test_empty_entity_type_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_entity(ExtractedEntity(text='Fed', entity_type='', confidence=3))

    def test_span_out_of_range_raises(self):
        span = SemanticSpan(0, 200)
        with pytest.raises(SemanticValidationError):
            validate_entity(
                ExtractedEntity(text='Fed', entity_type='org', confidence=3, span=span),
                input_text_len=50,
            )


# ---------------------------------------------------------------------------
# validate_claim
# ---------------------------------------------------------------------------

class TestValidateClaim:
    def test_valid_polarities(self):
        for polarity in ('positive', 'negative', 'neutral', 'uncertain'):
            validate_claim(ExtractedClaim(text='claim', polarity=polarity, confidence=3))

    def test_invalid_polarity_raises(self):
        with pytest.raises(SemanticValidationError, match='polarity'):
            validate_claim(ExtractedClaim(text='x', polarity='bullish', confidence=3))

    def test_empty_text_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_claim(ExtractedClaim(text='', polarity='neutral', confidence=3))


# ---------------------------------------------------------------------------
# validate_relation
# ---------------------------------------------------------------------------

class TestValidateRelation:
    def test_valid(self):
        validate_relation(
            ExtractedRelation(subject='Fed', predicate='holds', object_='rates', confidence=3)
        )

    def test_empty_subject_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_relation(
                ExtractedRelation(subject='', predicate='holds', object_='rates', confidence=3)
            )

    def test_empty_predicate_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_relation(
                ExtractedRelation(subject='Fed', predicate='', object_='rates', confidence=3)
            )

    def test_empty_object_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_relation(
                ExtractedRelation(subject='Fed', predicate='holds', object_='', confidence=3)
            )


# ---------------------------------------------------------------------------
# validate_task
# ---------------------------------------------------------------------------

class TestValidateTask:
    def test_valid_task(self):
        validate_task(_make_task())

    def test_all_task_types_accepted(self):
        from semantic.models import SEMANTIC_TASK_TYPES
        for tt in SEMANTIC_TASK_TYPES:
            validate_task(_make_task(task_type=tt))

    def test_invalid_task_type_raises(self):
        t = _make_task()
        t.task_type = 'rate_prediction'
        with pytest.raises(SemanticValidationError, match='task_type'):
            validate_task(t)

    def test_empty_input_raises(self):
        t = _make_task(input_text='   ')
        with pytest.raises(SemanticValidationError, match='input_text'):
            validate_task(t)

    def test_not_a_task_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_task({'task_type': 'tagging'})

    def test_source_span_range_checked(self):
        t = _make_task(input_text='short')
        t.source_span = SemanticSpan(start=0, end=100)
        with pytest.raises(SemanticValidationError, match='exceeds text length'):
            validate_task(t)


# ---------------------------------------------------------------------------
# validate_result
# ---------------------------------------------------------------------------

class TestValidateResult:
    def test_valid_result_without_task(self):
        task = _make_task()
        result = _make_result(task)
        validate_result(result)  # no exception

    def test_valid_result_with_task(self):
        task = _make_task()
        result = _make_result(task)
        validate_result(result, task)

    def test_task_id_mismatch_raises(self):
        task = _make_task()
        result = _make_result(task)
        result.task_id = 'wrong_id'
        with pytest.raises(SemanticValidationError, match='task_id'):
            validate_result(result, task)

    def test_task_type_mismatch_raises(self):
        task = _make_task(task_type='tagging')
        result = _make_result(task)
        result.task_type = 'entity_extraction'
        with pytest.raises(SemanticValidationError, match='task_type'):
            validate_result(result, task)

    def test_bad_confidence_raises(self):
        task = _make_task()
        result = _make_result(task, overall_confidence=0)
        with pytest.raises(SemanticValidationError, match='1–5'):
            validate_result(result, task)

    def test_source_bound_missing_source_id_raises(self):
        task = _make_task(source_id='src1')
        prov = SemanticProvenance(extraction_method='rule_based')  # no source_id
        result = _make_result(task, provenance=prov)
        with pytest.raises(SemanticValidationError, match='source_id'):
            validate_result(result, task)

    def test_source_bound_with_source_id_ok(self):
        task = _make_task(source_id='src1')
        prov = SemanticProvenance(extraction_method='rule_based', source_id='src1')
        result = _make_result(task, provenance=prov)
        validate_result(result, task)  # no exception

    def test_not_a_result_raises(self):
        with pytest.raises(SemanticValidationError):
            validate_result({'task_id': 'x'})

    def test_invalid_task_type_in_result_raises(self):
        task = _make_task()
        result = _make_result(task)
        result.task_type = 'bad_type'
        with pytest.raises(SemanticValidationError, match='task_type'):
            validate_result(result)

    def test_empty_extraction_method_raises(self):
        task = _make_task()
        result = _make_result(task)
        result.extraction_method = ''
        with pytest.raises(SemanticValidationError, match='extraction_method'):
            validate_result(result)

    def test_invalid_label_inside_result_raises(self):
        task = _make_task()
        result = _make_result(task, labels=[SemanticLabel(label='', confidence=3)])
        with pytest.raises(SemanticValidationError):
            validate_result(result, task)

    def test_invalid_claim_polarity_raises(self):
        task = _make_task()
        result = _make_result(task, claims=[
            ExtractedClaim(text='x', polarity='bullish', confidence=3)
        ])
        with pytest.raises(SemanticValidationError, match='polarity'):
            validate_result(result, task)

    def test_span_range_checked_against_input(self):
        task = _make_task(input_text='short')  # len=5
        bad_span = SemanticSpan(start=0, end=100)
        result = _make_result(task, entities=[
            ExtractedEntity(text='x', entity_type='misc', confidence=3, span=bad_span)
        ])
        with pytest.raises(SemanticValidationError, match='exceeds text length'):
            validate_result(result, task)
