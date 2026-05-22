"""
Rule-based extraction engine: maps Chunks to CandidateMemoryEvents.

Three rule families:
  1. KeywordRule   — triggers on exact word/phrase presence (case-insensitive)
  2. PatternRule   — triggers on a compiled regex match
  3. HeuristicRule — triggers on structural document signals (sentence length,
                     marker phrases, numbered lists, etc.)

Each rule maps to one of the 12 EXTRACTABLE_EVENT_TYPES and emits a
CandidateMemoryEvent with source attribution carried from the Chunk.

Extraction is:
  - Deterministic: same chunk → same candidates (no randomness, no LLM)
  - Local: no network calls, no filesystem writes
  - Attributing: every candidate carries a SourceSpan with character offsets
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .models import (
    Chunk,
    CandidateMemoryEvent,
    SourceSpan,
    EXTRACTION_METHOD_KEYWORD,
    EXTRACTION_METHOD_PATTERN,
    EXTRACTION_METHOD_HEURISTIC,
    EXTRACTABLE_EVENT_TYPES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_sentence(text: str, max_chars: int = 120) -> str:
    """Return the first sentence (up to max_chars) of text."""
    for end_char in ('.', '!', '?', '\n'):
        idx = text.find(end_char)
        if 0 < idx < max_chars:
            return text[:idx + 1].strip()
    return text[:max_chars].strip()


def _title_from_text(text: str, prefix: str = '', max_chars: int = 80) -> str:
    sentence = _first_sentence(text, max_chars=max_chars)
    if prefix:
        # Avoid double prefix if the sentence already starts with the prefix
        if not sentence.lower().startswith(prefix.lower()):
            sentence = f"{prefix}: {sentence}"
    return sentence[:max_chars]


def _span_from_match(m: re.Match, chunk: Chunk) -> SourceSpan:
    return SourceSpan(
        start=chunk.start_char + m.start(),
        end=chunk.start_char + m.end(),
        text=m.group(0),
    )


def _span_from_chunk(chunk: Chunk) -> SourceSpan:
    return SourceSpan(
        start=chunk.start_char,
        end=chunk.end_char,
        text=chunk.text,
    )


# ---------------------------------------------------------------------------
# Rule base classes
# ---------------------------------------------------------------------------

@dataclass
class KeywordRule:
    """
    Fire when any of the keywords are found (whole-word, case-insensitive).
    Emits one candidate per chunk (not per keyword match).
    """
    keywords: Tuple[str, ...]
    event_type: str
    confidence: int
    status: str = 'proposed'
    tag_hint: str = ''

    def __post_init__(self) -> None:
        assert self.event_type in EXTRACTABLE_EVENT_TYPES
        assert 1 <= self.confidence <= 5
        self._patterns = tuple(
            re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
            for kw in self.keywords
        )

    def match(self, chunk: Chunk) -> Optional[CandidateMemoryEvent]:
        for pat in self._patterns:
            m = pat.search(chunk.text)
            if m:
                span = _span_from_match(m, chunk)
                title = _title_from_text(chunk.text, prefix=self.event_type.replace('_', ' ').title())
                tags = [self.tag_hint] if self.tag_hint else []
                return CandidateMemoryEvent(
                    event_type=self.event_type,
                    title=title,
                    summary=chunk.text[:500],
                    evidence=chunk.text if len(chunk.text) <= 1000 else chunk.text[:1000] + '…',
                    source=chunk.source_path,
                    confidence=self.confidence,
                    status=self.status,
                    tags=tags,
                    created_by='ingestion-pipeline',
                    source_span=span,
                    extraction_method=EXTRACTION_METHOD_KEYWORD,
                )
        return None


@dataclass
class PatternRule:
    """
    Fire when a regex pattern matches the chunk text.
    The first capturing group (if present) is used for the title.
    """
    pattern: re.Pattern
    event_type: str
    confidence: int
    title_prefix: str = ''
    status: str = 'proposed'
    tag_hint: str = ''

    def __post_init__(self) -> None:
        assert self.event_type in EXTRACTABLE_EVENT_TYPES
        assert 1 <= self.confidence <= 5

    def match(self, chunk: Chunk) -> Optional[CandidateMemoryEvent]:
        m = self.pattern.search(chunk.text)
        if not m:
            return None
        # Use first capturing group as title seed if available
        captured = m.group(1).strip() if m.lastindex and m.group(1) else chunk.text
        title = _title_from_text(captured, prefix=self.title_prefix)
        span = _span_from_match(m, chunk)
        tags = [self.tag_hint] if self.tag_hint else []
        return CandidateMemoryEvent(
            event_type=self.event_type,
            title=title,
            summary=chunk.text[:500],
            evidence=chunk.text if len(chunk.text) <= 1000 else chunk.text[:1000] + '…',
            source=chunk.source_path,
            confidence=self.confidence,
            status=self.status,
            tags=tags,
            created_by='ingestion-pipeline',
            source_span=span,
            extraction_method=EXTRACTION_METHOD_PATTERN,
        )


@dataclass
class HeuristicRule:
    """
    Fire based on structural signals: sentence count, word density,
    numbered lists, emphasis markers, question marks, etc.
    """
    event_type: str
    confidence: int
    detector_fn: object  # Callable[[str], bool]
    title_prefix: str = ''
    status: str = 'proposed'
    tag_hint: str = ''

    def __post_init__(self) -> None:
        assert self.event_type in EXTRACTABLE_EVENT_TYPES
        assert 1 <= self.confidence <= 5

    def match(self, chunk: Chunk) -> Optional[CandidateMemoryEvent]:
        if not self.detector_fn(chunk.text):
            return None
        title = _title_from_text(chunk.text, prefix=self.title_prefix)
        tags = [self.tag_hint] if self.tag_hint else []
        return CandidateMemoryEvent(
            event_type=self.event_type,
            title=title,
            summary=chunk.text[:500],
            evidence=chunk.text if len(chunk.text) <= 1000 else chunk.text[:1000] + '…',
            source=chunk.source_path,
            confidence=self.confidence,
            status=self.status,
            tags=tags,
            created_by='ingestion-pipeline',
            source_span=_span_from_chunk(chunk),
            extraction_method=EXTRACTION_METHOD_HEURISTIC,
        )


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------

def _build_rules() -> List:
    rules: List = []

    # --- open_question ---
    rules.append(KeywordRule(
        keywords=('?',),
        event_type='open_question',
        confidence=2,
        tag_hint='question',
    ))
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:open\s+question|unresolved\s+question|to\s+be\s+determined'
            r'|tbd|tbh|unclear|need\s+to\s+decide)[:\s]+([^\n]{5,120})',
            re.IGNORECASE,
        ),
        event_type='open_question',
        confidence=3,
        title_prefix='Open Question',
        status='unresolved',
        tag_hint='question',
    ))

    # --- architecture_decision ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:decided\s+to|decision[:\s]+|we\s+will\s+use|chose\s+to\s+use'
            r'|architecture\s+decision|adr\s*[\-:#]?\s*)([^\n]{5,120})',
            re.IGNORECASE,
        ),
        event_type='architecture_decision',
        confidence=3,
        title_prefix='Decision',
        tag_hint='architecture',
    ))
    rules.append(KeywordRule(
        keywords=('ADR', 'architecture decision record'),
        event_type='architecture_decision',
        confidence=4,
        tag_hint='architecture',
    ))

    # --- governance_rule ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:must\s+not|must\s+always|never\s+(?:allow|use|deploy)'
            r'|governance\s+rule|policy[:\s]+|rule[:\s]+|constraint[:\s]+)'
            r'([^\n]{5,120})',
            re.IGNORECASE,
        ),
        event_type='governance_rule',
        confidence=3,
        title_prefix='Governance Rule',
        tag_hint='governance',
    ))
    rules.append(KeywordRule(
        keywords=('no live capital', 'risk veto', 'human approval required',
                  'quant validation required'),
        event_type='governance_rule',
        confidence=4,
        status='proposed',
        tag_hint='governance',
    ))

    # --- hypothesis ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:hypothesis[:\s]+|we\s+hypothes(?:ize|ise)|posit\s+that'
            r'|conjecture|assume\s+that)([^\n]{5,120})',
            re.IGNORECASE,
        ),
        event_type='hypothesis',
        confidence=3,
        title_prefix='Hypothesis',
        tag_hint='research',
    ))
    rules.append(KeywordRule(
        keywords=('if this holds', 'we believe that', 'our assumption is'),
        event_type='hypothesis',
        confidence=2,
        tag_hint='research',
    ))

    # --- experiment ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:experiment[:\s]+|we\s+tested|we\s+ran|a/b\s+test|pilot\s+study'
            r'|trial\s+run|backt(?:est|ested))([^\n]{0,120})',
            re.IGNORECASE,
        ),
        event_type='experiment',
        confidence=3,
        title_prefix='Experiment',
        tag_hint='experiment',
    ))

    # --- validation_result ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:result[s]?[:\s]+|validated|confirmed|disproved|failed\s+to\s+confirm'
            r'|passed\s+validation|sharpe\s+ratio|drawdown|pnl|p&l)([^\n]{0,120})',
            re.IGNORECASE,
        ),
        event_type='validation_result',
        confidence=3,
        title_prefix='Validation Result',
        tag_hint='validation',
    ))

    # --- adaptation ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:adapted\s+to|adjusted\s+(?:for|due\s+to)|regime\s+change'
            r'|parameter\s+update|recalibrat(?:ed|ion)|strategy\s+modified)'
            r'([^\n]{0,120})',
            re.IGNORECASE,
        ),
        event_type='adaptation',
        confidence=3,
        title_prefix='Adaptation',
        tag_hint='regime',
    ))

    # --- regime_observation ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:regime[:\s]+|market\s+regime|volatility\s+regime'
            r'|risk[- ]off|risk[- ]on|macro\s+environment|fed\s+pivot'
            r'|hawkish|dovish|stagflat)([^\n]{0,120})',
            re.IGNORECASE,
        ),
        event_type='regime_observation',
        confidence=2,
        title_prefix='Regime Observation',
        tag_hint='macro',
    ))
    rules.append(KeywordRule(
        keywords=('risk-off', 'risk-on', 'macro regime', 'vol regime'),
        event_type='regime_observation',
        confidence=3,
        tag_hint='macro',
    ))

    # --- implementation_note ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:note[:\s]+|implementation\s+note|technical\s+note|caveat[:\s]+'
            r'|important[:\s]+|warning[:\s]+|todo[:\s]+)([^\n]{5,120})',
            re.IGNORECASE,
        ),
        event_type='implementation_note',
        confidence=2,
        title_prefix='Note',
        tag_hint='implementation',
    ))

    # --- rejected_idea ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:rejected\s+because|decided\s+against|not\s+using|abandoned'
            r'|ruled\s+out|won\'t\s+(?:use|do|implement)|we\s+chose\s+not\s+to)'
            r'([^\n]{0,120})',
            re.IGNORECASE,
        ),
        event_type='rejected_idea',
        confidence=3,
        title_prefix='Rejected Idea',
        tag_hint='decision',
    ))

    # --- incident ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:incident[:\s]+|post[- ]mortem|outage|bug\s+report'
            r'|production\s+issue|root\s+cause|rca[:\s]+|failure\s+mode)'
            r'([^\n]{0,120})',
            re.IGNORECASE,
        ),
        event_type='incident',
        confidence=3,
        title_prefix='Incident',
        status='unresolved',
        tag_hint='incident',
    ))

    # --- source_reference ---
    rules.append(PatternRule(
        pattern=re.compile(
            r'(?:see[:\s]+|ref(?:erence)?[:\s]+|source[:\s]+'
            r'|https?://\S+|doi:\s*[\w./]+|arxiv\.org/abs/\S+)'
            r'([^\n]{0,120})',
            re.IGNORECASE,
        ),
        event_type='source_reference',
        confidence=2,
        title_prefix='Reference',
        tag_hint='reference',
    ))

    # --- heuristic: open_question on trailing question marks ---
    def _ends_with_question(text: str) -> bool:
        stripped = text.rstrip()
        return stripped.endswith('?') and len(stripped) > 20

    rules.append(HeuristicRule(
        event_type='open_question',
        confidence=2,
        detector_fn=_ends_with_question,
        title_prefix='Open Question',
        status='unresolved',
        tag_hint='question',
    ))

    # --- heuristic: hypothesis on "if ... then" structure ---
    _IF_THEN_RE = re.compile(r'\bif\b.{5,80}\bthen\b', re.IGNORECASE | re.DOTALL)

    def _has_if_then(text: str) -> bool:
        return bool(_IF_THEN_RE.search(text))

    rules.append(HeuristicRule(
        event_type='hypothesis',
        confidence=2,
        detector_fn=_has_if_then,
        title_prefix='Hypothesis',
        tag_hint='research',
    ))

    return rules


_RULES: List = _build_rules()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_from_chunk(chunk: Chunk) -> List[CandidateMemoryEvent]:
    """
    Apply all extraction rules to a single chunk.

    Returns a list of CandidateMemoryEvent objects (may be empty).
    Rules are applied in registration order; each rule produces at most one
    candidate per chunk. Duplicates (same event_type from multiple rules on
    the same chunk) are preserved — deduplication is done by candidates.py.
    """
    candidates: List[CandidateMemoryEvent] = []
    for rule in _RULES:
        try:
            cand = rule.match(chunk)
            if cand is not None:
                candidates.append(cand)
        except Exception:
            # A broken rule must not abort extraction of the whole chunk
            pass
    return candidates


def extract_from_chunks(chunks: List[Chunk]) -> List[CandidateMemoryEvent]:
    """
    Apply extraction rules to every chunk in order.

    Returns a flat list of all candidates across all chunks,
    preserving document chunk order.
    """
    all_candidates: List[CandidateMemoryEvent] = []
    for chunk in chunks:
        all_candidates.extend(extract_from_chunk(chunk))
    return all_candidates
