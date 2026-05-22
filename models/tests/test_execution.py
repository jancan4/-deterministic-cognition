"""Tests for models/execution.py."""
import pytest

from semantic.contracts import make_task
from models.adapters import EchoModelAdapter, StubModelAdapter
from models.contracts import (
    LocalModelRequest,
    LocalModelResponse,
    ModelContractError,
    ModelExecutionPolicy,
    derive_request_id,
    task_to_request,
)
from models.execution import DEFAULT_POLICY, execute_with_policy, make_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(task_type='tagging', input_text='The Fed held rates.',
                  model_name='stub', model_version='1.0.0'):
    return LocalModelRequest(
        request_id=derive_request_id(model_name, model_version, task_type, input_text),
        model_name=model_name,
        model_version=model_version,
        task_type=task_type,
        input_text=input_text,
        extraction_method=f'local_model:{model_name}:{model_version}',
    )


class _FailingAdapter(StubModelAdapter):
    """Always raises ModelContractError on execute()."""
    NAME = 'failing'
    VERSION = '1.0.0'

    def execute(self, request):
        validate_request = __import__(
            'models.contracts', fromlist=['validate_request']
        ).validate_request
        validate_request(request)
        raise ModelContractError("simulated adapter failure")


class _CountingAdapter(StubModelAdapter):
    """Counts executions, fails on first N attempts."""
    NAME = 'counting'
    VERSION = '1.0.0'

    def __init__(self, fail_first_n: int = 0):
        self.call_count = 0
        self.fail_first_n = fail_first_n

    def execute(self, request):
        self.call_count += 1
        if self.call_count <= self.fail_first_n:
            raise ModelContractError(f"simulated failure on attempt {self.call_count}")
        return super().execute(request)


# ---------------------------------------------------------------------------
# DEFAULT_POLICY
# ---------------------------------------------------------------------------

class TestDefaultPolicy:
    def test_type(self):
        assert isinstance(DEFAULT_POLICY, ModelExecutionPolicy)

    def test_deterministic_mode(self):
        assert DEFAULT_POLICY.deterministic_mode is True

    def test_max_retries_zero(self):
        assert DEFAULT_POLICY.max_retries == 0


# ---------------------------------------------------------------------------
# make_policy
# ---------------------------------------------------------------------------

class TestMakePolicy:
    def test_defaults(self):
        p = make_policy()
        assert p.timeout_seconds == 30.0
        assert p.max_retries == 0
        assert p.deterministic_mode is True

    def test_custom(self):
        p = make_policy(timeout_seconds=60.0, max_retries=3, deterministic_mode=False)
        assert p.timeout_seconds == 60.0
        assert p.max_retries == 3
        assert p.deterministic_mode is False

    def test_invalid_timeout_raises(self):
        with pytest.raises(ModelContractError):
            make_policy(timeout_seconds=-1.0)


# ---------------------------------------------------------------------------
# execute_with_policy — success cases
# ---------------------------------------------------------------------------

