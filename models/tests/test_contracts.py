"""Tests for models/contracts.py."""
import json

import pytest

from semantic.contracts import make_task
from semantic.models import SemanticLabel, ExtractedClaim
from models.contracts import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    ModelContractError,
    ModelExecutionPolicy,
    ModelExecutionResult,
    LocalModelRequest,
    LocalModelResponse,
    derive_request_id,
    response_to_semantic_result,
    task_to_request,
    validate_request,
    validate_response,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(**kwargs):
    defaults = dict(
        request_id='req0001',
        model_name='test-model',
        model_version='1.0',
        task_type='tagging',
        input_text='The Federal Reserve held rates.',
        extraction_method='local_model:test-model:1.0',
    )
    defaults.update(kwargs)
    return LocalModelRequest(**defaults)


def _make_response(request=None, **kwargs):
    req = request or _make_request()
    defaults = dict(
        request_id=req.request_id,
        model_name=req.model_name,
        model_version=req.model_version,
        task_type=req.task_type,
        extraction_method=req.extraction_method,
        overall_confidence=3,
        responded_at='2026-01-01T00:00:00Z',
    )
    defaults.update(kwargs)
    return LocalModelResponse(**defaults)


# ---------------------------------------------------------------------------
# derive_request_id
# ---------------------------------------------------------------------------

class TestDeriveRequestId:
    def test_deterministic(self):
        a = derive_request_id('model', '1.0', 'tagging', 'hello')
        b = derive_request_id('model', '1.0', 'tagging', 'hello')
        assert a == b

    def test_length_16(self):
        assert len(derive_request_id('m', '1', 'tagging', 'x')) == 16

    def test_different_model_different_id(self):
        a = derive_request_id('model-a', '1.0', 'tagging', 'hello')
        b = derive_request_id('model-b', '1.0', 'tagging', 'hello')
        assert a != b

    def test_different_version_different_id(self):
        a = derive_request_id('m', '1.0', 'tagging', 'hello')
        b = derive_request_id('m', '2.0', 'tagging', 'hello')
        assert a != b

    def test_different_task_different_id(self):
        a = derive_request_id('m', '1.0', 'tagging', 'hello')
        b = derive_request_id('m', '1.0', 'entity_extraction', 'hello')
        assert a != b

    def test_different_text_different_id(self):
        a = derive_request_id('m', '1.0', 'tagging', 'hello')
        b = derive_request_id('m', '1.0', 'tagging', 'world')
        assert a != b


# ---------------------------------------------------------------------------
# LocalModelRequest
# ---------------------------------------------------------------------------

class TestLocalModelRequest:
    def test_basic(self):
        req = _make_request()
        assert req.model_name == 'test-model'
        assert req.task_type == 'tagging'

    def test_to_dict_keys(self):
        req = _make_request()
        d = req.to_dict()
        assert set(d.keys()) == {
            'request_id', 'model_name', 'model_version', 'task_type',
            'input_text', 'extraction_method', 'source_id', 'source_span',
            'metadata', 'requested_at',
        }

    def test_to_json_valid(self):
        req = _make_request()
        parsed = json.loads(req.to_json())
        assert parsed['model_name'] == 'test-model'

    def test_to_json_deterministic_content(self):
        req = _make_request()
        d1 = json.loads(req.to_json())
        d2 = json.loads(req.to_json())
        assert d1['request_id'] == d2['request_id']
        assert d1['input_text'] == d2['input_text']

    def test_source_id_optional(self):
        req = _make_request(source_id='src1')
        assert req.to_dict()['source_id'] == 'src1'

    def test_metadata_stored(self):
        req = _make_request(metadata={'key': 'val'})
        assert req.to_dict()['metadata'] == {'key': 'val'}


# ---------------------------------------------------------------------------
# validate_request
# ---------------------------------------------------------------------------

class TestValidateRequest:
    def test_valid(self):
        validate_request(_make_request())  # no exception

    def test_empty_model_name_raises(self):
        with pytest.raises(ModelContractError, match='model_name'):
            validate_request(_make_request(model_name=''))

    def test_whitespace_model_name_raises(self):
        with pytest.raises(ModelContractError, match='model_name'):
            validate_request(_make_request(model_name='   '))

    def test_empty_model_version_raises(self):
        with pytest.raises(ModelContractError, match='model_version'):
            validate_request(_make_request(model_version=''))

    def test_invalid_task_type_raises(self):
        with pytest.raises(ModelContractError, match='task_type'):
            validate_request(_make_request(task_type='llm_query'))

    def test_empty_input_text_raises(self):
        with pytest.raises(ModelContractError, match='input_text'):
            validate_request(_make_request(input_text=''))

    def test_whitespace_input_raises(self):
        with pytest.raises(ModelContractError, match='input_text'):
            validate_request(_make_request(input_text='   '))

    def test_empty_extraction_method_raises(self):
        with pytest.raises(ModelContractError, match='extraction_method'):
            validate_request(_make_request(extraction_method=''))

    def test_empty_request_id_raises(self):
        with pytest.raises(ModelContractError, match='request_id'):
            validate_request(_make_request(request_id=''))

    def test_not_a_request_raises(self):
        with pytest.raises(ModelContractError):
            validate_request({'model_name': 'x'})


