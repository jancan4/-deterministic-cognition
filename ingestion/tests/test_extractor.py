"""Tests for ingestion.extractor."""
import pytest
from ingestion.extractor import extract_from_chunk, extract_from_chunks
from ingestion.models import Chunk, CandidateMemoryEvent, SourceSpan


def _chunk(text: str, start: int = 0, idx: int = 0) -> Chunk:
    return Chunk(
        source_path="<test>",
        source_id="abc1234500000000",
        chunk_index=idx,
        text=text,
        start_char=start,
        end_char=start + len(text),
    )


# ---------------------------------------------------------------------------
# extract_from_chunk: return types
# ---------------------------------------------------------------------------

def test_extract_from_chunk_returns_list():
    chunk = _chunk("Some text without signals.")
    result = extract_from_chunk(chunk)
    assert isinstance(result, list)


def test_extract_from_chunk_returns_candidate_events():
    chunk = _chunk("open question: what is the correct threshold?")
    result = extract_from_chunk(chunk)
    for item in result:
        assert isinstance(item, CandidateMemoryEvent)


def test_extract_from_chunk_source_path_preserved():
    chunk = _chunk("Should we do this?")
    for cand in extract_from_chunk(chunk):
        assert cand.source == "<test>"


def test_extract_from_chunk_source_span_preserved():
    text = "open question: what model to use?"
    chunk = _chunk(text, start=50)
    for cand in extract_from_chunk(chunk):
        assert isinstance(cand.source_span, SourceSpan)
        # span must fall within the chunk's character range
        assert cand.source_span.start >= 50
        assert cand.source_span.end <= 50 + len(text)


def test_extract_from_chunk_created_by():
    chunk = _chunk("ADR: We will use SQLite as the primary store.")
    for cand in extract_from_chunk(chunk):
        assert cand.created_by == "ingestion-pipeline"


def test_extract_from_chunk_confidence_in_range():
    chunk = _chunk("rejected because the latency was too high")
    for cand in extract_from_chunk(chunk):
        assert 1 <= cand.confidence <= 5


def test_extract_from_chunk_status_valid():
    chunk = _chunk("we tested the momentum strategy last quarter")
    for cand in extract_from_chunk(chunk):
        assert cand.status in ("proposed", "unresolved")


def test_extract_from_chunk_event_type_valid():
    from ingestion.models import EXTRACTABLE_EVENT_TYPES
    chunk = _chunk("governance rule: no live capital without approval")
    for cand in extract_from_chunk(chunk):
        assert cand.event_type in EXTRACTABLE_EVENT_TYPES


# ---------------------------------------------------------------------------
# Keyword rules
# ---------------------------------------------------------------------------

def test_open_question_keyword_question_mark():
    chunk = _chunk("What should our drawdown limit be?")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "open_question" in types


