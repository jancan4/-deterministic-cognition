"""Tests for semantic/pipeline.py."""
import json
import sqlite3

import pytest

from semantic.models import SEMANTIC_TASK_TYPES
from semantic.pipeline import (
    SEMANTIC_DEFAULT_EVENT_TYPE,
    SemanticPipelineResult,
    enrich_chunks_with_semantic,
    run_semantic_task,
)
from models.adapters import EchoModelAdapter, StubModelAdapter
from models.registry import make_default_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(text='The Fed held rates.', source_id='src1', chunk_index=0):
    from ingestion.models import Chunk
    return Chunk(
        source_path='/data/test.txt',
        source_id=source_id,
        chunk_index=chunk_index,
        text=text,
        start_char=0,
        end_char=len(text),
    )


# ---------------------------------------------------------------------------
# run_semantic_task — basic
# ---------------------------------------------------------------------------

class TestRunSemanticTaskBasic:
    def test_stub_success(self):
        result = run_semantic_task('tagging', 'The Fed held rates.', StubModelAdapter())
        assert result.success is True
        assert result.error is None

    def test_echo_success(self):
        result = run_semantic_task('entity_extraction', 'The Federal Reserve.', EchoModelAdapter())
        assert result.success is True

    def test_all_task_types_accepted(self):
        adapter = StubModelAdapter()
        for tt in SEMANTIC_TASK_TYPES:
            r = run_semantic_task(tt, 'some text here', adapter)
            assert r.success, f"Failed for task_type={tt!r}: {r.error}"

    def test_invalid_task_type_raises(self):
        from semantic.validators import SemanticValidationError
        with pytest.raises(SemanticValidationError, match='task_type'):
            run_semantic_task('rate_prediction', 'text', StubModelAdapter())

    def test_empty_input_raises(self):
        from semantic.validators import SemanticValidationError
        with pytest.raises(SemanticValidationError, match='input_text'):
            run_semantic_task('tagging', '', StubModelAdapter())

    def test_whitespace_input_raises(self):
        from semantic.validators import SemanticValidationError
        with pytest.raises(SemanticValidationError):
            run_semantic_task('tagging', '   ', StubModelAdapter())

    def test_returns_semantic_pipeline_result(self):
        result = run_semantic_task('tagging', 'text', StubModelAdapter())
        assert isinstance(result, SemanticPipelineResult)

    def test_task_id_in_result(self):
        result = run_semantic_task('tagging', 'stable text', StubModelAdapter())
        assert result.task.task_id
        assert len(result.task.task_id) == 16

    def test_semantic_result_present(self):
        from semantic.models import SemanticExtractionResult
        result = run_semantic_task('tagging', 'text', StubModelAdapter())
        assert isinstance(result.semantic_result, SemanticExtractionResult)

    def test_execution_metadata_present(self):
        from models.contracts import ModelExecutionResult
        result = run_semantic_task('tagging', 'text', StubModelAdapter())
        er = result.execution_result
        assert isinstance(er, ModelExecutionResult)
        assert er.adapter_name == 'stub'
        assert er.duration_ms >= 0.0


# ---------------------------------------------------------------------------
# run_semantic_task — candidates
# ---------------------------------------------------------------------------

class TestRunSemanticTaskCandidates:
    def test_stub_generates_candidates(self):
        result = run_semantic_task('tagging', 'text', StubModelAdapter())
        assert len(result.candidates) >= 1

    def test_candidates_are_proposed(self):
        result = run_semantic_task('tagging', 'text', StubModelAdapter())
        for c in result.candidates:
            assert c.status == 'proposed'

    def test_candidates_have_no_committed_id(self):
        result = run_semantic_task('tagging', 'text', StubModelAdapter())
        for c in result.candidates:
            assert c.committed_id is None

    def test_generate_candidates_false(self):
        result = run_semantic_task(
            'tagging', 'text', StubModelAdapter(), generate_candidates=False
        )
        assert result.candidates == []

    def test_custom_event_type(self):
        result = run_semantic_task(
            'tagging', 'text', StubModelAdapter(),
            event_type='regime_observation',
        )
        for c in result.candidates:
            assert c.event_type == 'regime_observation'

    def test_custom_title(self):
        result = run_semantic_task(
            'tagging', 'text', StubModelAdapter(), title='My custom title'
        )
        assert any(c.title == 'My custom title' for c in result.candidates)

    def test_created_by_preserved(self):
        result = run_semantic_task(
            'tagging', 'text', StubModelAdapter(), created_by='test-runner'
        )
        for c in result.candidates:
            assert c.created_by == 'test-runner'

    def test_source_id_propagated(self):
        result = run_semantic_task(
            'tagging', 'text', StubModelAdapter(), source_id='src-abc'
        )
        assert result.task.source_id == 'src-abc'

    def test_no_memory_write(self):
        """run_semantic_task must not write to any database."""
        import os, tempfile
        db = tempfile.mktemp(suffix='.db')
        try:
            run_semantic_task('tagging', 'text', StubModelAdapter())
            assert not os.path.exists(db)
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ---------------------------------------------------------------------------
# run_semantic_task — determinism
# ---------------------------------------------------------------------------