# ---------------------------------------------------------------------------
# LocalModelResponse
# ---------------------------------------------------------------------------

class TestLocalModelResponse:
    def test_basic(self):
        resp = _make_response()
        assert resp.overall_confidence == 3
        assert resp.labels == []
        assert resp.summary is None

    def test_to_dict_keys(self):
        resp = _make_response()
        d = resp.to_dict()
        assert set(d.keys()) == {
            'request_id', 'model_name', 'model_version', 'task_type',
            'extraction_method', 'overall_confidence', 'responded_at',
            'labels', 'entities', 'claims', 'relations', 'summary', 'metadata',
        }

    def test_to_json_valid(self):
        resp = _make_response()
        parsed = json.loads(resp.to_json())
        assert parsed['overall_confidence'] == 3

    def test_labels_serialised(self):
        lb = SemanticLabel(label='usd', confidence=3)
        resp = _make_response(labels=[lb])
        d = resp.to_dict()
        assert d['labels'][0]['label'] == 'usd'

    def test_to_json_deterministic(self):
        resp = _make_response()
        assert resp.to_json() == resp.to_json()


# ---------------------------------------------------------------------------
# validate_response
# ---------------------------------------------------------------------------

class TestValidateResponse:
    def test_valid(self):
        req = _make_request()
        resp = _make_response(req)
        validate_response(resp, req)  # no exception

    def test_invalid_task_type_raises(self):
        resp = _make_response(task_type='tagging')
        resp.task_type = 'bad_type'
        with pytest.raises(ModelContractError, match='task_type'):
            validate_response(resp)

    def test_empty_extraction_method_raises(self):
        resp = _make_response(extraction_method='x')
        resp.extraction_method = ''
        with pytest.raises(ModelContractError, match='extraction_method'):
            validate_response(resp)

    def test_bad_confidence_zero_raises(self):
        with pytest.raises(ModelContractError, match='overall_confidence'):
            validate_response(_make_response(overall_confidence=0))

    def test_bad_confidence_six_raises(self):
        with pytest.raises(ModelContractError, match='overall_confidence'):
            validate_response(_make_response(overall_confidence=6))

    def test_bool_confidence_raises(self):
        with pytest.raises(ModelContractError, match='overall_confidence'):
            validate_response(_make_response(overall_confidence=True))

    def test_request_id_mismatch_raises(self):
        req = _make_request()
        resp = _make_response(req, request_id='wrong')
        with pytest.raises(ModelContractError, match='request_id'):
            validate_response(resp, req)

    def test_task_type_mismatch_raises(self):
        req = _make_request(task_type='tagging')
        resp = _make_response(req, task_type='entity_extraction')
        with pytest.raises(ModelContractError, match='task_type'):
            validate_response(resp, req)

    def test_model_name_mismatch_raises(self):
        req = _make_request(model_name='model-a')
        resp = _make_response(req, model_name='model-b')
        with pytest.raises(ModelContractError, match='model_name'):
            validate_response(resp, req)

    def test_model_version_mismatch_raises(self):
        req = _make_request(model_version='1.0')
        resp = _make_response(req, model_version='2.0')
        with pytest.raises(ModelContractError, match='model_version'):
            validate_response(resp, req)

    def test_not_a_response_raises(self):
        with pytest.raises(ModelContractError):
            validate_response({'request_id': 'x'})


# ---------------------------------------------------------------------------
# ModelExecutionPolicy
# ---------------------------------------------------------------------------

class TestModelExecutionPolicy:
    def test_defaults(self):
        p = ModelExecutionPolicy()
        assert p.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
        assert p.max_retries == 0
        assert p.deterministic_mode is True

    def test_custom_values(self):
        p = ModelExecutionPolicy(timeout_seconds=60.0, max_retries=2)
        assert p.timeout_seconds == 60.0
        assert p.max_retries == 2

    def test_zero_timeout_raises(self):
        with pytest.raises(ModelContractError, match='timeout_seconds'):
            ModelExecutionPolicy(timeout_seconds=0)

    def test_negative_timeout_raises(self):
        with pytest.raises(ModelContractError, match='timeout_seconds'):
            ModelExecutionPolicy(timeout_seconds=-1)

    def test_max_timeout_raises(self):
        with pytest.raises(ModelContractError, match='timeout_seconds'):
            ModelExecutionPolicy(timeout_seconds=MAX_TIMEOUT_SECONDS + 1)

    def test_max_timeout_exactly_ok(self):
        p = ModelExecutionPolicy(timeout_seconds=MAX_TIMEOUT_SECONDS)
        assert p.timeout_seconds == MAX_TIMEOUT_SECONDS

    def test_negative_max_retries_raises(self):
        with pytest.raises(ModelContractError, match='max_retries'):
            ModelExecutionPolicy(max_retries=-1)

    def test_bool_max_retries_raises(self):
        with pytest.raises(ModelContractError, match='max_retries'):
            ModelExecutionPolicy(max_retries=True)

    def test_negative_retry_delay_raises(self):
        with pytest.raises(ModelContractError, match='retry_delay'):
            ModelExecutionPolicy(retry_delay_seconds=-0.1)

    def test_to_dict(self):
        p = ModelExecutionPolicy()
        d = p.to_dict()
        assert set(d.keys()) == {
            'timeout_seconds', 'max_retries', 'retry_delay_seconds', 'deterministic_mode'
        }


