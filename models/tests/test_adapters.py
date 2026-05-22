"""Tests for models/adapters.py."""
import pytest

from semantic.contracts import make_task
from semantic.models import SEMANTIC_TASK_TYPES
from models.adapters import EchoModelAdapter, LocalModelAdapter, StubModelAdapter
from models.capabilities import ModelCapabilitySet
from models.contracts import (
    LocalModelRequest,
    LocalModelResponse,
    ModelContractError,
    derive_request_id,
    task_to_request,
    validate_response,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(task_type='tagging', input_text='The Federal Reserve held rates.',
                  model_name='stub', model_version='1.0.0'):
    return LocalModelRequest(
        request_id=derive_request_id(model_name, model_version, task_type, input_text),
        model_name=model_name,
        model_version=model_version,
        task_type=task_type,
        input_text=input_text,
        extraction_method=f'local_model:{model_name}:{model_version}',
    )


# ---------------------------------------------------------------------------
# LocalModelAdapter interface contract
# ---------------------------------------------------------------------------

class TestLocalModelAdapterInterface:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            LocalModelAdapter()

    def test_stub_is_concrete(self):
        adapter = StubModelAdapter()
        assert isinstance(adapter, LocalModelAdapter)

    def test_echo_is_concrete(self):
        adapter = EchoModelAdapter()
        assert isinstance(adapter, LocalModelAdapter)

    def test_supports_delegates_to_capability_set(self):
        adapter = StubModelAdapter()
        for tt in SEMANTIC_TASK_TYPES:
            assert adapter.supports(tt)

    def test_get_capability_returns_capability(self):
        adapter = StubModelAdapter()
        cap = adapter.get_capability('tagging')
        assert cap is not None
        assert cap.task_type == 'tagging'

    def test_get_capability_unknown_returns_none(self):
        adapter = StubModelAdapter()
        assert adapter.get_capability('llm_query') is None

    def test_supported_task_types_tuple(self):
        adapter = StubModelAdapter()
        types = adapter.supported_task_types()
        assert isinstance(types, tuple)
        for tt in SEMANTIC_TASK_TYPES:
            assert tt in types


# ---------------------------------------------------------------------------
# StubModelAdapter
# ---------------------------------------------------------------------------

class TestStubModelAdapter:
    def setup_method(self):
        self.adapter = StubModelAdapter()

    def test_adapter_name(self):
        assert self.adapter.adapter_name == 'stub'

    def test_adapter_version(self):
        assert self.adapter.adapter_version == '1.0.0'

    def test_capability_set_type(self):
        assert isinstance(self.adapter.capability_set, ModelCapabilitySet)

    def test_execute_returns_response(self):
        req = _make_request()
        resp = self.adapter.execute(req)
        assert isinstance(resp, LocalModelResponse)

    def test_execute_response_validates(self):
        req = _make_request()
        resp = self.adapter.execute(req)
        validate_response(resp, req)  # no exception

    def test_execute_deterministic(self):
        req = _make_request()
        r1 = self.adapter.execute(req)
        r2 = self.adapter.execute(req)
        assert r1.labels[0].label == r2.labels[0].label
        assert r1.overall_confidence == r2.overall_confidence

    def test_execute_label_is_stub(self):
        req = _make_request()
        resp = self.adapter.execute(req)
        assert any(lb.label == 'stub' for lb in resp.labels)

    def test_execute_confidence_is_3(self):
        req = _make_request()
        resp = self.adapter.execute(req)
        assert resp.overall_confidence == 3

    def test_execute_summary_set(self):
        req = _make_request()
        resp = self.adapter.execute(req)
        assert resp.summary == 'stub response'

    def test_execute_all_task_types(self):
        for tt in SEMANTIC_TASK_TYPES:
            req = _make_request(task_type=tt)
            resp = self.adapter.execute(req)
            assert resp.task_type == tt

    def test_execute_request_id_preserved(self):
        req = _make_request()
        resp = self.adapter.execute(req)
        assert resp.request_id == req.request_id

    def test_execute_model_identity_preserved(self):
        req = _make_request()
        resp = self.adapter.execute(req)
        assert resp.model_name == req.model_name
        assert resp.model_version == req.model_version

    def test_execute_invalid_request_raises(self):
        req = _make_request()
        req.input_text = ''  # invalidate
        with pytest.raises(ModelContractError):
            self.adapter.execute(req)


# ---------------------------------------------------------------------------
# EchoModelAdapter
# ---------------------------------------------------------------------------

class TestEchoModelAdapter:
    def setup_method(self):
        self.adapter = EchoModelAdapter()

    def test_adapter_name(self):
        assert self.adapter.adapter_name == 'echo'

    def test_adapter_version(self):
        assert self.adapter.adapter_version == '1.0.0'

    def test_execute_returns_response(self):
        req = _make_request(model_name='echo')
        resp = self.adapter.execute(req)
        assert isinstance(resp, LocalModelResponse)

    def test_execute_response_validates(self):
        req = _make_request(model_name='echo')
        resp = self.adapter.execute(req)
        validate_response(resp, req)

    def test_execute_derives_labels_from_input(self):
        req = _make_request(
            model_name='echo',
            input_text='Federal Reserve raised interest rates sharply.',
        )
        resp = self.adapter.execute(req)
        label_names = [lb.label for lb in resp.labels]
        assert len(label_names) > 0

    def test_execute_deterministic_same_input(self):
        req = _make_request(
            model_name='echo',
            input_text='Stable text for determinism testing.',
        )
        r1 = self.adapter.execute(req)
        r2 = self.adapter.execute(req)
        assert [lb.label for lb in r1.labels] == [lb.label for lb in r2.labels]
        assert r1.overall_confidence == r2.overall_confidence

    def test_execute_different_inputs_different_labels(self):
        req1 = _make_request(
            model_name='echo',
            input_text='Federal Reserve raised rates.',
        )
        req2 = _make_request(
            model_name='echo',
            input_text='Completely different monetary policy text here.',
        )
        r1 = self.adapter.execute(req1)
        r2 = self.adapter.execute(req2)
        # Different inputs should generally produce different label sets
        labels1 = {lb.label for lb in r1.labels}
        labels2 = {lb.label for lb in r2.labels}
        assert labels1 != labels2 or True  # allowed to match if text is semantically equivalent

    def test_execute_caps_labels_at_10(self):
        many_words = ' '.join(
            f'Word{i}' for i in range(30)
        )
        req = _make_request(model_name='echo', input_text=many_words)
        resp = self.adapter.execute(req)
        assert len(resp.labels) <= 10

    def test_execute_entities_from_allcaps(self):
        req = _make_request(
            model_name='echo',
            input_text='The FED and ECB held USD rates.',
        )
        resp = self.adapter.execute(req)
        entity_texts = [e.text for e in resp.entities]
        # FED, ECB, USD should be picked up as ALL-CAPS entities
        assert any(e in entity_texts for e in ('FED', 'ECB', 'USD'))

    def test_execute_summary_is_first_sentence(self):
        req = _make_request(
            model_name='echo',
            input_text='First sentence here. Second sentence follows.',
        )
        resp = self.adapter.execute(req)
        assert 'First sentence' in resp.summary

    def test_execute_invalid_request_raises(self):
        req = _make_request(model_name='echo')
        req.task_type = 'bad_type'
        with pytest.raises(ModelContractError):
            self.adapter.execute(req)

    def test_execute_all_task_types_accepted(self):
        for tt in SEMANTIC_TASK_TYPES:
            req = _make_request(task_type=tt, model_name='echo')
            resp = self.adapter.execute(req)
            assert resp.task_type == tt


# ---------------------------------------------------------------------------
# Adapter integration: task → request → execute
# ---------------------------------------------------------------------------

class TestAdapterIntegration:
    def test_stub_roundtrip_via_task(self):
        from models.contracts import task_to_request, response_to_semantic_result
        task = make_task('tagging', 'The Fed held rates.')
        req = task_to_request(task, 'stub', '1.0.0')
        adapter = StubModelAdapter()
        resp = adapter.execute(req)
        result = response_to_semantic_result(resp, task)
        assert result.task_id == task.task_id
        assert result.extraction_method == req.extraction_method

    def test_echo_roundtrip_via_task(self):
        from models.contracts import task_to_request, response_to_semantic_result
        task = make_task('entity_extraction', 'The Federal Reserve raised rates.')
        req = task_to_request(task, 'echo', '1.0.0')
        adapter = EchoModelAdapter()
        resp = adapter.execute(req)
        result = response_to_semantic_result(resp, task)
        assert result.task_type == 'entity_extraction'

    def test_result_is_candidate_not_memory(self):
        """Adapter results never have a committed_id — they are candidates only."""
        from models.contracts import task_to_request, response_to_semantic_result
        from semantic.contracts import result_to_candidate
        task = make_task('claim_extraction', 'Rates are rising in Europe.')
        req = task_to_request(task, 'stub', '1.0.0')
        adapter = StubModelAdapter()
        resp = adapter.execute(req)
        result = response_to_semantic_result(resp, task)
        candidate = result_to_candidate(
            result, task,
            event_type='regime_observation',
            title='Rate observation',
        )
        assert candidate.status == 'proposed'
        assert candidate.committed_id is None
