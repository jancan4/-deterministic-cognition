"""Tests for semantic/models.py."""
import json

import pytest

from semantic.models import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    SEMANTIC_TASK_TYPES,
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


# ---------------------------------------------------------------------------
# SemanticSpan
# ---------------------------------------------------------------------------

class TestSemanticSpan:
    def test_basic_creation(self):
        span = SemanticSpan(start=0, end=10)
        assert span.start == 0
        assert span.end == 10

    def test_slice_text(self):
        span = SemanticSpan(start=6, end=11)
        assert span.slice_text("hello world") == "world"
        assert span.slice_text("hello world") == "hello world"[6:11]

    def test_to_dict(self):
        span = SemanticSpan(start=3, end=7)
        d = span.to_dict()
        assert d == {'start': 3, 'end': 7}

    def test_from_dict_roundtrip(self):
        d = {'start': 5, 'end': 15}
        span = SemanticSpan.from_dict(d)
        assert span.to_dict() == d

    def test_non_int_start_raises(self):
        with pytest.raises(TypeError):
            SemanticSpan(start=0.5, end=10)

    def test_bool_start_raises(self):
        with pytest.raises(TypeError):
            SemanticSpan(start=True, end=10)


# ---------------------------------------------------------------------------
# SemanticProvenance
# ---------------------------------------------------------------------------

class TestSemanticProvenance:
    def test_minimal(self):
        p = SemanticProvenance(extraction_method='rule_based')
        assert p.source_id is None
        assert p.source_span is None
        assert p.model_id is None

    def test_with_all_fields(self):
        span = SemanticSpan(start=0, end=5)
        p = SemanticProvenance(
            extraction_method='local_model:phi3',
            source_id='abc123',
            source_span=span,
            model_id='phi3-mini',
        )
        assert p.source_id == 'abc123'
        assert p.model_id == 'phi3-mini'

    def test_to_dict_includes_all_keys(self):
        p = SemanticProvenance(extraction_method='keyword')
        d = p.to_dict()
        assert set(d.keys()) == {'extraction_method', 'source_id', 'source_span', 'model_id'}

    def test_to_dict_span_is_dict(self):
        span = SemanticSpan(start=1, end=4)
        p = SemanticProvenance(extraction_method='pattern', source_span=span)
        assert p.to_dict()['source_span'] == {'start': 1, 'end': 4}

    def test_from_dict_roundtrip(self):
        span = SemanticSpan(start=2, end=8)
        p = SemanticProvenance(
            extraction_method='heuristic',
            source_id='sid1',
            source_span=span,
        )
        p2 = SemanticProvenance.from_dict(p.to_dict())
        assert p2.extraction_method == 'heuristic'
        assert p2.source_id == 'sid1'
        assert p2.source_span.start == 2


# ---------------------------------------------------------------------------
# SemanticTask
# ---------------------------------------------------------------------------

class TestSemanticTask:
    def _make(self, **kwargs):
        defaults = dict(
            task_id='abc123',
            task_type='tagging',
            input_text='Some text to tag.',
        )
        defaults.update(kwargs)
        return SemanticTask(**defaults)

    def test_basic(self):
        t = self._make()
        assert t.task_type == 'tagging'
        assert t.is_source_bound is False

    def test_source_bound_true(self):
        t = self._make(source_id='src1')
        assert t.is_source_bound is True

    def test_to_dict_keys(self):
        t = self._make()
        d = t.to_dict()
        assert set(d.keys()) == {
            'task_id', 'task_type', 'input_text', 'source_id',
            'source_span', 'metadata', 'created_at',
        }

    def test_to_dict_metadata_default(self):
        t = self._make()
        assert t.to_dict()['metadata'] == {}

    def test_source_span_serialised(self):
        span = SemanticSpan(start=0, end=4)
        t = self._make(source_span=span)
        assert t.to_dict()['source_span'] == {'start': 0, 'end': 4}


# ---------------------------------------------------------------------------
# derive_task_id
# ---------------------------------------------------------------------------

class TestDeriveTaskId:
    def test_deterministic(self):
        a = derive_task_id('tagging', 'hello world', 'src1')
        b = derive_task_id('tagging', 'hello world', 'src1')
        assert a == b

    def test_length_16(self):
        assert len(derive_task_id('tagging', 'x')) == 16

    def test_different_type_different_id(self):
        a = derive_task_id('tagging', 'hello', 'src')
        b = derive_task_id('entity_extraction', 'hello', 'src')
        assert a != b

    def test_different_text_different_id(self):
        a = derive_task_id('tagging', 'hello', 'src')
        b = derive_task_id('tagging', 'world', 'src')
        assert a != b

    def test_different_source_different_id(self):
        a = derive_task_id('tagging', 'hello', 'src1')
        b = derive_task_id('tagging', 'hello', 'src2')
        assert a != b

    def test_no_source_id_stable(self):
        a = derive_task_id('tagging', 'text')
        b = derive_task_id('tagging', 'text', '')
        assert a == b


# ---------------------------------------------------------------------------
# SemanticLabel
# ---------------------------------------------------------------------------