class TestExecuteSuccess:
    def test_basic_success(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.success is True
        assert result.error is None

    def test_response_present(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert isinstance(result.response, LocalModelResponse)

    def test_semantic_result_none_without_task(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.semantic_result is None

    def test_semantic_result_present_with_task(self):
        from semantic.models import SemanticExtractionResult
        adapter = StubModelAdapter()
        task = make_task('tagging', 'The Fed held rates.')
        req = task_to_request(task, 'stub', '1.0.0')
        result = execute_with_policy(adapter, req, task=task)
        assert result.success is True
        assert isinstance(result.semantic_result, SemanticExtractionResult)

    def test_echo_adapter_success(self):
        adapter = EchoModelAdapter()
        req = _make_request(model_name='echo')
        result = execute_with_policy(adapter, req)
        assert result.success is True

    def test_default_policy_used_when_none(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req, policy=None)
        assert result.success is True


# ---------------------------------------------------------------------------
# execute_with_policy — execution metadata
# ---------------------------------------------------------------------------

class TestExecutionMetadata:
    def test_adapter_name_in_result(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.adapter_name == 'stub'

    def test_adapter_version_in_result(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.adapter_version == '1.0.0'

    def test_request_id_in_result(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.request_id == req.request_id

    def test_started_at_set(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.started_at and 'T' in result.started_at

    def test_completed_at_set(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.completed_at and 'T' in result.completed_at

    def test_duration_ms_positive(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.duration_ms >= 0.0

    def test_retry_count_zero_on_success(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.retry_count == 0

    def test_timeout_applied_false_for_stub(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        assert result.timeout_applied is False

    def test_to_dict_has_all_metadata_keys(self):
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        d = result.to_dict()
        for key in ('started_at', 'completed_at', 'duration_ms', 'timeout_applied',
                    'retry_count', 'adapter_name', 'adapter_version', 'request_id'):
            assert key in d, f"Missing key: {key}"

    def test_to_json_valid(self):
        import json
        adapter = StubModelAdapter()
        req = _make_request()
        result = execute_with_policy(adapter, req)
        parsed = json.loads(result.to_json())
        assert parsed['success'] is True
        assert parsed['adapter_name'] == 'stub'


# ---------------------------------------------------------------------------
# execute_with_policy — failure cases
# ---------------------------------------------------------------------------

class TestExecuteFailure:
    def test_invalid_request_returns_failure(self):
        adapter = StubModelAdapter()
        req = _make_request()
        req.input_text = ''  # invalidate
        result = execute_with_policy(adapter, req)
        assert result.success is False
        assert result.error is not None

    def test_adapter_failure_returns_failure(self):
        adapter = _FailingAdapter()
        req = _make_request(model_name='failing')
        result = execute_with_policy(adapter, req)
        assert result.success is False
        assert 'simulated adapter failure' in result.error

    def test_capability_not_supported_returns_failure(self):
        from models.capabilities import ModelCapabilitySet, ModelCapability
        from models.adapters import LocalModelAdapter

        class LimitedAdapter(StubModelAdapter):
            NAME = 'limited'
            VERSION = '1.0.0'

            @property
            def capability_set(self):
                cap = ModelCapability(task_type='tagging', max_input_chars=10)
                return ModelCapabilitySet('limited', '1.0.0', [cap])

        adapter = LimitedAdapter()
        req = _make_request(task_type='entity_extraction', model_name='limited')
        result = execute_with_policy(adapter, req)
        assert result.success is False
        assert result.error is not None

    def test_input_too_long_for_capability_returns_failure(self):
        from models.capabilities import ModelCapabilitySet, ModelCapability

        class TinyAdapter(StubModelAdapter):
            NAME = 'tiny'
            VERSION = '1.0.0'

            @property
            def capability_set(self):
                cap = ModelCapability(task_type='tagging', max_input_chars=5)
                return ModelCapabilitySet('tiny', '1.0.0', [cap])

        adapter = TinyAdapter()
        req = _make_request(task_type='tagging', model_name='tiny',
                            input_text='this is much longer than 5 chars')
        result = execute_with_policy(adapter, req)
        assert result.success is False

    def test_failure_result_has_no_response_content(self):
        adapter = _FailingAdapter()
        req = _make_request(model_name='failing')
        result = execute_with_policy(adapter, req)
        assert result.success is False
        assert result.semantic_result is None


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------

class TestRetryBehavior:
    def test_zero_retries_no_retry_on_failure(self):
        adapter = _CountingAdapter(fail_first_n=1)
        req = _make_request()
        policy = make_policy(max_retries=0)
        result = execute_with_policy(adapter, req, policy=policy)
        assert result.success is False
        assert adapter.call_count == 1
        assert result.retry_count == 0

    def test_one_retry_succeeds_on_second_attempt(self):
        adapter = _CountingAdapter(fail_first_n=1)
        req = _make_request()
        policy = make_policy(max_retries=1)
        result = execute_with_policy(adapter, req, policy=policy)
        assert result.success is True
        assert adapter.call_count == 2
        assert result.retry_count == 1

    def test_two_retries_exhausted(self):
        adapter = _CountingAdapter(fail_first_n=5)
        req = _make_request()
        policy = make_policy(max_retries=2)
        result = execute_with_policy(adapter, req, policy=policy)
        assert result.success is False
        assert adapter.call_count == 3  # 1 attempt + 2 retries
        assert result.retry_count == 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_repeated_execution_same_content(self):
        import json
        adapter = StubModelAdapter()
        req = _make_request()
        r1 = execute_with_policy(adapter, req)
        r2 = execute_with_policy(adapter, req)
        # Core content must match (timestamps will differ)
        assert r1.success == r2.success
        d1 = r1.to_dict()
        d2 = r2.to_dict()
        assert d1['request_id'] == d2['request_id']
        assert d1['adapter_name'] == d2['adapter_name']
        assert (d1['response'] or {}).get('labels') == (d2['response'] or {}).get('labels')

    def test_semantic_result_deterministic_content(self):
        adapter = StubModelAdapter()
        task = make_task('tagging', 'The same stable input text.')
        req = task_to_request(task, 'stub', '1.0.0')
        r1 = execute_with_policy(adapter, req, task=task)
        r2 = execute_with_policy(adapter, req, task=task)
        assert r1.semantic_result.task_id == r2.semantic_result.task_id
        l1 = [lb.label for lb in r1.semantic_result.labels]
        l2 = [lb.label for lb in r2.semantic_result.labels]
        assert l1 == l2
