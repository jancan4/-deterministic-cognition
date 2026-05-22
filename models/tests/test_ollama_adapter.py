"""
Tests for models/ollama_adapter.py.

All tests use mocked HTTP — no live Ollama instance is required.
The 'requests' library is patched at the module level for each test class
to simulate Ollama API responses.
"""
import json
import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from models.contracts import (
    LocalModelRequest,
    LocalModelResponse,
    ModelContractError,
    derive_request_id,
)
from models.ollama_adapter import (
    OLLAMA_ADAPTER_NAME,
    OLLAMA_DEFAULT_BASE_URL,
    OLLAMA_MAX_INPUT_CHARS,
    OllamaAdapter,
    _PROMPT_TEMPLATES,
    _build_payload,
    _build_prompt,
    _clamp_confidence,
    _extract_json_from_text,
    _parse_response,
    _sha16,
    _template_hash,
)
from semantic.models import SEMANTIC_TASK_TYPES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ollama_response(
    response_text: str = '{"labels": [{"label": "fed", "confidence": 4, "rationale": "central bank"}], "summary": "test"}',
    model: str = 'phi3:mini',
    done: bool = True,
    eval_count: int = 42,
    eval_duration: int = 1000000,
    status_code: int = 200,
) -> MagicMock:
    """Build a mock requests.Response for Ollama /api/generate."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = {
        'model': model,
        'response': response_text,
        'done': done,
        'eval_count': eval_count,
        'eval_duration': eval_duration,
    }
    return mock_resp


def _make_adapter(model: str = 'phi3:mini', **kwargs) -> OllamaAdapter:
    """Instantiate OllamaAdapter with mocked init HTTP calls (no live Ollama)."""
    with patch('models.ollama_adapter._fetch_runtime_version', return_value='0.3.0'), \
         patch('models.ollama_adapter._fetch_model_info', return_value=('sha256:abc123', 'phi3')):
        return OllamaAdapter(model=model, **kwargs)


def _make_request(
    task_type: str = 'tagging',
    input_text: str = 'The Federal Reserve held rates steady.',
    model: str = 'phi3:mini',
    version: str = '1.0.0',
) -> LocalModelRequest:
    return LocalModelRequest(
        request_id=derive_request_id(model, version, task_type, input_text),
        model_name=model,
        model_version=version,
        task_type=task_type,
        input_text=input_text,
        extraction_method=f'local_model:ollama:{model}',
    )


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions, no HTTP)
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_sha16_produces_16_char_hex(self):
        result = _sha16('hello world')
        assert len(result) == 16
        assert all(c in '0123456789abcdef' for c in result)

    def test_sha16_deterministic(self):
        assert _sha16('test input') == _sha16('test input')

    def test_sha16_differs_on_different_input(self):
        assert _sha16('abc') != _sha16('def')

    def test_clamp_confidence_within_range(self):
        assert _clamp_confidence(3) == 3
        assert _clamp_confidence(1) == 1
        assert _clamp_confidence(5) == 5

    def test_clamp_confidence_clamps_below(self):
        assert _clamp_confidence(0) == 1
        assert _clamp_confidence(-5) == 1

    def test_clamp_confidence_clamps_above(self):
        assert _clamp_confidence(6) == 5
        assert _clamp_confidence(100) == 5

    def test_clamp_confidence_handles_non_int(self):
        assert _clamp_confidence('x') == 3
        assert _clamp_confidence(None) == 3

    def test_extract_json_exact(self):
        text = '{"labels": [], "summary": "test"}'
        result = _extract_json_from_text(text)
        assert result == {'labels': [], 'summary': 'test'}

    def test_extract_json_embedded_in_prose(self):
        text = 'Here is the result: {"labels": [{"label": "fed"}]} done.'
        result = _extract_json_from_text(text)
        assert result is not None
        assert 'labels' in result

    def test_extract_json_returns_none_on_garbage(self):
        assert _extract_json_from_text('no json here at all') is None

    def test_extract_json_returns_none_on_empty(self):
        assert _extract_json_from_text('') is None

    def test_prompt_templates_cover_all_task_types(self):
        for tt in SEMANTIC_TASK_TYPES:
            assert tt in _PROMPT_TEMPLATES, f"Missing template for {tt!r}"

    def test_build_prompt_interpolates_input(self):
        prompt = _build_prompt('tagging', 'The ECB raised rates.')
        assert 'The ECB raised rates.' in prompt

    def test_template_hash_is_16_char_hex(self):
        h = _template_hash('tagging')
        assert len(h) == 16

    def test_build_payload_contains_model(self):
        payload = _build_payload('phi3:mini', 'some prompt', 0.0, 42, 512)
        assert payload['model'] == 'phi3:mini'
        assert payload['options']['temperature'] == 0.0
        assert payload['options']['seed'] == 42
        assert payload['stream'] is False


# ---------------------------------------------------------------------------
# Adapter instantiation
# ---------------------------------------------------------------------------

class TestOllamaAdapterInstantiation:
    def test_instantiation_succeeds_with_mock(self):
        adapter = _make_adapter()
        assert adapter.adapter_name == OLLAMA_ADAPTER_NAME

    def test_adapter_version_propagated(self):
        adapter = _make_adapter(version='2.0.0')
        assert adapter.adapter_version == '2.0.0'

    def test_empty_model_raises(self):
        with patch('models.ollama_adapter._fetch_runtime_version', return_value=None), \
             patch('models.ollama_adapter._fetch_model_info', return_value=(None, None)):
            with pytest.raises(ModelContractError, match='model name'):
                OllamaAdapter(model='')

    def test_zero_timeout_raises(self):
        with patch('models.ollama_adapter._fetch_runtime_version', return_value=None), \
             patch('models.ollama_adapter._fetch_model_info', return_value=(None, None)):
            with pytest.raises(ModelContractError, match='timeout_seconds'):
                OllamaAdapter(model='phi3:mini', timeout_seconds=0)

    def test_missing_requests_raises(self):
        import models.ollama_adapter as mod
        original = mod._HAS_REQUESTS
        try:
            mod._HAS_REQUESTS = False
            with pytest.raises(ModelContractError, match='requests'):
                OllamaAdapter(model='phi3:mini')
        finally:
            mod._HAS_REQUESTS = original

    def test_runtime_version_stored_from_init(self):
        adapter = _make_adapter()
        assert adapter._runtime_version == '0.3.0'

    def test_model_digest_stored_from_init(self):
        adapter = _make_adapter()
        assert adapter._model_digest == 'sha256:abc123'

    def test_model_family_stored_from_init(self):
        adapter = _make_adapter()
        assert adapter._model_family == 'phi3'

    def test_runtime_version_none_on_failure(self):
        with patch('models.ollama_adapter._fetch_runtime_version', return_value=None), \
             patch('models.ollama_adapter._fetch_model_info', return_value=(None, None)):
            adapter = OllamaAdapter(model='phi3:mini')
        assert adapter._runtime_version is None

    def test_model_digest_none_on_failure(self):
        with patch('models.ollama_adapter._fetch_runtime_version', return_value=None), \
             patch('models.ollama_adapter._fetch_model_info', return_value=(None, None)):
            adapter = OllamaAdapter(model='phi3:mini')
        assert adapter._model_digest is None


# ---------------------------------------------------------------------------
# Capability set
# ---------------------------------------------------------------------------

class TestOllamaAdapterCapabilities:
    def test_supports_all_task_types(self):
        adapter = _make_adapter()
        for tt in SEMANTIC_TASK_TYPES:
            assert adapter.supports(tt), f"Expected support for {tt!r}"

    def test_deterministic_mode_supported_is_false(self):
        adapter = _make_adapter()
        for tt in SEMANTIC_TASK_TYPES:
            cap = adapter.get_capability(tt)
            assert cap is not None
            assert cap.deterministic_mode_supported is False

    def test_max_input_chars(self):
        adapter = _make_adapter()
        cap = adapter.get_capability('tagging')
        assert cap.max_input_chars == OLLAMA_MAX_INPUT_CHARS

    def test_provenance_supported(self):
        adapter = _make_adapter()
        cap = adapter.get_capability('tagging')
        assert cap.provenance_supported is True


# ---------------------------------------------------------------------------
# execute() — happy path
# ---------------------------------------------------------------------------

class TestOllamaAdapterExecute:
    def _execute(
        self,
        response_text: str = '{"labels": [{"label": "fed", "confidence": 4, "rationale": "central bank"}], "summary": "Fed held rates."}',
        task_type: str = 'tagging',
        status_code: int = 200,
    ) -> LocalModelResponse:
        adapter = _make_adapter()
        request = _make_request(task_type=task_type)
        mock_resp = _make_ollama_response(response_text=response_text, status_code=status_code)
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            return adapter.execute(request)

    def test_returns_local_model_response(self):
        resp = self._execute()
        assert isinstance(resp, LocalModelResponse)

    def test_request_id_preserved(self):
        adapter = _make_adapter()
        request = _make_request()
        mock_resp = _make_ollama_response()
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            resp = adapter.execute(request)
        assert resp.request_id == request.request_id

    def test_task_type_preserved(self):
        resp = self._execute(task_type='tagging')
        assert resp.task_type == 'tagging'

    def test_labels_extracted(self):
        resp = self._execute()
        assert len(resp.labels) == 1
        assert resp.labels[0].label == 'fed'
        assert resp.labels[0].confidence == 4

    def test_summary_extracted(self):
        resp = self._execute()
        assert resp.summary == 'Fed held rates.'

    def test_extraction_method_contains_model(self):
        resp = self._execute()
        assert 'ollama' in resp.extraction_method
        assert 'phi3:mini' in resp.extraction_method

    def test_entity_extraction_task(self):
        payload = '{"entities": [{"text": "Federal Reserve", "entity_type": "ORG", "confidence": 4}], "summary": "s"}'
        resp = self._execute(response_text=payload, task_type='entity_extraction')
        assert len(resp.entities) == 1
        assert resp.entities[0].text == 'Federal Reserve'
        assert resp.entities[0].entity_type == 'ORG'

    def test_claim_extraction_task(self):
        payload = '{"claims": [{"text": "rates held", "polarity": "neutral", "confidence": 3}], "summary": "s"}'
        resp = self._execute(response_text=payload, task_type='claim_extraction')
        assert len(resp.claims) == 1
        assert resp.claims[0].polarity == 'neutral'

    def test_relation_extraction_task(self):
        payload = '{"relations": [{"subject": "Fed", "predicate": "held", "object": "rates"}], "summary": "s"}'
        resp = self._execute(response_text=payload, task_type='relation_extraction')
        assert len(resp.relations) == 1
        assert resp.relations[0].subject == 'Fed'


# ---------------------------------------------------------------------------
# execute() — provenance metadata
# ---------------------------------------------------------------------------

class TestOllamaAdapterProvenance:
    def _get_metadata(self, task_type: str = 'tagging') -> dict:
        adapter = _make_adapter()
        request = _make_request(task_type=task_type)
        mock_resp = _make_ollama_response()
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            resp = adapter.execute(request)
        return resp.metadata

    def test_adapter_name_in_metadata(self):
        assert self._get_metadata()['adapter_name'] == OLLAMA_ADAPTER_NAME

    def test_provider_in_metadata(self):
        assert self._get_metadata()['provider'] == 'ollama'

    def test_runtime_version_in_metadata(self):
        assert self._get_metadata()['runtime_version'] == '0.3.0'

    def test_model_name_in_metadata(self):
        assert self._get_metadata()['model_name'] == 'phi3:mini'

    def test_model_digest_in_metadata(self):
        assert self._get_metadata()['model_digest'] == 'sha256:abc123'

    def test_temperature_in_metadata(self):
        assert self._get_metadata()['temperature'] == 0.0

    def test_seed_in_metadata(self):
        assert self._get_metadata()['seed'] == 42

    def test_prompt_template_hash_in_metadata(self):
        h = self._get_metadata()['prompt_template_hash']
        assert isinstance(h, str) and len(h) == 16

    def test_request_payload_hash_in_metadata(self):
        h = self._get_metadata()['request_payload_hash']
        assert isinstance(h, str) and len(h) == 16

    def test_input_hash_in_metadata(self):
        h = self._get_metadata()['input_hash']
        assert isinstance(h, str) and len(h) == 16

    def test_started_at_in_metadata(self):
        ts = self._get_metadata()['started_at']
        assert isinstance(ts, str) and 'T' in ts

    def test_ollama_eval_count_in_metadata(self):
        assert self._get_metadata()['ollama_eval_count'] == 42

    def test_parse_error_none_on_success(self):
        assert self._get_metadata()['parse_error'] is None

    def test_raw_output_in_metadata(self):
        meta = self._get_metadata()
        assert 'raw_output' in meta
        raw = meta['raw_output']
        assert 'raw' in raw
        assert isinstance(raw['raw'], str)

    def test_raw_output_model_field(self):
        raw = self._get_metadata()['raw_output']
        assert raw['model'] == 'phi3:mini'


# ---------------------------------------------------------------------------
# execute() — determinism of request_id
# ---------------------------------------------------------------------------

class TestRequestIdDeterminism:
    def test_same_input_same_request_id(self):
        adapter = _make_adapter()
        req1 = _make_request(input_text='stable text here')
        req2 = _make_request(input_text='stable text here')
        assert req1.request_id == req2.request_id

    def test_different_input_different_request_id(self):
        req1 = _make_request(input_text='text A')
        req2 = _make_request(input_text='text B')
        assert req1.request_id != req2.request_id

    def test_request_id_length_16(self):
        req = _make_request()
        assert len(req.request_id) == 16


# ---------------------------------------------------------------------------
# execute() — HTTP payload
# ---------------------------------------------------------------------------

class TestHttpPayload:
    def _capture_payload(self, temperature: float = 0.0, seed: int = 42) -> dict:
        with patch('models.ollama_adapter._fetch_runtime_version', return_value=None), \
             patch('models.ollama_adapter._fetch_model_info', return_value=(None, None)):
            adapter = OllamaAdapter(
                model='phi3:mini', temperature=temperature, seed=seed
            )
        request = _make_request()
        mock_resp = _make_ollama_response()
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured.update(json or {})
            return mock_resp

        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.side_effect = fake_post
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            adapter.execute(request)

        return captured

    def test_temperature_zero_in_payload(self):
        payload = self._capture_payload(temperature=0.0)
        assert payload['options']['temperature'] == 0.0

    def test_seed_in_payload(self):
        payload = self._capture_payload(seed=42)
        assert payload['options']['seed'] == 42

    def test_stream_false_in_payload(self):
        payload = self._capture_payload()
        assert payload['stream'] is False

    def test_model_in_payload(self):
        payload = self._capture_payload()
        assert payload['model'] == 'phi3:mini'

    def test_prompt_contains_input_text(self):
        payload = self._capture_payload()
        assert 'The Federal Reserve held rates steady.' in payload['prompt']


# ---------------------------------------------------------------------------
# execute() — error handling
# ---------------------------------------------------------------------------

class TestOllamaAdapterErrors:
    def _make_adapter_for_error(self):
        return _make_adapter()

    def test_timeout_raises_model_contract_error(self):
        import requests as real_requests
        adapter = self._make_adapter_for_error()
        request = _make_request()
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.side_effect = real_requests.exceptions.Timeout()
            mock_req.exceptions.Timeout = real_requests.exceptions.Timeout
            mock_req.exceptions.ConnectionError = real_requests.exceptions.ConnectionError
            with pytest.raises(ModelContractError, match='timed out'):
                adapter.execute(request)

    def test_connection_error_raises_model_contract_error(self):
        import requests as real_requests
        adapter = self._make_adapter_for_error()
        request = _make_request()
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.side_effect = real_requests.exceptions.ConnectionError('refused')
            mock_req.exceptions.Timeout = real_requests.exceptions.Timeout
            mock_req.exceptions.ConnectionError = real_requests.exceptions.ConnectionError
            with pytest.raises(ModelContractError, match='connection error'):
                adapter.execute(request)

    def test_http_404_raises_model_contract_error(self):
        adapter = self._make_adapter_for_error()
        request = _make_request()
        mock_resp = _make_ollama_response(status_code=404)
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            with pytest.raises(ModelContractError, match='HTTP 404'):
                adapter.execute(request)

    def test_invalid_json_response_no_raise(self):
        """Malformed JSON → parse_error set, minimal response returned, no exception."""
        adapter = self._make_adapter_for_error()
        request = _make_request()
        mock_resp = _make_ollama_response(response_text='this is not json at all')
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            resp = adapter.execute(request)
        assert resp.metadata['parse_error'] is not None
        assert isinstance(resp, LocalModelResponse)

    def test_partial_json_extract_succeeds(self):
        """JSON embedded in surrounding prose is still extracted."""
        adapter = self._make_adapter_for_error()
        request = _make_request()
        text = 'Sure! Here: {"labels": [{"label": "ecb", "confidence": 3}], "summary": "s"} Done.'
        mock_resp = _make_ollama_response(response_text=text)
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            resp = adapter.execute(request)
        assert resp.metadata['parse_error'] is None
        assert len(resp.labels) == 1
        assert resp.labels[0].label == 'ecb'


# ---------------------------------------------------------------------------
# execute_with_policy integration
# ---------------------------------------------------------------------------

class TestExecuteWithPolicyIntegration:
    def test_execute_with_policy_succeeds(self):
        from models.execution import execute_with_policy
        from models.contracts import ModelExecutionPolicy
        from semantic.contracts import make_task

        adapter = _make_adapter()
        task = make_task('tagging', 'The Fed held rates.')
        from models.contracts import task_to_request
        request = task_to_request(task, adapter.adapter_name, adapter.adapter_version)
        policy = ModelExecutionPolicy(timeout_seconds=30.0)

        mock_resp = _make_ollama_response()
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            result = execute_with_policy(adapter, request, policy=policy, task=task)

        assert result.success is True
        assert result.semantic_result is not None


# ---------------------------------------------------------------------------
# Ledger integration — raw_output persisted
# ---------------------------------------------------------------------------

class TestLedgerRawOutputPersistence:
    def test_record_run_persists_raw_output(self, tmp_path):
        from models.adapters import StubModelAdapter
        from semantic.pipeline import run_semantic_task
        from semantic.ledger import init_ledger, record_run, get_run

        db = str(tmp_path / 'ledger.db')
        init_ledger(db)

        pr = run_semantic_task('tagging', 'The Fed held rates.', StubModelAdapter())
        raw_out = {'raw': 'stub raw text', 'model': 'stub', 'done': True}
        record_run(db, pr, raw_output=raw_out)

        run = get_run(db, pr.execution_result.request_id)
        assert run is not None
        assert run.raw_output is not None
        assert run.raw_output['raw'] == 'stub raw text'

    def test_record_run_null_raw_output_by_default(self, tmp_path):
        from models.adapters import StubModelAdapter
        from semantic.pipeline import run_semantic_task
        from semantic.ledger import init_ledger, record_run, get_run

        db = str(tmp_path / 'ledger.db')
        init_ledger(db)

        pr = run_semantic_task('tagging', 'The ECB held rates.', StubModelAdapter())
        record_run(db, pr)  # no raw_output

        run = get_run(db, pr.execution_result.request_id)
        assert run is not None
        assert run.raw_output is None

    def test_ollama_raw_output_round_trips_through_ledger(self, tmp_path):
        """
        Simulate Ollama execute() → record_run() → get_run() and verify
        raw_output is faithfully persisted without a live Ollama process.
        """
        from semantic.contracts import make_task
        from semantic.pipeline import run_semantic_task
        from semantic.ledger import init_ledger, record_run, get_run

        db = str(tmp_path / 'ledger.db')
        init_ledger(db)

        adapter = _make_adapter()
        task = make_task('tagging', 'ECB signals rate cut.')
        mock_resp = _make_ollama_response(
            response_text='{"labels": [{"label": "ecb", "confidence": 4, "rationale": "r"}], "summary": "ECB."}'
        )
        with patch('models.ollama_adapter._requests') as mock_req:
            mock_req.post.return_value = mock_resp
            mock_req.exceptions.Timeout = __import__('requests').exceptions.Timeout
            mock_req.exceptions.ConnectionError = __import__('requests').exceptions.ConnectionError
            pr = run_semantic_task('tagging', 'ECB signals rate cut.', adapter)

        raw_out = pr.execution_result.response.metadata.get('raw_output')
        record_run(db, pr, raw_output=raw_out)

        run = get_run(db, pr.execution_result.request_id)
        assert run.raw_output is not None
        assert 'ecb' in run.raw_output['raw']
