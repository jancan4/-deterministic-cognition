from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .models import MemoryEvent
from .retrieval import ScoredEvent, DOCTRINE_PRIORITY

_SECTION_ORDER = [
    'GOVERNANCE CONTEXT',
    'ARCHITECTURE CONTEXT',
    'ACTIVE QUESTIONS',
    'RECENT ADAPTATIONS',
    'RELATED EXPERIMENTS',
    'RELEVANT MEMORY EVENTS',
]

_TYPE_TO_SECTION: Dict[str, str] = {
    'governance_rule': 'GOVERNANCE CONTEXT',
    'architecture_decision': 'ARCHITECTURE CONTEXT',
    'open_question': 'ACTIVE QUESTIONS',
    'adaptation': 'RECENT ADAPTATIONS',
    'experiment': 'RELATED EXPERIMENTS',
    'hypothesis': 'RELATED EXPERIMENTS',
}


@dataclass
class ContextEntry:
    event_id: int
    event_type: str
    title: str
    summary: str
    confidence: int
    status: str
    tags: List[str]
    evidence: Optional[str]
    is_expanded: bool

    def char_count(self) -> int:
        return len(self._render())

    def _render(self) -> str:
        parts = [
            f"[{self.event_id}] {self.event_type.upper()} | confidence={self.confidence} | status={self.status}",
            f"  Title   : {self.title}",
            f"  Summary : {self.summary}",
        ]
        if self.evidence:
            parts.append(f"  Evidence: {self.evidence}")
        if self.tags:
            parts.append(f"  Tags    : {', '.join(self.tags)}")
        if self.is_expanded:
            parts.append("  [related]")
        return '\n'.join(parts)


@dataclass
class ContextSection:
    name: str
    entries: List[ContextEntry] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.entries

    def char_count(self) -> int:
        if not self.entries:
            return 0
        header = f"## {self.name}\n\n"
        body = '\n\n'.join(e._render() for e in self.entries)
        return len(header) + len(body) + 2

    def render(self) -> str:
        if not self.entries:
            return ''
        header = f"## {self.name}\n\n"
        body = '\n\n'.join(e._render() for e in self.entries)
        return header + body + '\n'


@dataclass
class AssembledContext:
    sections: List[ContextSection]
    total_events: int
    included_events: int
    char_budget: int
    chars_used: int

    def to_text(self) -> str:
        parts = []
        for section in self.sections:
            if not section.is_empty():
                parts.append(section.render())
        return '\n'.join(parts)

    def to_dict(self) -> dict:
        return {
            'char_budget': self.char_budget,
            'chars_used': self.chars_used,
            'total_events': self.total_events,
            'included_events': self.included_events,
            'sections': [
                {
                    'name': s.name,
                    'entry_count': len(s.entries),
                    'entries': [
                        {
                            'event_id': e.event_id,
                            'event_type': e.event_type,
                            'title': e.title,
                            'summary': e.summary,
                            'confidence': e.confidence,
                            'status': e.status,
                            'tags': e.tags,
                            'evidence': e.evidence,
                            'is_expanded': e.is_expanded,
                        }
                        for e in s.entries
                    ],
                }
                for s in self.sections
            ],
        }


def _assign_section(event: MemoryEvent) -> str:
    return _TYPE_TO_SECTION.get(event.event_type, 'RELEVANT MEMORY EVENTS')


def _make_entry(scored: ScoredEvent) -> ContextEntry:
    ev = scored.event
    return ContextEntry(
        event_id=ev.id,
        event_type=ev.event_type,
        title=ev.title,
        summary=ev.summary,
        confidence=ev.confidence,
        status=ev.status,
        tags=ev.tags,
        evidence=ev.evidence,
        is_expanded=scored.is_expanded,
    )


def build_context(
    events: List[ScoredEvent],
    char_budget: int = 8000,
    include_sections: Optional[List[str]] = None,
) -> AssembledContext:
    allowed: Set[str] = set(include_sections) if include_sections else set(_SECTION_ORDER)

    sections: Dict[str, ContextSection] = {
        name: ContextSection(name=name)
        for name in _SECTION_ORDER
        if name in allowed
    }

    chars_used = 0
    included = 0

    for scored in events:
        section_name = _assign_section(scored.event)
        if section_name not in sections:
            continue

        entry = _make_entry(scored)
        entry_chars = entry.char_count() + 4  # separator overhead

        if chars_used + entry_chars > char_budget:
            continue

        sections[section_name].entries.append(entry)
        chars_used += entry_chars
        included += 1

    ordered = [sections[name] for name in _SECTION_ORDER if name in sections]

    return AssembledContext(
        sections=ordered,
        total_events=len(events),
        included_events=included,
        char_budget=char_budget,
        chars_used=chars_used,
    )
