import argparse
import json
import sys
from typing import List, Optional

from . import export as exporter
from . import service
from .models import (
    VALID_EVENT_TYPES, VALID_RELATIONSHIPS, VALID_STATUSES,
    CONFIDENCE_MIN, CONFIDENCE_MAX,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _print_event(ev) -> None:
    tags = ', '.join(ev.tags) if ev.tags else '—'
    print(f"[{ev.id}] {ev.event_type} | {ev.status} | confidence={ev.confidence} | v{ev.version}")
    print(f"  Title   : {ev.title}")
    print(f"  Summary : {ev.summary[:100]}")
    print(f"  Source  : {ev.source}")
    if ev.evidence:
        print(f"  Evidence: {ev.evidence[:100]}")
    print(f"  Tags    : {tags}")
    print(f"  By      : {ev.created_by}  Created: {ev.created_at}")
    print()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    service.init_db(args.db)
    print(f"Initialized memory database: {args.db}")


def cmd_add(args: argparse.Namespace) -> None:
    tags: List[str] = [t.strip() for t in args.tags.split(',')] if args.tags else []
    related_ids: List[int] = []
    if args.related_ids:
        try:
            related_ids = [int(x.strip()) for x in args.related_ids.split(',')]
        except ValueError:
            _die("--related-ids must be comma-separated integers")

    try:
        ev = service.add_memory_event(
            db_path=args.db,
            event_type=args.type,
            title=args.title,
            summary=args.summary,
            source=args.source,
            confidence=args.confidence,
            status=args.status,
            created_by=args.created_by,
            evidence=args.evidence,
            tags=tags,
            related_ids=related_ids,
        )
    except service.ValidationError as exc:
        _die(str(exc))
    print(json.dumps(ev.to_dict(), indent=2, sort_keys=True))


def cmd_list(args: argparse.Namespace) -> None:
    try:
        events = service.list_memory_events(
            db_path=args.db,
            event_type=args.type,
            status=args.status,
            tag=args.tag,
        )
    except service.ValidationError as exc:
        _die(str(exc))
    for ev in events:
        _print_event(ev)
    if not events:
        print("No memory events found.")


def cmd_search(args: argparse.Namespace) -> None:
    if not args.query and not args.tag:
        _die("At least one of --query or --tag is required")
    try:
        events = service.search_memory_events(
            db_path=args.db,
            query=args.query,
            tag=args.tag,
        )
    except service.ValidationError as exc:
        _die(str(exc))
    for ev in events:
        _print_event(ev)
    if not events:
        print("No results.")


def cmd_show(args: argparse.Namespace) -> None:
    try:
        ev, revisions, links = service.get_memory_event(args.db, args.id)
    except service.NotFoundError as exc:
        _die(str(exc))
    print(json.dumps(
        {
            'event': ev.to_dict(),
            'revision_count': len(revisions),
            'revisions': [r.to_dict() for r in revisions],
            'links': [lnk.to_dict() for lnk in links],
        },
        indent=2,
        sort_keys=True,
    ))


def cmd_update_status(args: argparse.Namespace) -> None:
    try:
        ev = service.update_status(
            db_path=args.db,
            memory_id=args.id,
            new_status=args.status,
            reason=args.reason,
            created_by=args.created_by,
        )
    except (service.ValidationError, service.NotFoundError) as exc:
        _die(str(exc))
    print(json.dumps(ev.to_dict(), indent=2, sort_keys=True))


def cmd_link(args: argparse.Namespace) -> None:
    try:
        lnk = service.link_memory_events(
            db_path=args.db,
            source_id=args.source_id,
            target_id=args.target_id,
            relationship=args.relationship,
        )
    except (service.ValidationError, service.NotFoundError) as exc:
        _die(str(exc))
    print(json.dumps(lnk.to_dict(), indent=2, sort_keys=True))


def cmd_export(args: argparse.Namespace) -> None:
    try:
        payload = exporter.export_to_file(args.db, args.out)
    except Exception as exc:
        _die(str(exc))
    n = len(payload['memory_events'])
    print(f"Exported {n} event(s) to {args.out}")


def cmd_promote_embedding(args: argparse.Namespace) -> None:
    from .embeddings import promote_embedding
    from .artifact_governance import GovernanceInvalidationError, GovernancePinError, GovernanceSchemaError
    try:
        row = promote_embedding(
            db_path=args.db,
            embedding_id=args.id,
            reason=args.reason,
            operator=args.operator,
        )
    except (GovernancePinError, GovernanceInvalidationError, GovernanceSchemaError, ValueError) as exc:
        _die(str(exc))
    print(json.dumps(row.to_dict(), indent=2, sort_keys=True))


def cmd_embed_event(args: argparse.Namespace) -> None:
    from .embeddings import embed_event
    from .embedding_pins import get_active_pin
    from .service import NotFoundError, get_memory_event
    try:
        event, _, _ = get_memory_event(args.db, args.id)
    except NotFoundError as exc:
        _die(str(exc))

    pin = get_active_pin(args.db, pin_scope=args.pin_scope)
    if pin is None:
        _die(
            f"No active pin for scope={args.pin_scope!r}. "
            f"Create one with 'pin-embedding-model' first."
        )

    adapter = _adapter_from_pin(pin)
    embedding_id = embed_event(
        args.db,
        event,
        adapter,
        pin_scope=args.pin_scope,
        actor=args.actor,
    )
    print(json.dumps({'embedding_id': embedding_id}, indent=2))


def cmd_pin_embedding_model(args: argparse.Namespace) -> None:
    from .embedding_pins import create_pin
    model_digest = args.model_digest if args.model_digest else None
    pin = create_pin(
        args.db,
        adapter_name=args.adapter_name,
        adapter_version=args.adapter_version,
        model_name=args.model_name,
        model_digest=model_digest,
        dimensions=args.dimensions,
        provider_name=args.provider_name,
        pinned_by=args.pinned_by,
        pin_scope=args.pin_scope,
        notes=args.notes,
    )
    print(json.dumps(pin.to_dict(), indent=2, sort_keys=True))


def cmd_retrieve(args: argparse.Namespace) -> None:
    from .retrieval import RetrievalQuery, retrieve

    query_vector = None
    query_vector_provenance = None

    if args.query_vector_json:
        raw = args.query_vector_json.strip()
        if raw.startswith('@'):
            try:
                with open(raw[1:]) as fh:
                    raw = fh.read()
            except OSError as exc:
                _die(str(exc))
        try:
            query_vector = json.loads(raw)
        except json.JSONDecodeError as exc:
            _die(f"Invalid --query-vector-json: {exc}")
        if not isinstance(query_vector, list):
            _die("--query-vector-json must be a JSON array of floats")
        if args.query_vector_provenance:
            try:
                query_vector_provenance = json.loads(args.query_vector_provenance)
            except json.JSONDecodeError as exc:
                _die(f"Invalid --query-vector-provenance: {exc}")

    tags: List[str] = [t.strip() for t in args.tags.split(',')] if args.tags else []

    q = RetrievalQuery(
        tags=tags,
        limit=args.limit,
        expand_related=not args.no_expand,
    )

    results = retrieve(
        args.db, q,
        query_vector=query_vector,
        query_vector_provenance=query_vector_provenance,
        log_retrieval=args.log_retrieval,
        actor=args.actor,
    )

    output = [
        {
            'event_id': s.event.id,
            'title': s.event.title,
            'event_type': s.event.event_type,
            'confidence': s.event.confidence,
            'semantic_rank': s.semantic_rank,
            'recency_rank': s.recency_rank,
            'tag_overlap': s.tag_overlap,
            'is_expanded': s.is_expanded,
        }
        for s in results
    ]
    print(json.dumps(output, indent=2))


def cmd_get_active_pin(args: argparse.Namespace) -> None:
    from .embedding_pins import get_active_pin
    pin = get_active_pin(args.db, pin_scope=args.pin_scope)
    if pin is None:
        print(json.dumps({'active_pin': None, 'pin_scope': args.pin_scope}, indent=2))
    else:
        print(json.dumps(pin.to_dict(), indent=2, sort_keys=True))


def _adapter_from_pin(pin) -> object:
    """Instantiate the appropriate EmbeddingAdapter from a PinRecord."""
    if pin.provider_name == 'stub':
        from models.embedding_adapter import StubEmbeddingAdapter
        return StubEmbeddingAdapter(dimensions=pin.dimensions)
    if pin.provider_name == 'ollama':
        from models.ollama_embedding_adapter import OllamaEmbeddingAdapter
        return OllamaEmbeddingAdapter(
            pin.model_name,
            expected_dimensions=pin.dimensions,
        )
    _die(f"Unknown provider_name={pin.provider_name!r}. Supported: 'stub', 'ollama'.")


def cmd_review(args: argparse.Namespace) -> None:
    try:
        events = service.review_memory(
            db_path=args.db,
            status=args.status,
            event_type=args.type,
        )
    except service.ValidationError as exc:
        _die(str(exc))
    if not events:
        print("No memory events pending review.")
        return
    label = 'event' if len(events) == 1 else 'events'
    print(f"{len(events)} memory {label} pending review:\n")
    for ev in events:
        _print_event(ev)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

_COMMANDS = {
    'init': cmd_init,
    'add': cmd_add,
    'list': cmd_list,
    'search': cmd_search,
    'show': cmd_show,
    'update-status': cmd_update_status,
    'link': cmd_link,
    'export': cmd_export,
    'review': cmd_review,
    'promote-embedding': cmd_promote_embedding,
    'embed-event': cmd_embed_event,
    'pin-embedding-model': cmd_pin_embedding_model,
    'get-active-pin': cmd_get_active_pin,
    'retrieve': cmd_retrieve,
}


def build_parser() -> argparse.ArgumentParser:
    # Shared --db argument inherited by every subparser so that the
    # brief's command style  "python3 -m memory.cli <cmd> --db PATH"  works.
    _db = argparse.ArgumentParser(add_help=False)
    _db.add_argument('--db', required=True, metavar='PATH',
                     help='SQLite database path')

    parser = argparse.ArgumentParser(
        prog='python3 -m memory.cli',
        description='Deterministic SQLite-backed external memory layer',
    )

    sub = parser.add_subparsers(dest='command', required=True)

    # init
    sub.add_parser('init', parents=[_db], help='Initialise the database schema')

    # add
    p_add = sub.add_parser('add', parents=[_db], help='Add a memory event')
    p_add.add_argument('--type', dest='type', required=True,
                       choices=list(VALID_EVENT_TYPES), help='Event type')
    p_add.add_argument('--title', required=True, help='Short title')
    p_add.add_argument('--summary', required=True, help='Summary of the memory')
    p_add.add_argument('--source', required=True, help='Source reference')
    p_add.add_argument('--confidence', required=True, type=int,
                       choices=list(range(CONFIDENCE_MIN, CONFIDENCE_MAX + 1)),
                       help='Confidence 1 (weak) – 5 (authoritative)')
    p_add.add_argument('--status', required=True,
                       choices=list(VALID_STATUSES), help='Initial status')
    p_add.add_argument('--created-by', required=True, dest='created_by',
                       help='Author identifier')
    p_add.add_argument('--evidence', help='Supporting evidence (optional)')
    p_add.add_argument('--tags', help='Comma-separated tags (optional)')
    p_add.add_argument('--related-ids', dest='related_ids',
                       help='Comma-separated related event IDs (optional)')

    # list
    p_list = sub.add_parser('list', parents=[_db], help='List memory events')
    p_list.add_argument('--type', dest='type', choices=list(VALID_EVENT_TYPES),
                        help='Filter by event type')
    p_list.add_argument('--status', choices=list(VALID_STATUSES),
                        help='Filter by status')
    p_list.add_argument('--tag', help='Filter by tag')

    # search
    p_search = sub.add_parser('search', parents=[_db],
                               help='Search title, summary, evidence, source, tags')
    p_search.add_argument('--query', help='Text query')
    p_search.add_argument('--tag', help='Filter by tag')

    # show
    p_show = sub.add_parser('show', parents=[_db], help='Show one memory event by id')
    p_show.add_argument('--id', required=True, type=int, help='Memory event id')

    # update-status
    p_upd = sub.add_parser('update-status', parents=[_db],
                            help='Update the status of a memory event')
    p_upd.add_argument('--id', required=True, type=int, help='Memory event id')
    p_upd.add_argument('--status', required=True, choices=list(VALID_STATUSES),
                       help='New status')
    p_upd.add_argument('--reason', required=True, help='Reason for change')
    p_upd.add_argument('--created-by', required=True, dest='created_by',
                       help='Author identifier')

    # link
    p_link = sub.add_parser('link', parents=[_db],
                             help='Create a relationship between two memory events')
    p_link.add_argument('--source-id', required=True, type=int, dest='source_id',
                        help='Source event id')
    p_link.add_argument('--target-id', required=True, type=int, dest='target_id',
                        help='Target event id')
    p_link.add_argument('--relationship', required=True,
                        choices=list(VALID_RELATIONSHIPS), help='Relationship type')

    # export
    p_exp = sub.add_parser('export', parents=[_db],
                            help='Export deterministic JSON snapshot')
    p_exp.add_argument('--out', required=True, metavar='FILE', help='Output file path')

    # review
    p_rev = sub.add_parser('review', parents=[_db],
                            help='Show memory events needing review')
    p_rev.add_argument('--status', choices=list(VALID_STATUSES),
                       help='Filter by status (default: proposed/unresolved/active)')
    p_rev.add_argument('--type', dest='type', choices=list(VALID_EVENT_TYPES),
                       help='Filter by event type')

    # promote-embedding
    p_promo = sub.add_parser('promote-embedding', parents=[_db],
                              help='Promote a candidate embedding to active')
    p_promo.add_argument('--id', required=True, type=int, help='Embedding id')
    p_promo.add_argument('--reason', required=True, help='Reason for promotion')
    p_promo.add_argument('--operator', required=True, help='Operator identifier')

    # embed-event
    p_embed = sub.add_parser('embed-event', parents=[_db],
                              help='Generate and persist a candidate embedding for a memory event')
    p_embed.add_argument('--id', required=True, type=int, help='Memory event id')
    p_embed.add_argument('--pin-scope', dest='pin_scope', default='global',
                         help='Pin scope to resolve adapter from (default: global)')
    p_embed.add_argument('--actor', default='operator',
                         help='Actor identifier for provenance (default: operator)')

    # pin-embedding-model
    p_pin = sub.add_parser('pin-embedding-model', parents=[_db],
                            help='Pin an embedding model as the governed embedding space')
    p_pin.add_argument('--adapter-name', required=True, dest='adapter_name')
    p_pin.add_argument('--adapter-version', required=True, dest='adapter_version')
    p_pin.add_argument('--model-name', required=True, dest='model_name')
    p_pin.add_argument('--model-digest', dest='model_digest', default=None,
                       help='Content-addressable model hash (optional)')
    p_pin.add_argument('--dimensions', required=True, type=int)
    p_pin.add_argument('--provider-name', required=True, dest='provider_name')
    p_pin.add_argument('--pinned-by', required=True, dest='pinned_by')
    p_pin.add_argument('--pin-scope', dest='pin_scope', default='global')
    p_pin.add_argument('--notes', default=None)

    # get-active-pin
    p_gap = sub.add_parser('get-active-pin', parents=[_db],
                            help='Show the active embedding model pin for a scope')
    p_gap.add_argument('--pin-scope', dest='pin_scope', default='global')

    # retrieve
    p_ret = sub.add_parser('retrieve', parents=[_db],
                            help='Retrieve ranked memory events with optional semantic reranking')
    p_ret.add_argument('--tags', help='Comma-separated tags to filter/rank by (optional)')
    p_ret.add_argument('--limit', type=int, default=20, help='Max results to return (default: 20)')
    p_ret.add_argument('--no-expand', dest='no_expand', action='store_true',
                       help='Disable related-event expansion (default: expand)')
    p_ret.add_argument('--query-vector-json', dest='query_vector_json', default=None,
                       metavar='JSON_OR_@FILE',
                       help='Precomputed query vector as JSON array, or @filepath to read from file')
    p_ret.add_argument('--query-vector-provenance', dest='query_vector_provenance', default=None,
                       metavar='JSON',
                       help='Provenance dict for the query vector as JSON object (optional)')
    p_ret.add_argument('--log-retrieval', dest='log_retrieval', action='store_true',
                       help='Persist retrieval to log for auditability')
    p_ret.add_argument('--actor', default='system',
                       help='Actor identifier for retrieval log (default: system)')

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _COMMANDS[args.command](args)


if __name__ == '__main__':
    main()