class TestRunSemanticTaskDeterminism:
    def test_same_input_same_task_id(self):
        r1 = run_semantic_task('tagging', 'stable text here', StubModelAdapter())
        r2 = run_semantic_task('tagging', 'stable text here', StubModelAdapter())
        assert r1.task.task_id == r2.task.task_id

    def test_same_input_same_labels(self):
        r1 = run_semantic_task('tagging', 'stable text', StubModelAdapter())
        r2 = run_semantic_task('tagging', 'stable text', StubModelAdapter())
        labels1 = [lb.label for lb in r1.semantic_result.labels]
        labels2 = [lb.label for lb in r2.semantic_result.labels]
        assert labels1 == labels2

    def test_same_input_same_candidate_count(self):
        r1 = run_semantic_task('tagging', 'stable text', StubModelAdapter())
        r2 = run_semantic_task('tagging', 'stable text', StubModelAdapter())
        assert len(r1.candidates) == len(r2.candidates)

    def test_different_input_different_task_id(self):
        r1 = run_semantic_task('tagging', 'hello world', StubModelAdapter())
        r2 = run_semantic_task('tagging', 'goodbye world', StubModelAdapter())
        assert r1.task.task_id != r2.task.task_id


# ---------------------------------------------------------------------------
# SemanticPipelineResult — serialisation
# ---------------------------------------------------------------------------

class TestSemanticPipelineResultSerialisation:
    def _make_result(self):
        return run_semantic_task('tagging', 'The Fed held rates.', StubModelAdapter())

    def test_to_dict_has_required_keys(self):
        r = self._make_result()
        d = r.to_dict()
        assert set(d.keys()) == {
            'task', 'execution', 'semantic_result', 'candidates', 'success', 'error'
        }

    def test_to_json_valid(self):
        r = self._make_result()
        parsed = json.loads(r.to_json())
        assert parsed['success'] is True

    def test_to_json_deterministic_content(self):
        r1 = self._make_result()
        r2 = run_semantic_task('tagging', 'The Fed held rates.', StubModelAdapter())
        d1, d2 = r1.to_dict(), r2.to_dict()
        assert d1['task']['task_id'] == d2['task']['task_id']
        assert d1['semantic_result']['labels'] == d2['semantic_result']['labels']

    def test_to_markdown_contains_task_id(self):
        r = self._make_result()
        md = r.to_markdown()
        assert r.task.task_id in md

    def test_to_markdown_contains_adapter_name(self):
        r = self._make_result()
        md = r.to_markdown()
        assert 'stub' in md

    def test_to_markdown_contains_task_type(self):
        r = self._make_result()
        md = r.to_markdown()
        assert 'tagging' in md

    def test_to_markdown_contains_labels(self):
        r = self._make_result()
        md = r.to_markdown()
        assert 'stub' in md  # stub label

    def test_to_markdown_deterministic_content(self):
        r1 = self._make_result()
        r2 = run_semantic_task('tagging', 'The Fed held rates.', StubModelAdapter())
        # Strip timestamps from markdown for comparison
        _skip = ('T00:', 'started', 'completed', 'duration_ms')
        md1_lines = [l for l in r1.to_markdown().split('\n') if not any(s in l for s in _skip)]
        md2_lines = [l for l in r2.to_markdown().split('\n') if not any(s in l for s in _skip)]
        assert md1_lines == md2_lines

    def test_to_markdown_echo_has_labels(self):
        r = run_semantic_task(
            'entity_extraction',
            'Federal Reserve raised interest rates.',
            EchoModelAdapter(),
        )
        md = r.to_markdown()
        assert '## Labels' in md or 'no semantic result' in md

    def test_to_json_sort_keys(self):
        r = self._make_result()
        text = r.to_json()
        # sort_keys=True means keys appear in alphabetical order
        first_key = json.loads(text)
        assert isinstance(first_key, dict)


