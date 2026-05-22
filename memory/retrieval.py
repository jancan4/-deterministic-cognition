from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import service
from .models import MemoryEvent, VALID_EVENT_TYPES, VALID_STATUSES

# Lower rank = higher priority. Types not listed default to 7.
DOCTRINE_PRIORITY: Dict[str, int] = {
    'governance_rule': 1,
    'architecture_decision': 2,
    'validation_result': 3,
    'adaptation': 4,
    'hypothesis': 5,
    'implementation_note': 6,
}

_DEFAULT_DOCTRINE_RANK = 7


@dataclass
class RetrievalQuery:
    tags: List[str] = field(default_factory=list)
    event_types: List[str] = field(default_factory=list)
    statuses: List[str] = field(default_factory=list)
    min_confidence: int = 1
    limit: int = 20
    offset: int = 0
    expand_related: bool = True


@dataclass
class ScoredEvent:
    event: MemoryEvent
    tag_overlap: int
    recency_rank: int
    is_expanded: bool

    @property
    def composite_key(self) -> Tuple:
        doctrine_rank = DOCTRINE_PRIORITY.get(self.event.event_type, _DEFAULT_DOCTRINE_RANK)
        return (
            int(self.is_expanded),
            doctrine_rank,
            -self.event.confidence,
            self.recency_rank,
            -self.tag_overlap,
            self.event.id,
        )


def retrieve(db_path: str, query: RetrievalQuery) -> List[ScoredEvent]:
    candidates = _fetch_candidates(db_path, query)
    scored = _score_events(candidates, query.tags)

    if query.expand_related:
        scored = _expand_related(db_path, scored, query.tags)

    scored.sort(key=lambda s: s.composite_key)

    start = query.offset
    end = start + query.limit
    return scored[start:end]


def retrieve_unresolved(db_path: str, limit: int = 20) -> List[ScoredEvent]:
    query = RetrievalQuery(
        statuses=['unresolved', 'proposed'],
        limit=limit,
        expand_related=False,
    )
    return retrieve(db_path, query)


def retrieve_adaptations(db_path: str, tags: Optional[List[str]] = None, limit: int = 20) -> List[ScoredEvent]:
    query = RetrievalQuery(
        event_types=['adaptation'],
        tags=tags or [],
        limit=limit,
        expand_related=False,
    )
    return retrieve(db_path, query)


def retrieve_governance(db_path: str, limit: int = 20) -> List[ScoredEvent]:
    query = RetrievalQuery(
        event_types=['governance_rule', 'architecture_decision'],
        limit=limit,
        expand_related=False,
    )
    return retrieve(db_path, query)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _fetch_candidates(db_path: str, query: RetrievalQuery) -> List[MemoryEvent]:
    import sqlite3

    clauses: List[str] = []
    params: list = []

    if query.event_types:
        placeholders = ','.join('?' * len(query.event_types))
        clauses.append(f'event_type IN ({placeholders})')
        params.extend(query.event_types)

    if query.statuses:
        placeholders = ','.join('?' * len(query.statuses))
        clauses.append(f'status IN ({placeholders})')
        params.extend(query.statuses)

    if query.min_confidence > 1:
        clauses.append('confidence >= ?')
        params.append(query.min_confidence)

    if query.tags:
        tag_clauses = []
        for tag in query.tags:
            tag_clauses.append("EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value = ?)")
            params.append(tag)
        clauses.append(f'({" OR ".join(tag_clauses)})')

    where = f'WHERE {" AND ".join(clauses)}' if clauses else ''

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        rows = conn.execute(
            f'SELECT * FROM memory_events {where} ORDER BY id ASC',
            params,
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]
    finally:
        conn.close()


def _assign_recency_ranks(events: List[MemoryEvent]) -> Dict[int, int]:
    # Primary: updated_at desc. Secondary: id desc (higher id = later insertion) for same-second ties.
    sorted_by_time = sorted(events, key=lambda e: (e.updated_at, e.id), reverse=True)
    return {e.id: rank for rank, e in enumerate(sorted_by_time)}


def _score_events(
    events: List[MemoryEvent],
    query_tags: List[str],
    is_expanded: bool = False,
) -> List[ScoredEvent]:
    tag_set: Set[str] = set(query_tags)
    recency = _assign_recency_ranks(events)
    scored: List[ScoredEvent] = []
    for ev in events:
        overlap = len(set(ev.tags) & tag_set)
        scored.append(ScoredEvent(
            event=ev,
            tag_overlap=overlap,
            recency_rank=recency[ev.id],
            is_expanded=is_expanded,
        ))
    return scored


def _expand_related(
    db_path: str,
    primary: List[ScoredEvent],
    query_tags: List[str],
) -> List[ScoredEvent]:
    import sqlite3

    seen_ids: Set[int] = {s.event.id for s in primary}
    related_ids_to_fetch: Set[int] = set()

    for scored in primary:
        for rid in scored.event.related_ids:
            if rid not in seen_ids:
                related_ids_to_fetch.add(rid)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        link_rows = conn.execute(
            'SELECT source_id, target_id FROM memory_links WHERE source_id IN ({ids}) OR target_id IN ({ids})'.format(
                ids=','.join(str(i) for i in seen_ids) if seen_ids else '0'
            )
        ).fetchall()
        for row in link_rows:
            for mid in (row['source_id'], row['target_id']):
                if mid not in seen_ids:
                    related_ids_to_fetch.add(mid)

        if not related_ids_to_fetch:
            return primary

        placeholders = ','.join('?' * len(related_ids_to_fetch))
        rows = conn.execute(
            f'SELECT * FROM memory_events WHERE id IN ({placeholders}) ORDER BY id ASC',
            list(related_ids_to_fetch),
        ).fetchall()
        related_events = [MemoryEvent.from_row(r) for r in rows]
    finally:
        conn.close()

    expanded = _score_events(related_events, query_tags, is_expanded=True)
    return primary + expanded
