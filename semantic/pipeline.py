"""
Semantic execution pipeline.

Connects the semantic contract layer (SemanticTask, SemanticExtractionResult)
with the model adapter layer (LocalModelAdapter, execute_with_policy) and
the ingestion layer (CandidateMemoryEvent).

Entry points:
  run_semantic_task()         — run one semantic task via an adapter
  enrich_chunks_with_semantic()— semantic enrichment for an ingestion pass

Design:
  - No database writes at any point.
  - Results are candidates only (status='proposed', committed_id=None).
  - All output is deterministically serializable.
  - Validation happens at every layer boundary.

Candidate generation:
  A CandidateMemoryEvent is generated from a SemanticExtractionResult when:
    - generate_candidates=True (default)
    - the result contains at least one label, entity, claim, or summary

  Empty results (no content at all) produce no candidates.
  The caller decides event_type; it must be an EXTRACTABLE_EVENT_TYPE.
"""
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from semantic.contracts import make_task, result_to_candidate
from semantic.models import (
    SemanticExtractionResult,
    SemanticSpan,
    SemanticTask,
    _now,
)
from semantic.validators import SemanticValidationError

from models.adapters import LocalModelAdapter
from models.contracts import (
    ModelExecutionPolicy,
    ModelExecutionResult,
    task_to_request,
)
from models.execution import execute_with_policy

if TYPE_CHECKING:
    from ingestion.models import CandidateMemoryEvent, Chunk

# Default event_type used when generating candidates from semantic results.
# Must be an EXTRACTABLE_EVENT_TYPE.
SEMANTIC_DEFAULT_EVENT_TYPE = 'hypothesis'
SEMANTIC_ENRICHMENT_CREATED_BY = 'semantic-enrichment'
SEMANTIC_PIPELINE_CREATED_BY = 'semantic-pipeline'


# ---------------------------------------------------------------------------
# SemanticPipelineResult
# ---------------------------------------------------------------------------

@dataclass
class SemanticPipelineResult:
    """
    Complete output of one run_semantic_task() call.

    task              — the SemanticTask that was executed
    execution_result  — full ModelExecutionResult with timing metadata
    semantic_result   — validated SemanticExtractionResult (None on failure)
    candidates        — CandidateMemoryEvent list (proposed, not committed)
    success           — True if execution and conversion succeeded
    error             — error message (None on success)
    """
    task: SemanticTask
    execution_result: ModelExecutionResult
    semantic_result: Optional[SemanticExtractionResult] = None
    candidates: List['CandidateMemoryEvent'] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'task': self.task.to_dict(),
            'execution': self.execution_result.to_dict(),
            'semantic_result': self.semantic_result.to_dict() if self.semantic_result else None,
            'candidates': [c.to_dict() for c in self.candidates],
            'success': self.success,
            'error': self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    def to_markdown(self) -> str:
        er = self.execution_result
        lines = [
            '# Semantic Extraction Result',
            '',
            f'**Task ID:** `{self.task.task_id}`',
            f'**Task Type:** `{self.task.task_type}`',
            f'**Adapter:** {er.adapter_name} v{er.adapter_version}',
        ]
        preview = self.task.input_text[:100]
        if len(self.task.input_text) > 100:
            preview += '...'
        lines += [f'**Input preview:** {preview}', '']

        lines += [
            '---',
            '',
            '## Execution',
            '',
            f'- **success:** {str(self.success).lower()}',
            f'- **duration_ms:** {er.duration_ms:.3f}',
            f'- **retry_count:** {er.retry_count}',
            f'- **timeout_applied:** {str(er.timeout_applied).lower()}',
            '',
        ]

        if self.error:
            lines += ['## Error', '', '```', self.error, '```', '']

        sr = self.semantic_result
        if sr:
            if sr.labels:
                lines += ['## Labels', '']
                for lb in sr.labels:
                    rationale = f' — {lb.rationale}' if lb.rationale else ''
                    lines.append(f'- `{lb.label}` (confidence: {lb.confidence}){rationale}')
                lines.append('')

            if sr.entities:
                lines += ['## Entities', '']
                for e in sr.entities:
                    lines.append(
                        f'- `{e.text}` [{e.entity_type}] (confidence: {e.confidence})'
                    )
                lines.append('')

            if sr.claims:
                lines += ['## Claims', '']
                for c in sr.claims:
                    lines.append(
                        f'- `{c.text}` [{c.polarity}] (confidence: {c.confidence})'
                    )
                lines.append('')

            if sr.relations:
                lines += ['## Relations', '']
                for r in sr.relations:
                    lines.append(f'- `{r.subject}` — `{r.predicate}` — `{r.object_}`')
                lines.append('')

            if sr.summary:
                lines += ['## Summary', '', sr.summary, '']
        else:
            lines += ['*(no semantic result)*', '']

        if self.candidates:
            lines += [f'## Candidates ({len(self.candidates)})', '']
            for c in self.candidates:
                lines.append(
                    f'- [{c.event_type}] {c.title} (confidence: {c.confidence})'
                )
            lines.append('')

        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Candidate generation helper
