"""
Local model adapter interface and built-in stub implementations.

LocalModelAdapter is the abstract base class that all model adapters must
implement. It defines the execution contract without any inference logic.

Built-in adapters (for testing and interface validation only):
  StubModelAdapter  — returns a fixed deterministic response regardless of input
  EchoModelAdapter  — derives labels from capitalised words in input text

No real inference is performed by any adapter in this module.
"""
import hashlib
import re
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from semantic.models import (
    EXTRACTION_METHOD_RULE_BASED,
    ExtractedEntity,
    SemanticLabel,
)

from .capabilities import ModelCapability, ModelCapabilitySet, build_full_capability_set
from .contracts import (
    LocalModelRequest,
    LocalModelResponse,
    ModelContractError,
    _now,
    validate_request,
)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LocalModelAdapter(ABC):
    """
    Abstract base class for all local model adapters.

    Subclasses must implement:
      adapter_name      — stable identifier (e.g. 'phi3-mini')
      adapter_version   — semver string (e.g. '1.0.0')
      capability_set    — ModelCapabilitySet declaring supported task types
      execute()         — synchronous, side-effect-free inference call

    Contracts:
      - execute() must return a LocalModelResponse that is consistent with
        the request (same request_id, model_name, model_version, task_type).
      - execute() must not write to any database.
      - execute() must not make network calls.
      - execute() must not mutate the request.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable human-readable adapter identifier."""

    @property
    @abstractmethod
    def adapter_version(self) -> str:
        """Adapter version string."""

    @property
    @abstractmethod
    def capability_set(self) -> ModelCapabilitySet:
        """Declared capabilities for this adapter."""

    @abstractmethod
    def execute(self, request: LocalModelRequest) -> LocalModelResponse:
        """
        Run the extraction task described by request.

        Must validate the request before processing.
        Must return a LocalModelResponse consistent with the request.
        Must not write to any database or make network calls.
        """

    def supports(self, task_type: str) -> bool:
        """Return True if this adapter supports the given task type."""
        return self.capability_set.supports(task_type)

    def get_capability(self, task_type: str) -> Optional[ModelCapability]:
        """Return the ModelCapability for task_type, or None."""
        return self.capability_set.get(task_type)

    def supported_task_types(self) -> Tuple[str, ...]:
        """Return a tuple of all supported task type strings."""
        return self.capability_set.supported_task_types()

    def _make_response(
        self,
        request: LocalModelRequest,
        overall_confidence: int,
        labels: Optional[List[SemanticLabel]] = None,
        entities: Optional[List[ExtractedEntity]] = None,
        summary: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> LocalModelResponse:
        """Convenience builder for subclasses."""
        return LocalModelResponse(
            request_id=request.request_id,
            model_name=request.model_name,
            model_version=request.model_version,
            task_type=request.task_type,
            extraction_method=request.extraction_method,
            overall_confidence=overall_confidence,
            responded_at=_now(),
            labels=labels or [],
            entities=entities or [],
            claims=[],
            relations=[],
            summary=summary,
            metadata=metadata or {},
        )


# ---------------------------------------------------------------------------
# StubModelAdapter
# ---------------------------------------------------------------------------

class StubModelAdapter(LocalModelAdapter):
    """
    A deterministic stub adapter for testing.

    Always returns:
      - one label: 'stub' with confidence 3
      - overall_confidence: 3
      - summary: 'stub response'

    The response content is independent of the input, making it maximally
    predictable for interface-level tests.
    """

    NAME = 'stub'
    VERSION = '1.0.0'

    @property
    def adapter_name(self) -> str:
        return self.NAME

    @property
    def adapter_version(self) -> str:
        return self.VERSION

    @property
    def capability_set(self) -> ModelCapabilitySet:
        return build_full_capability_set(
            adapter_name=self.NAME,
            adapter_version=self.VERSION,
            max_input_chars=65536,
        )

    def execute(self, request: LocalModelRequest) -> LocalModelResponse:
        validate_request(request)
        if not self.supports(request.task_type):
            raise ModelContractError(
                f"StubModelAdapter does not support task_type {request.task_type!r}"
            )
        return self._make_response(
            request=request,
            overall_confidence=3,
            labels=[SemanticLabel(label='stub', confidence=3, rationale='stub adapter')],
            summary='stub response',
            metadata={'adapter': self.NAME, 'version': self.VERSION},
        )


# ---------------------------------------------------------------------------
# EchoModelAdapter
# ---------------------------------------------------------------------------

class EchoModelAdapter(LocalModelAdapter):
    """
    A deterministic echo adapter for testing.

    Derives labels from the input text by finding all title-cased tokens
    (capitalised words of 3+ characters that are not stop words). Confidence
    is proportional to how many such tokens appear.

    The response is a pure function of the input: same input → same labels.
    No state. No randomness.
    """

    NAME = 'echo'
    VERSION = '1.0.0'

    _STOP_WORDS = frozenset({
        'The', 'A', 'An', 'And', 'But', 'Or', 'For', 'Nor', 'So',
        'Yet', 'In', 'On', 'At', 'To', 'By', 'Of', 'From', 'With',
        'Is', 'Are', 'Was', 'Were', 'Be', 'Been', 'Being',
        'Has', 'Have', 'Had', 'Do', 'Does', 'Did',
        'Will', 'Would', 'Could', 'Should', 'May', 'Might',
    })

    @property
    def adapter_name(self) -> str:
        return self.NAME

    @property
    def adapter_version(self) -> str:
        return self.VERSION

    @property
    def capability_set(self) -> ModelCapabilitySet:
        return build_full_capability_set(
            adapter_name=self.NAME,
            adapter_version=self.VERSION,
            max_input_chars=8192,
        )

    def execute(self, request: LocalModelRequest) -> LocalModelResponse:
        validate_request(request)
        if not self.supports(request.task_type):
            raise ModelContractError(
                f"EchoModelAdapter does not support task_type {request.task_type!r}"
            )

        labels = self._extract_labels(request.input_text)
        entities = self._extract_entities(request.input_text)
        confidence = min(5, max(1, len(labels) + 1))

        return self._make_response(
            request=request,
            overall_confidence=confidence,
            labels=labels,
            entities=entities,
            summary=self._extract_summary(request.input_text),
            metadata={'echo_token_count': len(labels)},
        )

    def _extract_labels(self, text: str) -> List[SemanticLabel]:
        """Extract unique capitalised non-stop-word tokens as labels."""
        tokens = re.findall(r'\b[A-Z][a-z]{2,}\b', text)
        seen = []
        for tok in tokens:
            if tok not in self._STOP_WORDS and tok.lower() not in seen:
                seen.append(tok.lower())
        return [
            SemanticLabel(label=tok, confidence=3, rationale='echo:title-case')
            for tok in seen[:10]
        ]

    def _extract_entities(self, text: str) -> List[ExtractedEntity]:
        """Extract ALL-CAPS tokens as entities (simple heuristic)."""
        tokens = re.findall(r'\b[A-Z]{2,}\b', text)
        seen = []
        for tok in tokens:
            if tok not in seen:
                seen.append(tok)
        return [
            ExtractedEntity(
                text=tok,
                entity_type='unknown',
                confidence=2,
                rationale='echo:all-caps',
            )
            for tok in seen[:5]
        ]

    def _extract_summary(self, text: str) -> str:
        first_sentence = re.split(r'[.!?]', text.strip())[0].strip()
        return first_sentence[:200] if first_sentence else text[:200]