class TestSemanticLabel:
    def test_basic(self):
        lb = SemanticLabel(label='usd', confidence=4)
        assert lb.label == 'usd'
        assert lb.rationale is None

    def test_to_dict(self):
        lb = SemanticLabel(label='fed', confidence=3, rationale='Mentioned in body')
        d = lb.to_dict()
        assert d['label'] == 'fed'
        assert d['confidence'] == 3
        assert d['rationale'] == 'Mentioned in body'

    def test_from_dict_roundtrip(self):
        lb = SemanticLabel(label='eur', confidence=2)
        lb2 = SemanticLabel.from_dict(lb.to_dict())
        assert lb2.label == 'eur'
        assert lb2.confidence == 2


# ---------------------------------------------------------------------------
# ExtractedEntity
# ---------------------------------------------------------------------------

class TestExtractedEntity:
    def test_basic(self):
        e = ExtractedEntity(text='Federal Reserve', entity_type='org', confidence=4)
        assert e.text == 'Federal Reserve'
        assert e.span is None

    def test_with_span(self):
        span = SemanticSpan(start=0, end=15)
        e = ExtractedEntity(text='Federal Reserve', entity_type='org', confidence=4, span=span)
        assert e.span.start == 0

    def test_to_dict_span_present(self):
        span = SemanticSpan(start=1, end=6)
        e = ExtractedEntity(text='hello', entity_type='misc', confidence=3, span=span)
        d = e.to_dict()
        assert d['span'] == {'start': 1, 'end': 6}

    def test_from_dict_roundtrip(self):
        span = SemanticSpan(start=0, end=3)
        e = ExtractedEntity(text='USD', entity_type='currency', confidence=5, span=span)
        e2 = ExtractedEntity.from_dict(e.to_dict())
        assert e2.text == 'USD'
        assert e2.span.end == 3


# ---------------------------------------------------------------------------
# ExtractedClaim
# ---------------------------------------------------------------------------

class TestExtractedClaim:
    def test_basic(self):
        c = ExtractedClaim(text='Rates held steady', polarity='neutral', confidence=3)
        assert c.polarity == 'neutral'

    def test_to_dict(self):
        c = ExtractedClaim(text='Risk rising', polarity='negative', confidence=2)
        d = c.to_dict()
        assert d['polarity'] == 'negative'
        assert d['span'] is None

    def test_from_dict_roundtrip(self):
        c = ExtractedClaim(text='Growth positive', polarity='positive', confidence=4)
        c2 = ExtractedClaim.from_dict(c.to_dict())
        assert c2.polarity == 'positive'
        assert c2.confidence == 4


# ---------------------------------------------------------------------------
# ExtractedRelation
# ---------------------------------------------------------------------------

class TestExtractedRelation:
    def test_basic(self):
        r = ExtractedRelation(
            subject='Fed', predicate='holds', object_='rates', confidence=3
        )
        assert r.subject == 'Fed'
        assert r.object_ == 'rates'

    def test_to_dict_uses_object_key(self):
        r = ExtractedRelation(
            subject='ECB', predicate='raises', object_='rates', confidence=4
        )
        d = r.to_dict()
        assert 'object' in d
        assert d['object'] == 'rates'

    def test_from_dict_roundtrip(self):
        r = ExtractedRelation(
            subject='BoJ', predicate='holds', object_='policy', confidence=2
        )
        r2 = ExtractedRelation.from_dict(r.to_dict())
        assert r2.subject == 'BoJ'
        assert r2.object_ == 'policy'


# ---------------------------------------------------------------------------
# SemanticExtractionResult
# ---------------------------------------------------------------------------

class TestSemanticExtractionResult:
    def _make_result(self, **kwargs):
        prov = SemanticProvenance(extraction_method='rule_based')
        defaults = dict(
            task_id='t1',
            task_type='tagging',
            extraction_method='rule_based',
            provenance=prov,
            overall_confidence=3,
            extracted_at='2026-01-01T00:00:00Z',
        )
        defaults.update(kwargs)
        return SemanticExtractionResult(**defaults)

    def test_basic(self):
        r = self._make_result()
        assert r.labels == []
        assert r.entities == []
        assert r.claims == []
        assert r.relations == []
        assert r.summary is None

    def test_to_dict_keys(self):
        r = self._make_result()
        keys = set(r.to_dict().keys())
        assert keys == {
            'task_id', 'task_type', 'extraction_method', 'provenance',
            'overall_confidence', 'extracted_at',
            'labels', 'entities', 'claims', 'relations', 'summary', 'metadata',
        }

    def test_to_json_is_valid_json(self):
        r = self._make_result()
        parsed = json.loads(r.to_json())
        assert parsed['task_id'] == 't1'

    def test_to_json_is_deterministic(self):
        r = self._make_result()
        assert r.to_json() == r.to_json()

    def test_to_json_with_labels(self):
        lb = SemanticLabel(label='usd', confidence=3)
        r = self._make_result(labels=[lb])
        d = json.loads(r.to_json())
        assert d['labels'][0]['label'] == 'usd'

    def test_repeated_serialisation_identical(self):
        lb = SemanticLabel(label='fed', confidence=4)
        entity = ExtractedEntity(text='Fed', entity_type='org', confidence=4)
        r = self._make_result(labels=[lb], entities=[entity])
        assert r.to_json() == r.to_json()
        assert r.to_json() == r.to_json()
