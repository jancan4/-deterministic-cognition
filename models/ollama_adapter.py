"""
Ollama-compatible local model adapter.

OllamaAdapter wraps the Ollama HTTP API (/api/generate) as a LocalModelAdapter.
It is the first real-inference adapter in this system; all others (stub, echo)
are deterministic test stubs.

Import safety
-------------
This module is safe to import without 'requests' installed. The ImportError
is deferred to OllamaAdapter instantiation so the rest of the adapter layer
remains usable even when Ollama is not configured.

Determinism and replay semantics
---------------------------------
Ollama inference is NOT bitwise deterministic. Even with temperature=0 and
seed=42, different model versions, quantization formats, or hardware may
produce different token sequences. Therefore:

  - ModelCapability.deterministic_mode_supported = False for this adapter.
  - Canonical truth is the RECORDED artifact (normalized_result_json,
    raw_output_json in the semantic ledger), not a re-query of Ollama.
  - 'Replaying' a run means reading the ledger row, not regenerating tokens.
  - temperature=0 and a fixed seed are defaults that improve stability but
    do not constitute a guarantee.

Provenance
----------
Every execute() call populates LocalModelResponse.metadata with a complete
provenance dict including adapter identity, runtime version, model name,
model digest (from Ollama /api/show), inference parameters, prompt template
hash, request payload hash, and Ollama eval statistics.

The raw Ollama response text is returned in metadata['raw_output'] so the
caller can persist it in semantic_execution_runs.raw_output_json via
record_run(raw_output=...).

Governance
----------
Ollama output is candidate-only. No memory write occurs in this module.
All output follows the same SemanticPipelineResult → promote_candidate()
→ operator review path as stub/echo adapters.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from semantic.models import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    EXTRACTION_METHOD_LOCAL_MODEL_PREFIX,
    EXTRACTION_POLARITY_VALUES,
    SEMANTIC_TASK_TYPES,
    ExtractedClaim,
    ExtractedEntity,
    ExtractedRelation,
    SemanticLabel,
)

from .adapters import LocalModelAdapter
from .capabilities import ModelCapability, ModelCapabilitySet
from .contracts import (
    LocalModelRequest,
    LocalModelResponse,
    ModelContractError,
    _now,
)

# ---------------------------------------------------------------------------
# Optional requests import — deferred to instantiation
# ---------------------------------------------------------------------------

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _requests = None  # type: ignore[assignment]
    _HAS_REQUESTS = False

_REQUESTS_INSTALL_MSG = (
    "OllamaAdapter requires 'requests'. "
    "Install with: pip install requests"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLLAMA_ADAPTER_NAME = 'ollama'
OLLAMA_DEFAULT_BASE_URL = 'http://localhost:11434'
OLLAMA_DEFAULT_TEMPERATURE = 0.0
OLLAMA_DEFAULT_SEED = 42
OLLAMA_DEFAULT_NUM_PREDICT = 512
OLLAMA_DEFAULT_TIMEOUT = 60.0
OLLAMA_MAX_INPUT_CHARS = 32768

# ---------------------------------------------------------------------------
# Prompt templates — one per task type
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATES: Dict[str, str] = {
    'tagging': (
        "You are a financial text classifier. Extract semantic labels from the following text.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"labels": [{{"label": "<str>", "confidence": <1-5>, "rationale": "<str>"}}], '
        '"summary": "<str>"}}\n\n'
        "Text:\n{input_text}"
    ),
    'polarity_classification': (
        "You are a financial sentiment analyser. Classify the polarity of the following text.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"polarity": "<positive|negative|neutral|uncertain>", "confidence": <1-5>, '
        '"claims": [{{"text": "<str>", "polarity": "<positive|negative|neutral|uncertain>", '
        '"confidence": <1-5>}}], "summary": "<str>"}}\n\n'
        "Text:\n{input_text}"
    ),
    'entity_extraction': (
        "You are a named-entity recogniser for financial text. Extract entities from the following text.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"entities": [{{"text": "<str>", "entity_type": "<str>", "confidence": <1-5>}}], '
        '"summary": "<str>"}}\n\n'
        "Text:\n{input_text}"
    ),
    'claim_extraction': (
        "You are a financial fact extractor. Extract factual claims from the following text.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"claims": [{{"text": "<str>", "polarity": "<positive|negative|neutral|uncertain>", '
        '"confidence": <1-5>}}], "summary": "<str>"}}\n\n'
        "Text:\n{input_text}"
    ),
    'relation_extraction': (
        "You are a relation extractor for financial text. Extract subject-predicate-object triples.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"relations": [{{"subject": "<str>", "predicate": "<str>", "object": "<str>"}}], '
        '"summary": "<str>"}}\n\n'
        "Text:\n{input_text}"
    ),
    'summary_extraction': (
        "You are a financial text summariser. Produce a concise summary.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"summary": "<str>", "confidence": <1-5>}}\n\n'
        "Text:\n{input_text}"
    ),
    'clustering_hint': (
        "You are a financial topic classifier. Identify thematic clusters in the following text.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"labels": [{{"label": "<str>", "confidence": <1-5>, "rationale": "<str>"}}], '
        '"summary": "<str>"}}\n\n'
        "Text:\n{input_text}"
    ),
    'memory_candidate_classification': (
        "You are a financial memory classifier. Determine if the following text contains a "
        "durable, referenceable insight worth storing as a governed memory event.\n"
        "Respond ONLY with a JSON object in this exact format, with no other text:\n"
        '{{"labels": [{{"label": "<str>", "confidence": <1-5>, "rationale": "<str>"}}], '
        '"claims": [{{"text": "<str>", "polarity": "<positive|negative|neutral|uncertain>", '
        '"confidence": <1-5>}}], "summary": "<str>"}}\n\n'
        "Text:\n{input_text}"
    ),
}

assert set(_PROMPT_TEMPLATES.keys()) == set(SEMANTIC_TASK_TYPES), (
    "Prompt template keys must match SEMANTIC_TASK_TYPES exactly"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _clamp_confidence(val) -> int:
    """Coerce a value to a valid confidence int in [1, 5]."""
    try:
        v = int(val)
    except (TypeError, ValueError):
        return 3
    return max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, v))


def _safe_str(val, max_len: int = 500) -> str:
    if not isinstance(val, str):
        return ''
    return val[:max_len]


def _normalise_polarity(val) -> str:
    if isinstance(val, str) and val in EXTRACTION_POLARITY_VALUES:
        return val
    return 'uncertain'


def _template_hash(task_type: str) -> str:
    return _sha16(_PROMPT_TEMPLATES[task_type])


def _build_prompt(task_type: str, input_text: str) -> str:
    return _PROMPT_TEMPLATES[task_type].format(input_text=input_text)


def _build_payload(
    model: str,
    prompt: str,
    temperature: float,
    seed: int,
    num_predict: int,
    stream: bool = False,
) -> dict:
    return {
        'model': model,
        'prompt': prompt,
        'stream': stream,
        'options': {
            'temperature': temperature,
            'seed': seed,
            'num_predict': num_predict,
        },
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_json_from_text(text: str) -> Optional[dict]:
    """
    Extract the first JSON object from raw Ollama response text.

    Ollama models sometimes wrap the JSON in prose. Try strict parse first,
    then fall back to regex extraction of the first {...} block.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _parse_response(
    raw_text: str,
    task_type: str,
    request: LocalModelRequest,
    extraction_method: str,
    responded_at: str,
) -> Tuple[LocalModelResponse, Optional[str]]:
    """
    Parse raw Ollama text into a LocalModelResponse.

    Returns (response, parse_error). parse_error is None on success.
    On parse failure returns a minimal valid response so the caller can
    still record the run with the raw output preserved.
    """
    parsed = _extract_json_from_text(raw_text)
    parse_error: Optional[str] = None

    labels: List[SemanticLabel] = []
    entities: List[ExtractedEntity] = []
    claims: List[ExtractedClaim] = []
    relations: List[ExtractedRelation] = []
    summary: Optional[str] = None
    overall_confidence = 3

    if parsed is None:
        parse_error = f"No JSON object found in model output (len={len(raw_text)})"
    else:
        try:
            for lb in parsed.get('labels', []):
                if not isinstance(lb, dict):
                    continue
                labels.append(SemanticLabel(
                    label=_safe_str(lb.get('label', ''), 200) or 'unknown',
                    confidence=_clamp_confidence(lb.get('confidence', 3)),
                    rationale=_safe_str(lb.get('rationale', ''), 300) or None,
                ))

            for ent in parsed.get('entities', []):
                if not isinstance(ent, dict):
                    continue
                entities.append(ExtractedEntity(
                    text=_safe_str(ent.get('text', ''), 200) or 'unknown',
                    entity_type=_safe_str(ent.get('entity_type', 'unknown'), 100),
                    confidence=_clamp_confidence(ent.get('confidence', 3)),
                    rationale=_safe_str(ent.get('rationale', ''), 300) or None,
                ))

            for cl in parsed.get('claims', []):
                if not isinstance(cl, dict):
                    continue
                claims.append(ExtractedClaim(
                    text=_safe_str(cl.get('text', ''), 500) or 'unknown',
                    polarity=_normalise_polarity(cl.get('polarity')),
                    confidence=_clamp_confidence(cl.get('confidence', 3)),
                ))

            for rel in parsed.get('relations', []):
                if not isinstance(rel, dict):
                    continue
                relations.append(ExtractedRelation(
                    subject=_safe_str(rel.get('subject', ''), 200) or 'unknown',
                    predicate=_safe_str(rel.get('predicate', ''), 200) or 'unknown',
                    object_=_safe_str(rel.get('object', ''), 200) or 'unknown',
                    confidence=_clamp_confidence(rel.get('confidence', 3)),
                ))

            raw_summary = parsed.get('summary', '')
            if isinstance(raw_summary, str) and raw_summary.strip():
                summary = raw_summary[:500]

            if parsed.get('confidence') is not None:
                overall_confidence = _clamp_confidence(parsed.get('confidence'))
            elif labels:
                overall_confidence = _clamp_confidence(
                    sum(lb.confidence for lb in labels) // len(labels)
                )
            elif entities:
                overall_confidence = 2

        except Exception as exc:
            parse_error = f"Parse error: {exc}"
            labels, entities, claims, relations, summary = [], [], [], [], None
            overall_confidence = 1

    response = LocalModelResponse(
        request_id=request.request_id,
        model_name=request.model_name,
        model_version=request.model_version,
        task_type=task_type,
        extraction_method=extraction_method,
        overall_confidence=overall_confidence,
        responded_at=responded_at,
        labels=labels,
        entities=entities,
        claims=claims,
        relations=relations,
        summary=summary,
        metadata={},
    )
    return response, parse_error