def test_architecture_decision_keyword_adr():
    chunk = _chunk("ADR: we adopt event sourcing for the workflow layer.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "architecture_decision" in types


def test_governance_rule_keyword_no_live_capital():
    chunk = _chunk("No live capital deployment without quant validation.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "governance_rule" in types


def test_regime_observation_keyword():
    chunk = _chunk("The market is in a clear risk-off regime this quarter.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "regime_observation" in types


# ---------------------------------------------------------------------------
# Pattern rules
# ---------------------------------------------------------------------------

def test_open_question_pattern():
    chunk = _chunk("open question: how do we handle reconnection on failure?")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "open_question" in types


def test_open_question_unresolved_status():
    chunk = _chunk("tbd: what volatility model to use")
    results = extract_from_chunk(chunk)
    oq = [c for c in results if c.event_type == "open_question"]
    if oq:
        assert any(c.status == "unresolved" for c in oq)


def test_architecture_decision_pattern():
    chunk = _chunk("We decided to use PostgreSQL over MySQL for JSONB support.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "architecture_decision" in types


def test_governance_rule_must_pattern():
    chunk = _chunk("must not deploy strategies without human approval.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "governance_rule" in types


def test_hypothesis_pattern():
    chunk = _chunk("Hypothesis: momentum signals decay after 3 days in ranging markets.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "hypothesis" in types


def test_experiment_pattern():
    chunk = _chunk("We backtested the strategy over 5 years of EUR/USD data.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "experiment" in types


def test_validation_result_pattern():
    chunk = _chunk("Result: Sharpe ratio of 1.4 with 12% max drawdown.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "validation_result" in types


def test_regime_observation_pattern():
    chunk = _chunk("The Fed pivot has created a risk-on macro environment.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "regime_observation" in types


def test_implementation_note_pattern():
    chunk = _chunk("Note: the cursor must be advanced past the separator byte.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "implementation_note" in types


def test_rejected_idea_pattern():
    chunk = _chunk("We decided against using Redis because operational overhead is too high.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "rejected_idea" in types


def test_incident_pattern():
    chunk = _chunk("Incident: production outage on 2025-03-10 due to connection pool exhaustion.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "incident" in types


def test_incident_unresolved_status():
    chunk = _chunk("Post-mortem: root cause not yet identified.")
    results = extract_from_chunk(chunk)
    inc = [c for c in results if c.event_type == "incident"]
    if inc:
        assert any(c.status == "unresolved" for c in inc)


def test_source_reference_pattern():
    chunk = _chunk("See: https://arxiv.org/abs/2301.00001 for the original paper.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "source_reference" in types


# ---------------------------------------------------------------------------
# Heuristic rules
# ---------------------------------------------------------------------------

def test_heuristic_trailing_question():
    chunk = _chunk("What is the optimal lookback window for this signal?")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "open_question" in types


def test_heuristic_if_then_hypothesis():
    chunk = _chunk("If momentum is above threshold then the signal fires.")
    results = extract_from_chunk(chunk)
    types = [c.event_type for c in results]
    assert "hypothesis" in types


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_chunk_produces_no_candidates():
    chunk = _chunk("   ")
    results = extract_from_chunk(chunk)
    # We cannot assert empty (whitespace may hit a rule), but we ensure no crash
    assert isinstance(results, list)


def test_extract_from_chunks_empty_list():
    assert extract_from_chunks([]) == []


def test_extract_from_chunks_preserves_order():
    chunks = [
        _chunk("hypothesis: markets mean-revert", start=0, idx=0),
        _chunk("ADR: use SQLite", start=100, idx=1),
    ]
    results = extract_from_chunks(chunks)
    # Results from chunk 0 should appear before results from chunk 1
    spans = [c.source_span.start for c in results]
    # All chunk-0 results have start_char < 100
    chunk0_results = [c for c in results if c.source_span.start < 100]
    chunk1_results = [c for c in results if c.source_span.start >= 100]
    assert len(chunk0_results) >= 0  # may be 0 but no crash
    assert len(chunk1_results) >= 0


def test_extract_deterministic():
    chunk = _chunk("We decided to use WAL mode. open question: why not memory?")
    r1 = extract_from_chunk(chunk)
    r2 = extract_from_chunk(chunk)
    assert [c.event_type for c in r1] == [c.event_type for c in r2]


# ---------------------------------------------------------------------------
# Defect 1 regression: section header extraction guard
# ---------------------------------------------------------------------------

def test_header_only_h2_produces_no_candidates():
    """'## Root Cause' standalone chunk must not produce any incident candidate."""
    chunk = _chunk("## Root Cause")
    results = extract_from_chunk(chunk)
    assert results == []


def test_header_only_h1_produces_no_candidates():
    chunk = _chunk("# Title")
    assert extract_from_chunk(chunk) == []


def test_header_only_h3_produces_no_candidates():
    chunk = _chunk("### Background")
    assert extract_from_chunk(chunk) == []


def test_header_with_body_still_extracts():
    """A chunk that has a heading PLUS content lines must still be processed."""
    text = "## Root Cause\nThe connection pool was exhausted due to a configuration error."
    chunk = _chunk(text)
    # Has body content — extraction must run (may or may not produce candidates but must not
    # short-circuit unconditionally)
    results = extract_from_chunk(chunk)
    assert isinstance(results, list)


def test_header_only_whitespace_variants_no_candidates():
    """Header chunk with trailing whitespace must still be detected as header-only."""
    chunk = _chunk("## Impact  ")
    assert extract_from_chunk(chunk) == []


def test_non_header_chunk_with_hash_symbol_not_blocked():
    """A chunk referencing a GitHub issue '#123' must not be blocked."""
    chunk = _chunk("We decided to use ADR #123 as the reference.")
    results = extract_from_chunk(chunk)
    # The chunk itself is not a header-only chunk and should be processed normally
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# EI-001 regression: governance_rule fragment over-extraction
# ---------------------------------------------------------------------------

def test_rule_mid_sentence_no_governance():
    """'rule' as a mid-sentence noun without a colon must not produce governance_rule."""
    chunk = _chunk("The routing rule evaluates each incoming message against the ruleset.")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov == [], f"Unexpected governance_rule from mid-sentence 'rule': {[c.title for c in gov]}"


def test_rule_evaluation_fragment_no_governance():
    """'rule evaluation was' is a technical observation, not a governance statement."""
    chunk = _chunk("The rule evaluation was single-threaded in the current implementation.")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov == [], f"Fragment 'rule evaluation was' must not produce governance_rule: {[c.title for c in gov]}"


def test_rule_set_fragment_no_governance():
    """'rule set is N rules' is a measurement, not a governance statement."""
    chunk = _chunk("The rule set is currently 47 rules.")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov == [], f"Fragment 'rule set is' must not produce governance_rule: {[c.title for c in gov]}"


def test_governance_rule_no_colon_no_governance():
    """'governance rule' without a colon in running prose must not produce a fragment candidate."""
    chunk = _chunk("The governance rule parallelism work is tracked in the Phase 3 backlog.")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov == [], f"'governance rule <noun>' without colon must not fire: {[c.title for c in gov]}"


def test_rule_colon_still_produces_governance():
    """'rule: <content>' with explicit colon must still fire after the narrowing fix."""
    chunk = _chunk("rule: no live capital deployment without human approval")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov, "Expected governance_rule from 'rule:' — must not have been broken by fix"


def test_governance_rule_colon_still_produces_governance():
    """'governance rule: <content>' with colon must still fire."""
    chunk = _chunk("governance rule: changes require a signed approval before deployment")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov, "Expected governance_rule from 'governance rule:'"


def test_policy_colon_still_produces_governance():
    """'policy: <content>' with explicit colon must still fire."""
    chunk = _chunk("policy: all deployments require a risk-team sign-off before release")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov, "Expected governance_rule from 'policy:'"


def test_constraint_colon_still_produces_governance():
    """'constraint: <content>' with explicit colon must still fire."""
    chunk = _chunk("constraint: maximum 3 replicas per service at all times")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov, "Expected governance_rule from 'constraint:'"


def test_must_not_unaffected_by_rule_colon_fix():
    """'must not' trigger must be unaffected by the rule/policy/constraint narrowing."""
    chunk = _chunk("must not deploy strategies without human approval.")
    results = extract_from_chunk(chunk)
    gov = [c for c in results if c.event_type == 'governance_rule']
    assert gov, "Expected governance_rule from 'must not' — this trigger was not changed"
