import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from . import service
from .artifact_governance import ArtifactStatus
from .models import MemoryEvent, VALID_EVENT_TYPES, VALID_STATUSES

RETRIEVAL_SCORING_VERSION = '1.0.0'

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


def _canonical_query_dict(query: 'RetrievalQuery') -> dict:
    return {
        'tags': sorted(query.tags),
        'event_types': sorted(query.event_types),
        'statuses': sorted(query.statuses),
        'min_confidence': query.min_confidence,
        'limit': query.limit,
        'offset': query.offset,
        'expand_related': query.expand_related,
    }


def _query_hash(query_json: str) -> str:
    return hashlib.sha256(query_json.encode('utf-8')).hexdigest()[:16]


def _scoring_params_json() -> str:
    return json.dumps({'doctrine_priority': DOCTRINE_PRIORITY}, sort_keys=True, ensure_ascii=True)


@dataclass
class RetrievalLogEntry:
    id: int
    query_hash: str
    session_id: Optional[str]
    query_json: str
    scoring_version: str
    scoring_params_json: str
    result_event_ids_json: str
    result_count: int
    executed_at: str
    actor: str
    status: str

    @property
    def query(self) -> 'RetrievalQuery':
        d = json.loads(self.query_json)
        return RetrievalQuery(
            tags=d.get('tags', []),
            event_types=d.get('event_types', []),
            statuses=d.get('statuses', []),
            min_confidence=d.get('min_confidence', 1),
            limit=d.get('limit', 20),
            offset=d.get('offset', 0),
            expand_related=d.get('expand_related', True),
        )

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'query_hash': self.query_hash,
            'session_id': self.session_id,
            'query_json': self.query_json,
            'scoring_version': self.scoring_version,
            'scoring_params_json': self.scoring_params_json,
            'result_event_ids_json': self.result_event_ids_json,
            'result_count': self.result_count,
            'executed_at': self.executed_at,
            'actor': self.actor,
            'status': self.status,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'RetrievalLogEntry':
        return cls(
            id=row['id'],
            query_hash=row['query_hash'],
            session_id=row['session_id'],
            query_json=row['query_json'],
            scoring_version=row['scoring_version'],
            scoring_params_json=row['scoring_params_json'],
            result_event_ids_json=row['result_event_ids_json'],
            result_count=row['result_count'],
            executed_at=row['executed_at'],
            actor=row['actor'],
            status=row['status'],
        )


def retrieve(
    db_path: str,
    query: RetrievalQuery,
    *,
    log_retrieval: bool = False,
    log_db_path: Optional[str] = None,
    actor: str = 'system',
    session_id: Optional[str] = None,
) -> List[ScoredEvent]:
    candidates = _fetch_candidates(db_path, query)
    scored = _score_events(candidates, query.tags)

    if query.expand_related:
        scored = _expand_related(db_path, scored, query.tags)

    scored.sort(key=lambda s: s.composite_key)

    start = query.offset
    end = start + query.limit
    results = scored[start:end]

    if log_retrieval:
        _target = log_db_path if log_db_path is not None else db_path
        log_retrieval_query(_target, query, results, actor=actor, session_id=session_id)

    return results


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


# ---------------------------------------------------------------------------
# retrieval logging
# ---------------------------------------------------------------------------

def log_retrieval_query(
    db_path: str,
    query: RetrievalQuery,
    results: List[ScoredEvent],
    *,
    actor: str = 'system',
    session_id: Optional[str] = None,
) -> int:
    query_json = json.dumps(_canonical_query_dict(query), sort_keys=True, ensure_ascii=True)
    q_hash = _query_hash(query_json)
    result_ids_json = json.dumps([s.event.id for s in results])
    params_json = _scoring_params_json()
    executed_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        cur = conn.execute(
            'INSERT INTO retrieval_log'
            ' (query_hash, session_id, query_json, scoring_version, scoring_params_json,'
            '  result_event_ids_json, result_count, executed_at, actor, status)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
            (q_hash, session_id, query_json, RETRIEVAL_SCORING_VERSION, params_json,
             result_ids_json, len(results), executed_at, actor, ArtifactStatus.ACTIVE),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_retrieval_log(db_path: str, log_id: int) -> RetrievalLogEntry:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute('SELECT * FROM retrieval_log WHERE id = ?', (log_id,)).fetchone()
        if row is None:
            raise KeyError(f"Retrieval log entry {log_id} not found")
        return RetrievalLogEntry.from_row(row)
    finally:
        conn.close()


def list_retrieval_log(
    db_path: str,
    *,
    limit: int = 50,
    session_id: Optional[str] = None,
) -> List[RetrievalLogEntry]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if session_id is not None:
            rows = conn.execute(
                'SELECT * FROM retrieval_log WHERE session_id = ? ORDER BY id DESC LIMIT ?',
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM retrieval_log ORDER BY id DESC LIMIT ?',
                (limit,),
            ).fetchall()
        return [RetrievalLogEntry.from_row(r) for r in rows]
    finally:
        conn.close()
