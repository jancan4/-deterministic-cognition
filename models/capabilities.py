"""
Model capability declarations.

A ModelCapability describes what one model can do for one task type.
Adapters declare their capabilities explicitly; no implicit capabilities
are ever assumed.

Capabilities are static metadata — they do not change at runtime and
carry no model weights, inference logic, or network state.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from semantic.models import SEMANTIC_TASK_TYPES


class CapabilityError(ValueError):
    pass


# ---------------------------------------------------------------------------
# ModelCapability
# ---------------------------------------------------------------------------

@dataclass
class ModelCapability:
    """
    Declares what a model adapter can do for a single task type.

    max_input_chars: hard limit; requests exceeding this must be rejected.
    deterministic_mode_supported: True if the adapter can guarantee
        identical output for identical input (e.g. temperature=0).
    provenance_supported: True if the adapter returns source_span in results.
    confidence_supported: True if the adapter returns calibrated 1–5 scores.
    """
    task_type: str
    max_input_chars: int
    deterministic_mode_supported: bool = True
    provenance_supported: bool = True
    confidence_supported: bool = True

    def __post_init__(self) -> None:
        if self.task_type not in SEMANTIC_TASK_TYPES:
            raise CapabilityError(
                f"task_type {self.task_type!r} is not an approved semantic task type. "
                f"Approved: {SEMANTIC_TASK_TYPES}"
            )
        if not isinstance(self.max_input_chars, int) or isinstance(self.max_input_chars, bool):
            raise CapabilityError("max_input_chars must be a positive integer")
        if self.max_input_chars <= 0:
            raise CapabilityError(
                f"max_input_chars must be > 0, got {self.max_input_chars}"
            )

    def to_dict(self) -> dict:
        return {
            'task_type': self.task_type,
            'max_input_chars': self.max_input_chars,
            'deterministic_mode_supported': self.deterministic_mode_supported,
            'provenance_supported': self.provenance_supported,
            'confidence_supported': self.confidence_supported,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ModelCapability':
        return cls(
            task_type=d['task_type'],
            max_input_chars=d['max_input_chars'],
            deterministic_mode_supported=d.get('deterministic_mode_supported', True),
            provenance_supported=d.get('provenance_supported', True),
            confidence_supported=d.get('confidence_supported', True),
        )


# ---------------------------------------------------------------------------
# Capability set helper
# ---------------------------------------------------------------------------

@dataclass
class ModelCapabilitySet:
    """
    The full set of capabilities declared by one model adapter.

    Indexed by task_type for O(1) lookup.
    """
    adapter_name: str
    adapter_version: str
    capabilities: List[ModelCapability] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.adapter_name or not self.adapter_name.strip():
            raise CapabilityError("adapter_name must not be empty")
        if not self.adapter_version or not self.adapter_version.strip():
            raise CapabilityError("adapter_version must not be empty")

    def _index(self) -> dict:
        return {c.task_type: c for c in self.capabilities}

    def supports(self, task_type: str) -> bool:
        return task_type in self._index()

    def get(self, task_type: str) -> Optional[ModelCapability]:
        return self._index().get(task_type)

    def supported_task_types(self) -> Tuple[str, ...]:
        return tuple(c.task_type for c in self.capabilities)

    def to_dict(self) -> dict:
        return {
            'adapter_name': self.adapter_name,
            'adapter_version': self.adapter_version,
            'capabilities': [c.to_dict() for c in self.capabilities],
        }


# ---------------------------------------------------------------------------
# Validators and helpers
# ---------------------------------------------------------------------------

def validate_capability(capability: ModelCapability) -> None:
    """
    Validate a ModelCapability.

    Raises CapabilityError if invalid.
    The dataclass __post_init__ catches most issues; this adds extra checks
    for callers building capabilities from external data.
    """
    if not isinstance(capability, ModelCapability):
        raise CapabilityError(
            f"Expected ModelCapability, got {type(capability).__name__}"
        )
    # __post_init__ already ran; re-validate task_type for defence-in-depth
    if capability.task_type not in SEMANTIC_TASK_TYPES:
        raise CapabilityError(
            f"Invalid task_type {capability.task_type!r}"
        )
    if capability.max_input_chars <= 0:
        raise CapabilityError("max_input_chars must be > 0")


def check_model_supports(
    capability_set: ModelCapabilitySet,
    task_type: str,
    input_text: Optional[str] = None,
) -> ModelCapability:
    """
    Assert that a model supports a given task type.

    Optionally checks that input_text does not exceed max_input_chars.
    Returns the matching ModelCapability on success.
    Raises CapabilityError if not supported or input too long.
    """
    cap = capability_set.get(task_type)
    if cap is None:
        raise CapabilityError(
            f"Adapter {capability_set.adapter_name!r} does not support "
            f"task_type {task_type!r}. "
            f"Supported: {capability_set.supported_task_types()}"
        )
    if input_text is not None and len(input_text) > cap.max_input_chars:
        raise CapabilityError(
            f"input_text length {len(input_text)} exceeds "
            f"max_input_chars={cap.max_input_chars} for "
            f"task_type={task_type!r} on adapter {capability_set.adapter_name!r}"
        )
    return cap


def build_full_capability_set(
    adapter_name: str,
    adapter_version: str,
    max_input_chars: int = 4096,
    deterministic_mode_supported: bool = True,
    provenance_supported: bool = True,
    confidence_supported: bool = True,
) -> ModelCapabilitySet:
    """
    Build a ModelCapabilitySet covering all approved semantic task types
    with uniform parameters. Convenience helper for stub/test adapters.
    """
    caps = [
        ModelCapability(
            task_type=tt,
            max_input_chars=max_input_chars,
            deterministic_mode_supported=deterministic_mode_supported,
            provenance_supported=provenance_supported,
            confidence_supported=confidence_supported,
        )
        for tt in SEMANTIC_TASK_TYPES
    ]
    return ModelCapabilitySet(
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        capabilities=caps,
    )