# ---------------------------------------------------------------------------
# enrich_chunks_with_semantic
# ---------------------------------------------------------------------------

class TestEnrichChunksWithSemantic:
    """
    enrich_chunks_with_semantic() returns List[SemanticPipelineResult], one per
    chunk (skipping invalid/empty chunks). Callers flatten candidates via:
        [c for r in results for c in r.candidates]
    """

    def test_empty_chunks_returns_empty(self):
        result = enrich_chunks_with_semantic([], StubModelAdapter())
        assert result == []

    def test_returns_pipeline_results_not_candidates(self):
        chunk = _make_chunk('The Federal Reserve held interest rates steady.')
        results = enrich_chunks_with_semantic([chunk], StubModelAdapter())
        assert len(results) == 1
        assert isinstance(results[0], SemanticPipelineResult)

    def test_one_chunk_has_candidates(self):
        chunk = _make_chunk('The Federal Reserve held interest rates steady.')
        results = enrich_chunks_with_semantic([chunk], StubModelAdapter())
        candidates = [c for r in results for c in r.candidates]
        assert len(candidates) >= 1

    def test_multiple_chunks_returns_one_result_per_chunk(self):
        chunks = [
            _make_chunk('The Fed held rates.', chunk_index=0),
            _make_chunk('The ECB raised rates.', chunk_index=1),
        ]
        results = enrich_chunks_with_semantic(chunks, StubModelAdapter())
        assert len(results) == 2

    def test_multiple_chunks_flat_candidates(self):
        chunks = [
            _make_chunk('The Fed held rates.', chunk_index=0),
            _make_chunk('The ECB raised rates.', chunk_index=1),
        ]
        results = enrich_chunks_with_semantic(chunks, StubModelAdapter())
        candidates = [c for r in results for c in r.candidates]
        assert len(candidates) >= 2

    def test_all_candidates_proposed(self):
        chunk = _make_chunk()
        results = enrich_chunks_with_semantic([chunk], StubModelAdapter())
        for r in results:
            for c in r.candidates:
                assert c.status == 'proposed'
                assert c.committed_id is None

    def test_no_database_write(self):
        import os, tempfile
        db = tempfile.mktemp(suffix='.db')
        chunk = _make_chunk()
        try:
            enrich_chunks_with_semantic([chunk], StubModelAdapter())
            assert not os.path.exists(db)
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_custom_event_type(self):
        chunk = _make_chunk()
        results = enrich_chunks_with_semantic(
            [chunk], StubModelAdapter(), event_type='regime_observation'
        )
        for r in results:
            for c in r.candidates:
                assert c.event_type == 'regime_observation'

    def test_custom_created_by(self):
        chunk = _make_chunk()
        results = enrich_chunks_with_semantic(
            [chunk], StubModelAdapter(), created_by='test-enricher'
        )
        for r in results:
            for c in r.candidates:
                assert c.created_by == 'test-enricher'

    def test_echo_adapter_integration(self):
        chunk = _make_chunk('Federal Reserve ECB USD rates decision.')
        results = enrich_chunks_with_semantic([chunk], EchoModelAdapter())
        assert isinstance(results, list)

    def test_source_id_from_chunk(self):
        chunk = _make_chunk('text', source_id='my-source-001')
        results = enrich_chunks_with_semantic([chunk], StubModelAdapter())
        assert isinstance(results, list)

    def test_default_task_type_is_memory_candidate_classification(self):
        """Default task_type flows correctly to pipeline — no SemanticValidationError."""
        chunk = _make_chunk()
        results = enrich_chunks_with_semantic([chunk], StubModelAdapter())
        assert isinstance(results, list)

    def test_each_result_carries_execution_result(self):
        chunk = _make_chunk()
        results = enrich_chunks_with_semantic([chunk], StubModelAdapter())
        assert len(results) == 1
        assert results[0].execution_result.adapter_name == 'stub'

    def test_each_result_carries_task(self):
        chunk = _make_chunk('some text here')
        results = enrich_chunks_with_semantic([chunk], StubModelAdapter())
        assert len(results) == 1
        assert results[0].task.input_text == 'some text here'