# ---------------------------------------------------------------------------
# Ollama API helpers (tolerant — errors become None, not exceptions)
# ---------------------------------------------------------------------------

def _fetch_runtime_version(base_url: str, timeout: float) -> Optional[str]:
    """GET /api/version → str or None on any error."""
    if not _HAS_REQUESTS:
        return None
    try:
        resp = _requests.get(f"{base_url}/api/version", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('version')
    except Exception:
        pass
    return None


def _fetch_model_info(base_url: str, model: str, timeout: float) -> Tuple[Optional[str], Optional[str]]:
    """
    POST /api/show → (digest, family) or (None, None) on any error.
    """
    if not _HAS_REQUESTS:
        return None, None
    try:
        resp = _requests.post(
            f"{base_url}/api/show",
            json={'name': model},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            digest = data.get('digest') or data.get('model_info', {}).get('general.basename')
            family = (
                data.get('details', {}).get('family')
                or data.get('model_info', {}).get('general.basename')
            )
            return digest, family
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------------------
# OllamaAdapter
# ---------------------------------------------------------------------------

class OllamaAdapter(LocalModelAdapter):
    """
    LocalModelAdapter that calls a local Ollama instance via HTTP.

    Requires 'requests' to be installed. If not installed, raises
    ModelContractError at instantiation time (not at import time).

    Inference is provenance-preserved but NOT bitwise deterministic.
    temperature=0 and seed=42 defaults improve stability but are not
    guarantees. ModelCapability.deterministic_mode_supported is False.

    Canonical truth is the recorded artifact in the semantic ledger
    (raw_output_json, normalized_result_json), not re-queried model output.
    """

    NAME = OLLAMA_ADAPTER_NAME

    def __init__(
        self,
        model: str,
        base_url: str = OLLAMA_DEFAULT_BASE_URL,
        version: str = '1.0.0',
        timeout_seconds: float = OLLAMA_DEFAULT_TIMEOUT,
        num_predict: int = OLLAMA_DEFAULT_NUM_PREDICT,
        temperature: float = OLLAMA_DEFAULT_TEMPERATURE,
        seed: int = OLLAMA_DEFAULT_SEED,
    ) -> None:
        if not _HAS_REQUESTS:
            raise ModelContractError(_REQUESTS_INSTALL_MSG)
        if not model or not model.strip():
            raise ModelContractError("OllamaAdapter: model name must not be empty")
        if timeout_seconds <= 0:
            raise ModelContractError(
                f"OllamaAdapter: timeout_seconds must be > 0, got {timeout_seconds}"
            )

        self._model = model.strip()
        self._base_url = base_url.rstrip('/')
        self._version = version
        self._timeout = timeout_seconds
        self._num_predict = num_predict
        self._temperature = temperature
        self._seed = seed
        self._extraction_method = (
            f"{EXTRACTION_METHOD_LOCAL_MODEL_PREFIX}:ollama:{self._model}"
        )

        # Fetch runtime metadata at init — tolerant of failure
        self._runtime_version = _fetch_runtime_version(self._base_url, min(5.0, timeout_seconds))
        self._model_digest, self._model_family = _fetch_model_info(
            self._base_url, self._model, min(10.0, timeout_seconds)
        )

        # Pre-compute template hashes (stable across runs)
        self._template_hashes: Dict[str, str] = {
            tt: _template_hash(tt) for tt in SEMANTIC_TASK_TYPES
        }

    @property
    def adapter_name(self) -> str:
        return self.NAME

    @property
    def adapter_version(self) -> str:
        return self._version

    @property
    def capability_set(self) -> ModelCapabilitySet:
        caps = [
            ModelCapability(
                task_type=tt,
                max_input_chars=OLLAMA_MAX_INPUT_CHARS,
                deterministic_mode_supported=False,
                provenance_supported=True,
                confidence_supported=True,
            )
            for tt in SEMANTIC_TASK_TYPES
        ]
        return ModelCapabilitySet(
            adapter_name=self.NAME,
            adapter_version=self._version,
            capabilities=caps,
        )

    def execute(self, request: LocalModelRequest) -> LocalModelResponse:
        """
        POST to Ollama /api/generate and parse the structured response.

        Raw model output is captured in the returned response's metadata
        under 'raw_output' so callers can persist it to the ledger via
        record_run(raw_output=result.response.metadata['raw_output']).

        Raises ModelContractError on HTTP failure, timeout, or connection error.
        Never raises on parse failure — parse errors are recorded in metadata
        and a minimal valid response is returned.
        """
        from .contracts import validate_request
        validate_request(request)

        task_type = request.task_type
        prompt = _build_prompt(task_type, request.input_text)
        payload = _build_payload(
            model=self._model,
            prompt=prompt,
            temperature=self._temperature,
            seed=self._seed,
            num_predict=self._num_predict,
        )
        payload_hash = _sha16(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        input_hash = _sha16(request.input_text)

        start_ns = __import__('time').monotonic_ns()
        started_at = _now()

        try:
            http_resp = _requests.post(
                f"{self._base_url}/api/generate",
                json=payload,
                timeout=self._timeout,
            )
        except _requests.exceptions.Timeout:
            raise ModelContractError(
                f"OllamaAdapter: request timed out after {self._timeout}s "
                f"(model={self._model!r})"
            )
        except _requests.exceptions.ConnectionError as exc:
            raise ModelContractError(
                f"OllamaAdapter: connection error to {self._base_url!r}: {exc}"
            )
        except Exception as exc:
            raise ModelContractError(
                f"OllamaAdapter: unexpected HTTP error: {exc}"
            )

        responded_at = _now()
        duration_ms = (__import__('time').monotonic_ns() - start_ns) / 1_000_000

        if http_resp.status_code != 200:
            raise ModelContractError(
                f"OllamaAdapter: HTTP {http_resp.status_code} from Ollama "
                f"(model={self._model!r})"
            )

        try:
            ollama_payload = http_resp.json()
        except Exception:
            ollama_payload = {}

        raw_text = ollama_payload.get('response', '') or ''
        eval_count = ollama_payload.get('eval_count')
        eval_duration_ns = ollama_payload.get('eval_duration')

        response, parse_error = _parse_response(
            raw_text=raw_text,
            task_type=task_type,
            request=request,
            extraction_method=self._extraction_method,
            responded_at=responded_at,
        )

        # Build complete provenance metadata
        provenance = {
            'adapter_name': self.NAME,
            'adapter_version': self._version,
            'provider': 'ollama',
            'runtime_version': self._runtime_version,
            'model_name': self._model,
            'model_digest': self._model_digest,
            'model_family': self._model_family,
            'temperature': self._temperature,
            'seed': self._seed,
            'num_predict': self._num_predict,
            'prompt_template_hash': self._template_hashes[task_type],
            'request_payload_hash': payload_hash,
            'input_hash': input_hash,
            'started_at': started_at,
            'completed_at': responded_at,
            'duration_ms': duration_ms,
            'ollama_eval_count': eval_count,
            'ollama_eval_duration_ns': eval_duration_ns,
            'parse_error': parse_error,
        }

        # raw_output carried in metadata so caller can persist it
        raw_output = {
            'raw': raw_text,
            'model': ollama_payload.get('model', self._model),
            'done': ollama_payload.get('done', False),
            'eval_count': eval_count,
            'ollama_response_payload': ollama_payload,
        }

        response.metadata = {**provenance, 'raw_output': raw_output}
        return response
