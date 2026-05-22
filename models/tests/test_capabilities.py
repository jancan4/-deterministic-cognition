"""Tests for models/capabilities.py."""
import pytest

from semantic.models import SEMANTIC_TASK_TYPES
from models.capabilities import (
    CapabilityError,
    ModelCapability,
    ModelCapabilitySet,
    build_full_capability_set,
    check_model_supports,
    validate_capability,
)


# ---------------------------------------------------------------------------
# ModelCapability
# ---------------------------------------------------------------------------

class TestModelCapability:
    def test_valid_creation(self):
        cap = ModelCapability(task_type='tagging', max_input_chars=4096)
        assert cap.task_type == 'tagging'
        assert cap.max_input_chars == 4096
        assert cap.deterministic_mode_supported is True
        assert cap.provenance_supported is True
        assert cap.confidence_supported is True

    def test_all_approved_task_types(self):
        for tt in SEMANTIC_TASK_TYPES:
            cap = ModelCapability(task_type=tt, max_input_chars=1024)
            assert cap.task_type == tt

    def test_invalid_task_type_raises(self):
        with pytest.raises(CapabilityError, match='task_type'):
            ModelCapability(task_type='llm_query', max_input_chars=1024)

    def test_zero_max_input_chars_raises(self):
        with pytest.raises(CapabilityError, match='> 0'):
            ModelCapability(task_type='tagging', max_input_chars=0)

    def test_negative_max_input_chars_raises(self):
        with pytest.raises(CapabilityError, match='> 0'):
            ModelCapability(task_type='tagging', max_input_chars=-1)

    def test_non_int_max_input_chars_raises(self):
        with pytest.raises(CapabilityError):
            ModelCapability(task_type='tagging', max_input_chars=4096.0)

    def test_to_dict_keys(self):
        cap = ModelCapability(task_type='entity_extraction', max_input_chars=2048)
        d = cap.to_dict()
        assert set(d.keys()) == {
            'task_type', 'max_input_chars',
            'deterministic_mode_supported', 'provenance_supported', 'confidence_supported',
        }

    def test_from_dict_roundtrip(self):
        cap = ModelCapability(
            task_type='claim_extraction', max_input_chars=8192,
            deterministic_mode_supported=False,
        )
        cap2 = ModelCapability.from_dict(cap.to_dict())
        assert cap2.task_type == 'claim_extraction'
        assert cap2.max_input_chars == 8192
        assert cap2.deterministic_mode_supported is False

    def test_optional_flags_false(self):
        cap = ModelCapability(
            task_type='tagging', max_input_chars=1024,
            provenance_supported=False,
            confidence_supported=False,
        )
        assert cap.provenance_supported is False
        assert cap.confidence_supported is False


# ---------------------------------------------------------------------------
# validate_capability
# ---------------------------------------------------------------------------

class TestValidateCapability:
    def test_valid(self):
        validate_capability(ModelCapability(task_type='tagging', max_input_chars=512))

    def test_not_a_capability_raises(self):
        with pytest.raises(CapabilityError):
            validate_capability({'task_type': 'tagging', 'max_input_chars': 512})


# ---------------------------------------------------------------------------
# ModelCapabilitySet
# ---------------------------------------------------------------------------

class TestModelCapabilitySet:
    def _make_set(self, task_types=('tagging', 'entity_extraction')):
        caps = [ModelCapability(task_type=tt, max_input_chars=2048) for tt in task_types]
        return ModelCapabilitySet('test-adapter', '1.0', caps)

    def test_supports_true(self):
        cs = self._make_set()
        assert cs.supports('tagging') is True

    def test_supports_false(self):
        cs = self._make_set()
        assert cs.supports('claim_extraction') is False

    def test_get_returns_capability(self):
        cs = self._make_set()
        cap = cs.get('tagging')
        assert cap is not None
        assert cap.task_type == 'tagging'

    def test_get_returns_none_for_unknown(self):
        cs = self._make_set()
        assert cs.get('summary_extraction') is None

    def test_supported_task_types(self):
        cs = self._make_set()
        assert 'tagging' in cs.supported_task_types()
        assert 'entity_extraction' in cs.supported_task_types()
        assert 'claim_extraction' not in cs.supported_task_types()

    def test_empty_adapter_name_raises(self):
        with pytest.raises(CapabilityError):
            ModelCapabilitySet('', '1.0', [])

    def test_empty_adapter_version_raises(self):
        with pytest.raises(CapabilityError):
            ModelCapabilitySet('adapter', '', [])

    def test_to_dict_structure(self):
        cs = self._make_set(task_types=('tagging',))
        d = cs.to_dict()
        assert d['adapter_name'] == 'test-adapter'
        assert d['adapter_version'] == '1.0'
        assert len(d['capabilities']) == 1


# ---------------------------------------------------------------------------
# check_model_supports
# ---------------------------------------------------------------------------

class TestCheckModelSupports:
    def _make_set(self, task_type='tagging', max_input_chars=100):
        cap = ModelCapability(task_type=task_type, max_input_chars=max_input_chars)
        return ModelCapabilitySet('adapter', '1.0', [cap])

    def test_supported_task_type(self):
        cs = self._make_set()
        cap = check_model_supports(cs, 'tagging')
        assert cap.task_type == 'tagging'

    def test_unsupported_task_type_raises(self):
        cs = self._make_set()
        with pytest.raises(CapabilityError, match='does not support'):
            check_model_supports(cs, 'entity_extraction')

    def test_input_within_limit(self):
        cs = self._make_set(max_input_chars=50)
        check_model_supports(cs, 'tagging', input_text='short text')

    def test_input_exceeds_limit_raises(self):
        cs = self._make_set(max_input_chars=5)
        with pytest.raises(CapabilityError, match='exceeds'):
            check_model_supports(cs, 'tagging', input_text='this is much longer than five chars')

    def test_exact_limit_ok(self):
        cs = self._make_set(max_input_chars=5)
        check_model_supports(cs, 'tagging', input_text='12345')


# ---------------------------------------------------------------------------
# build_full_capability_set
# ---------------------------------------------------------------------------

class TestBuildFullCapabilitySet:
    def test_covers_all_task_types(self):
        cs = build_full_capability_set('adapter', '1.0')
        for tt in SEMANTIC_TASK_TYPES:
            assert cs.supports(tt), f"Missing capability for {tt!r}"

    def test_custom_max_input_chars(self):
        cs = build_full_capability_set('adapter', '1.0', max_input_chars=8192)
        for tt in SEMANTIC_TASK_TYPES:
            assert cs.get(tt).max_input_chars == 8192

    def test_deterministic_mode_propagated(self):
        cs = build_full_capability_set('adapter', '1.0', deterministic_mode_supported=False)
        for tt in SEMANTIC_TASK_TYPES:
            assert cs.get(tt).deterministic_mode_supported is False

    def test_adapter_name_preserved(self):
        cs = build_full_capability_set('my-adapter', '2.3.1')
        assert cs.adapter_name == 'my-adapter'
        assert cs.adapter_version == '2.3.1'