# ---------------------------------------------------------------------------
# task_to_request
# ---------------------------------------------------------------------------

class TestTaskToRequest:
    def test_basic(self):
        task = make_task('tagging', 'The Fed held rates.')
        req = task_to_request(task, 'phi3', '1.0')
        assert req.task_type == 'tagging'
        assert req.model_name == 'phi3'
        assert req.model_version == '1.0'

    def test_request_id_deterministic(self):
        task = make_task('tagging', 'stable text', source_id='src1')
        req1 = task_to_request(task, 'phi3', '1.0')
        req2 = task_to_request(task, 'phi3', '1.0')
        assert req1.request_id == req2.request_id

    def test_extraction_method_default(self):
        task = make_task('entity_extraction', 'text')
        req = task_to_request(task, 'phi3', '1.0')
        assert req.extraction_method == 'local_model:phi3:1.0'

    def test_extraction_method_custom(self):
        task = make_task('tagging', 'text')
        req = task_to_request(task, 'phi3', '1.0', extraction_method='rule_based')
        assert req.extraction_method == 'rule_based'

    def test_source_id_propagated(self):
        task = make_task('tagging', 'text', source_id='abc123')
        req = task_to_request(task, 'phi3', '1.0')
        assert req.source_id == 'abc123'

    def test_empty_model_name_raises(self):
        task = make_task('tagging', 'text')
        with pytest.raises(ModelContractError, match='model_name'):
            task_to_request(task, '', '1.0')

    def test_empty_model_version_raises(self):
        task = make_task('tagging', 'text')
        with pytest.raises(ModelContractError, match='model_version'):
            task_to_request(task, 'phi3', '')


# ---------------------------------------------------------------------------
# response_to_semantic_result
# ---------------------------------------------------------------------------

class TestResponseToSemanticResult:
    def test_basic(self):
        task = make_task('tagging', 'The Fed held rates.')
        req = task_to_request(task, 'phi3', '1.0')
        resp = _make_response(req, labels=[SemanticLabel(label='fed', confidence=3)])
        result = response_to_semantic_result(resp, task)
        assert result.task_id == task.task_id
        assert result.labels[0].label == 'fed'

    def test_provenance_extraction_method(self):
        task = make_task('tagging', 'text')
        req = task_to_request(task, 'phi3', '1.0')
        resp = _make_response(req)
        result = response_to_semantic_result(resp, task)
        assert result.provenance.extraction_method == req.extraction_method

    def test_provenance_model_id(self):
        task = make_task('tagging', 'text')
        req = task_to_request(task, 'phi3', '1.0')
        resp = _make_response(req)
        result = response_to_semantic_result(resp, task)
        assert result.provenance.model_id == 'phi3:1.0'

    def test_source_id_propagated(self):
        task = make_task('tagging', 'text', source_id='src99')
        req = task_to_request(task, 'phi3', '1.0')
        resp = _make_response(req)
        result = response_to_semantic_result(resp, task)
        assert result.provenance.source_id == 'src99'

    def test_confidence_preserved(self):
        task = make_task('tagging', 'text')
        req = task_to_request(task, 'phi3', '1.0')
        resp = _make_response(req, overall_confidence=5)
        result = response_to_semantic_result(resp, task)
        assert result.overall_confidence == 5

    def test_result_validates_against_task(self):
        task = make_task('tagging', 'text', source_id='src')
        req = task_to_request(task, 'phi3', '1.0')
        resp = _make_response(req)
        # source-bound task requires provenance source_id — check it's automatically set
        result = response_to_semantic_result(resp, task)
        assert result.provenance.source_id == 'src'

    def test_summary_preserved(self):
        task = make_task('summary_extraction', 'A long text.')
        req = task_to_request(task, 'phi3', '1.0')
        resp = _make_response(req, summary='Short summary.')
        result = response_to_semantic_result(resp, task)
        assert result.summary == 'Short summary.'