# ---------------------------------------------------------------------------

def _result_has_content(sr: SemanticExtractionResult) -> bool:
    return bool(sr.labels or sr.entities or sr.claims or sr.relations or sr.summary)


def _derive_title(sr: SemanticExtractionResult, task_type: str) -> str:
    if sr.summary:
        return sr.summary[:80]
    if sr.claims:
        return sr.claims[0].text[:80]
    if sr.labels:
        labels_str = ', '.join(lb.label for lb in sr.labels[:3])
        return f'{task_type}: {labels_str}'
    return f'Semantic {task_type} result'


def _try_generate_candidates(
    sr: SemanticExtractionResult,
    task: SemanticTask,
    event_type: str,
    title: Optional[str],
    created_by: str,
) -> List['CandidateMemoryEvent']:
    if not _result_has_content(sr):
        return []
    resolved_title = title or _derive_title(sr, task.task_type)
    try:
        candidate = result_to_candidate(
            sr, task,
            event_type=event_type,
            title=resolved_title,
            created_by=created_by,
        )
        return [candidate]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# run_semantic_task
# ---------------------------------------------------------------------------

def run_semantic_task(
    task_type: str,
    input_text: str,
    adapter: LocalModelAdapter,
    source_id: Optional[str] = None,
    source_span: Optional[SemanticSpan] = None,
    policy: Optional[ModelExecutionPolicy] = None,
    event_type: str = SEMANTIC_DEFAULT_EVENT_TYPE,
    title: Optional[str] = None,
    created_by: str = SEMANTIC_PIPELINE_CREATED_BY,
    generate_candidates: bool = True,
) -> SemanticPipelineResult:
    """
    Execute one semantic task via an adapter and return a pipeline result.

    Steps:
      1. Build a validated SemanticTask.
      2. Convert to LocalModelRequest.
      3. Execute via execute_with_policy (captures timing metadata).
      4. Extract SemanticExtractionResult from execution result.
      5. Optionally generate CandidateMemoryEvent candidates.

    No database writes. Returns SemanticPipelineResult regardless of success.

    Raises:
        SemanticValidationError — task_type is invalid or input_text is empty.
        (Other errors are captured in SemanticPipelineResult.error.)
    """
    # Step 1: build task — let SemanticValidationError propagate (caller handles)
    task = make_task(
        task_type,
        input_text,
        source_id=source_id,
        source_span=source_span,
    )

    # Step 2-4: build request and execute
    req = task_to_request(task, adapter.adapter_name, adapter.adapter_version)
    exec_result = execute_with_policy(adapter, req, policy=policy, task=task)

    sr = exec_result.semantic_result  # None on failure
    candidates: List['CandidateMemoryEvent'] = []

    if generate_candidates and sr is not None:
        candidates = _try_generate_candidates(sr, task, event_type, title, created_by)

    return SemanticPipelineResult(
        task=task,
        execution_result=exec_result,
        semantic_result=sr,
        candidates=candidates,
        success=exec_result.success,
        error=exec_result.error,
    )


# ---------------------------------------------------------------------------
# enrich_chunks_with_semantic
# ---------------------------------------------------------------------------

def enrich_chunks_with_semantic(
    chunks: List['Chunk'],
    adapter: LocalModelAdapter,
    task_type: str = 'memory_candidate_classification',
    policy: Optional[ModelExecutionPolicy] = None,
    event_type: str = SEMANTIC_DEFAULT_EVENT_TYPE,
    created_by: str = SEMANTIC_ENRICHMENT_CREATED_BY,
) -> List['SemanticPipelineResult']:
    """
    Run semantic extraction on each chunk and return one SemanticPipelineResult
    per chunk (skipping chunks with invalid/empty text).

    Callers that need only the flat candidate list:
        results = enrich_chunks_with_semantic(chunks, adapter)
        candidates = [c for r in results for c in r.candidates]

    No database writes. All candidates are status='proposed'.
    """
    results: List[SemanticPipelineResult] = []

    for chunk in chunks:
        try:
            pipeline_result = run_semantic_task(
                task_type=task_type,
                input_text=chunk.text,
                adapter=adapter,
                source_id=chunk.source_id,
                policy=policy,
                event_type=event_type,
                created_by=created_by,
                generate_candidates=True,
            )
            results.append(pipeline_result)
        except SemanticValidationError:
            # Skip chunks with invalid/empty text
            continue

    return results
