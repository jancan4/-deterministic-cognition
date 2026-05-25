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
    from .governance import detect_unreviewed_confidence_candidates
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
    else:
        label = 'event' if len(events) == 1 else 'events'
        print(f"{len(events)} memory {label} pending review:\n")
        for ev in events:
            _print_event(ev)

    candidates = detect_unreviewed_confidence_candidates(args.db)
    if candidates:
        clabel = 'revision' if len(candidates) == 1 else 'revisions'
        print(f"{len(candidates)} confidence {clabel} pending review:\n")
        for issue in candidates:
            meta = issue.metadata or {}
            print(
                f"  [rev:{meta.get('revision_id')}] mem:{meta.get('memory_event_id')} "
                f"| {meta.get('confidence_before')} → {meta.get('confidence_after')} "
                f"| severity={issue.severity} | by={meta.get('revised_by')}"
            )
            print(f"    {issue.rationale}")
            print()


def cmd_revise_confidence(args: argparse.Namespace) -> None:
    if not args.operator or not args.operator.strip():
        _die("--operator must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")
    try:
        rev = service.revise_confidence(
            db_path=args.db,
            memory_id=args.id,
            confidence_after=args.confidence,
            revised_by=args.operator,
            reason=args.reason,
            revision_type='operator',
        )
    except (service.ValidationError, service.NotFoundError) as exc:
        _die(str(exc))
    print(json.dumps(rev.to_dict(), indent=2, sort_keys=True))


def cmd_approve_confidence_revision(args: argparse.Namespace) -> None:
    if not args.operator or not args.operator.strip():
        _die("--operator must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")
    try:
        rev = service.approve_confidence_revision(
            db_path=args.db,
            revision_id=args.id,
            operator=args.operator,
            reason=args.reason,
        )
    except (service.ValidationError, service.NotFoundError) as exc:
        _die(str(exc))
    print(json.dumps(rev.to_dict(), indent=2, sort_keys=True))


def cmd_reject_confidence_revision(args: argparse.Namespace) -> None:
    if not args.operator or not args.operator.strip():
        _die("--operator must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")
    try:
        rev = service.reject_candidate_revision(
            db_path=args.db,
            revision_id=args.id,
            rejected_by=args.operator,
            reason=args.reason,
        )
    except (service.ValidationError, service.NotFoundError) as exc:
        _die(str(exc))
    print(json.dumps(rev.to_dict(), indent=2, sort_keys=True))


def cmd_list_confidence_revisions(args: argparse.Namespace) -> None:
    try:
        revs = service.list_confidence_revisions(
            db_path=args.db,
            memory_event_id=args.memory_id,
            revision_type=args.type,
            status=args.status,
        )
    except service.ValidationError as exc:
        _die(str(exc))
    print(json.dumps([r.to_dict() for r in revs], indent=2, sort_keys=True))


def cmd_show_confidence_revision(args: argparse.Namespace) -> None:
    try:
        rev = service.get_confidence_revision(args.db, args.id)
    except service.NotFoundError as exc:
        _die(str(exc))
    print(json.dumps(rev.to_dict(), indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Session commands
# ---------------------------------------------------------------------------

def cmd_open_session(args: argparse.Namespace) -> None:
    from session.models import ContextActivationPolicy
    from session.reconstruction import open_cognition_session
    try:
        policy_dict = json.loads(args.policy_json) if args.policy_json else {}
    except json.JSONDecodeError as exc:
        _die(f"Invalid --policy-json: {exc}")
    policy = ContextActivationPolicy.from_dict(policy_dict)
    metadata = None
    if args.metadata_json:
        try:
            metadata = json.loads(args.metadata_json)
        except json.JSONDecodeError as exc:
            _die(f"Invalid --metadata-json: {exc}")
    if not args.triggered_by or not args.triggered_by.strip():
        _die("--triggered-by must not be empty")
    try:
        sess = open_cognition_session(args.db, policy, args.triggered_by, metadata=metadata)
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(sess.to_dict(), indent=2, sort_keys=True))


def cmd_close_session(args: argparse.Namespace) -> None:
    from session.reconstruction import close_cognition_session
    if not args.triggered_by or not args.triggered_by.strip():
        _die("--triggered-by must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")
    try:
        sess = close_cognition_session(args.db, args.id, args.reason, args.triggered_by)
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(sess.to_dict(), indent=2, sort_keys=True))


def cmd_log_transition(args: argparse.Namespace) -> None:
    from session.reconstruction import log_assembly_transition

    def _parse_int_list(raw: Optional[str]) -> Optional[List[int]]:
        if not raw:
            return None
        try:
            return [int(x.strip()) for x in raw.split(',') if x.strip()]
        except ValueError:
            _die(f"Expected comma-separated integers, got: {raw!r}")

    if not args.triggered_by or not args.triggered_by.strip():
        _die("--triggered-by must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")

    provenance = None
    if args.provenance_json:
        try:
            provenance = json.loads(args.provenance_json)
        except json.JSONDecodeError as exc:
            _die(f"Invalid --provenance-json: {exc}")

    try:
        transition = log_assembly_transition(
            db_path=args.db,
            cognition_session_id=args.session_id,
            to_assembly_id=args.assembly_id,
            transition_type=args.type,
            triggered_by=args.triggered_by,
            reason=args.reason,
            from_assembly_id=args.from_assembly_id,
            triggering_retrieval_ids=_parse_int_list(args.retrieval_ids),
            triggering_confidence_revision_ids=_parse_int_list(args.confidence_revision_ids),
            triggering_contradiction_link_ids=_parse_int_list(args.contradiction_link_ids),
            provenance=provenance,
        )
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(transition.to_dict(), indent=2, sort_keys=True))


def cmd_list_sessions(args: argparse.Namespace) -> None:
    from session.reconstruction import list_cognition_sessions
    sessions = list_cognition_sessions(args.db, status=args.status, limit=args.limit)
    print(json.dumps([s.to_dict() for s in sessions], indent=2, sort_keys=True))


def cmd_show_session(args: argparse.Namespace) -> None:
    from session.reconstruction import get_cognition_session, get_session_assemblies
    try:
        sess = get_cognition_session(args.db, args.id)
        assemblies = get_session_assemblies(args.id, args.db)
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(
        {'session': sess.to_dict(), 'assemblies': assemblies},
        indent=2, sort_keys=True,
    ))


def cmd_replay_session_timeline(args: argparse.Namespace) -> None:
    from session.reconstruction import replay_session_timeline
    try:
        reconstructions = replay_session_timeline(args.id, args.db)
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(
        [r.to_dict() for r in reconstructions],
        indent=2, sort_keys=True,
    ))


def cmd_create_compression_artifact(args: argparse.Namespace) -> None:
    from .compression import create_compression_artifact
    from .artifact_governance import GovernanceInvalidationError
    provenance = None
    if args.provenance_json:
        try:
            provenance = json.loads(args.provenance_json)
        except json.JSONDecodeError as exc:
            _die(f"Invalid --provenance-json: {exc}")
    excluded_ids: List[int] = []
    if args.excluded_event_ids:
        try:
            excluded_ids = [int(x.strip()) for x in args.excluded_event_ids.split(',') if x.strip()]
        except ValueError:
            _die("--excluded-event-ids must be comma-separated integers")
    try:
        artifact = create_compression_artifact(
            db_path=args.db,
            source_assembly_id=args.assembly_id,
            compression_method=args.method,
            producer_version=args.producer_version,
            artifact_text=args.artifact_text,
            created_by=args.created_by,
            cognition_session_id=args.session_id,
            compression_confidence=args.compression_confidence,
            excluded_event_ids=excluded_ids,
            unresolved_issue_count=args.unresolved_issue_count,
            provenance=provenance,
        )
    except (ValueError, GovernanceInvalidationError) as exc:
        _die(str(exc))
    print(json.dumps(artifact.to_dict(), indent=2, sort_keys=True))


def cmd_promote_compression_artifact(args: argparse.Namespace) -> None:
    from .compression import promote_compression_artifact
    from .artifact_governance import GovernanceInvalidationError
    try:
        artifact = promote_compression_artifact(
            db_path=args.db,
            artifact_id=args.id,
            promoted_by=args.promoted_by,
            promotion_notes=args.promotion_notes,
        )
    except (ValueError, GovernanceInvalidationError) as exc:
        _die(str(exc))
    print(json.dumps(artifact.to_dict(), indent=2, sort_keys=True))


def cmd_invalidate_compression_artifact(args: argparse.Namespace) -> None:
    from .compression import invalidate_compression_artifact
    from .artifact_governance import GovernanceInvalidationError
    try:
        artifact = invalidate_compression_artifact(
            db_path=args.db,
            artifact_id=args.id,
            reason=args.reason,
            invalidated_by=args.invalidated_by,
        )
    except (ValueError, GovernanceInvalidationError) as exc:
        _die(str(exc))
    print(json.dumps(artifact.to_dict(), indent=2, sort_keys=True))


def cmd_list_compression_artifacts(args: argparse.Namespace) -> None:
    from .compression import list_compression_artifacts
    artifacts = list_compression_artifacts(
        db_path=args.db,
        status=args.status,
        compression_method=args.method,
        source_assembly_id=args.assembly_id,
        limit=args.limit,
    )
    print(json.dumps([a.to_dict() for a in artifacts], indent=2, sort_keys=True))


def cmd_show_compression_artifact(args: argparse.Namespace) -> None:
    from .compression import get_compression_artifact
    try:
        artifact = get_compression_artifact(args.db, args.id)
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(artifact.to_dict(), indent=2, sort_keys=True))


def cmd_supersede_compression_artifact(args: argparse.Namespace) -> None:
    from .compression import supersede_compression_artifact
    try:
        artifact = supersede_compression_artifact(
            db_path=args.db,
            artifact_id=args.id,
            superseded_by_id=args.superseded_by,
            reason=args.reason,
            superseded_by_operator=args.operator,
        )
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(artifact.to_dict(), indent=2, sort_keys=True))


def cmd_list_supersession_chain(args: argparse.Namespace) -> None:
    from .compression import get_supersession_chain
    try:
        chain = get_supersession_chain(artifact_id=args.id, db_path=args.db)
    except ValueError as exc:
        _die(str(exc))
    print(json.dumps(chain.to_dict(), indent=2, sort_keys=True))


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
    'revise-confidence': cmd_revise_confidence,
    'approve-confidence-revision': cmd_approve_confidence_revision,
    'reject-confidence-revision': cmd_reject_confidence_revision,
    'list-confidence-revisions': cmd_list_confidence_revisions,
    'show-confidence-revision': cmd_show_confidence_revision,
    'open-session': cmd_open_session,
    'close-session': cmd_close_session,
    'log-transition': cmd_log_transition,
    'list-sessions': cmd_list_sessions,
    'show-session': cmd_show_session,
    'replay-session-timeline': cmd_replay_session_timeline,
    'create-compression-artifact': cmd_create_compression_artifact,
    'promote-compression-artifact': cmd_promote_compression_artifact,
    'invalidate-compression-artifact': cmd_invalidate_compression_artifact,
    'list-compression-artifacts': cmd_list_compression_artifacts,
    'show-compression-artifact': cmd_show_compression_artifact,
    'supersede-compression-artifact': cmd_supersede_compression_artifact,
    'list-supersession-chain': cmd_list_supersession_chain,
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

    # revise-confidence
    p_rc = sub.add_parser('revise-confidence', parents=[_db],
                          help='Create an operator confidence revision for a memory event')
    p_rc.add_argument('--id', required=True, type=int, help='Memory event id')
    p_rc.add_argument('--confidence', required=True, type=int,
                      choices=list(range(CONFIDENCE_MIN, CONFIDENCE_MAX + 1)),
                      help='New confidence value (1–5)')
    p_rc.add_argument('--operator', required=True, help='Operator identifier')
    p_rc.add_argument('--reason', required=True, help='Reason for revision')

    # approve-confidence-revision
    p_ac = sub.add_parser('approve-confidence-revision', parents=[_db],
                          help='Approve a proposed candidate confidence revision')
    p_ac.add_argument('--id', required=True, type=int, help='Candidate revision id')
    p_ac.add_argument('--operator', required=True, help='Operator identifier')
    p_ac.add_argument('--reason', required=True, help='Reason for approval')

    # reject-confidence-revision
    p_rjc = sub.add_parser('reject-confidence-revision', parents=[_db],
                           help='Reject a proposed candidate confidence revision')
    p_rjc.add_argument('--id', required=True, type=int, help='Candidate revision id')
    p_rjc.add_argument('--operator', required=True, help='Operator identifier')
    p_rjc.add_argument('--reason', required=True, help='Reason for rejection')

    # list-confidence-revisions
    p_lcr = sub.add_parser('list-confidence-revisions', parents=[_db],
                           help='List confidence revisions with optional filters (JSON output)')
    p_lcr.add_argument('--memory-id', dest='memory_id', type=int, default=None,
                       help='Filter by memory event id')
    p_lcr.add_argument('--type', dest='type', choices=['operator', 'candidate'], default=None,
                       help='Filter by revision type')
    p_lcr.add_argument('--status', choices=['active', 'proposed', 'superseded', 'rejected'],
                       default=None, help='Filter by status')

    # show-confidence-revision
    p_scr = sub.add_parser('show-confidence-revision', parents=[_db],
                           help='Show one confidence revision by id (JSON output)')
    p_scr.add_argument('--id', required=True, type=int, help='Confidence revision id')

    # open-session
    from session.models import VALID_TRANSITION_TYPES, VALID_SESSION_STATUSES
    p_os = sub.add_parser('open-session', parents=[_db],
                          help='Open a new cognition session')
    p_os.add_argument('--triggered-by', required=True, dest='triggered_by',
                      help='Actor opening the session')
    p_os.add_argument('--policy-json', dest='policy_json', default=None,
                      help='ContextActivationPolicy as JSON object (optional; defaults apply)')
    p_os.add_argument('--metadata-json', dest='metadata_json', default=None,
                      help='Arbitrary metadata dict as JSON (optional)')

    # close-session
    p_cs = sub.add_parser('close-session', parents=[_db],
                          help='Close an active cognition session')
    p_cs.add_argument('--id', required=True, type=int, help='Cognition session id')
    p_cs.add_argument('--reason', required=True, help='Reason for closing')
    p_cs.add_argument('--triggered-by', required=True, dest='triggered_by',
                      help='Actor closing the session')

    # log-transition
    p_lt = sub.add_parser('log-transition', parents=[_db],
                          help='Append an assembly transition to a cognition session')
    p_lt.add_argument('--session-id', required=True, type=int, dest='session_id',
                      help='Cognition session id')
    p_lt.add_argument('--assembly-id', required=True, type=int, dest='assembly_id',
                      help='Target assembly id (context_assembly_log.id)')
    p_lt.add_argument('--type', required=True, dest='type',
                      choices=sorted(VALID_TRANSITION_TYPES),
                      help='Transition type')
    p_lt.add_argument('--triggered-by', required=True, dest='triggered_by',
                      help='Actor triggering the transition')
    p_lt.add_argument('--reason', required=True, help='Reason for transition')
    p_lt.add_argument('--from-assembly-id', dest='from_assembly_id', type=int, default=None,
                      help='Prior assembly id (inferred from session state if omitted)')
    p_lt.add_argument('--retrieval-ids', dest='retrieval_ids', default=None,
                      help='Comma-separated retrieval log ids that triggered this transition')
    p_lt.add_argument('--confidence-revision-ids', dest='confidence_revision_ids', default=None,
                      help='Comma-separated confidence revision ids that triggered this transition')
    p_lt.add_argument('--contradiction-link-ids', dest='contradiction_link_ids', default=None,
                      help='Comma-separated contradiction link ids that triggered this transition')
    p_lt.add_argument('--provenance-json', dest='provenance_json', default=None,
                      help='Arbitrary provenance dict as JSON (optional)')

    # list-sessions
    p_ls = sub.add_parser('list-sessions', parents=[_db],
                          help='List cognition sessions (JSON output)')
    p_ls.add_argument('--status', choices=sorted(VALID_SESSION_STATUSES), default=None,
                      help='Filter by status')
    p_ls.add_argument('--limit', type=int, default=50,
                      help='Max sessions to return (default: 50)')

    # show-session
    p_ss = sub.add_parser('show-session', parents=[_db],
                          help='Show one cognition session and its assembly timeline (JSON output)')
    p_ss.add_argument('--id', required=True, type=int, help='Cognition session id')

    # replay-session-timeline
    p_rst = sub.add_parser('replay-session-timeline', parents=[_db],
                           help='Replay all assemblies in a cognition session (JSON output)')
    p_rst.add_argument('--id', required=True, type=int, help='Cognition session id')

    # create-compression-artifact
    p_cca = sub.add_parser('create-compression-artifact', parents=[_db],
                           help='Create a governed compression artifact (status=candidate)')
    p_cca.add_argument('--assembly-id', required=True, type=int, dest='assembly_id',
                       help='Source context_assembly_log id')
    p_cca.add_argument('--method', required=True,
                       help='Compression method identifier (e.g. "extractive_summary_v1")')
    p_cca.add_argument('--producer-version', required=True, dest='producer_version',
                       help='Algorithm version that produced this artifact')
    p_cca.add_argument('--artifact-text', required=True, dest='artifact_text',
                       help='Compressed artifact text')
    p_cca.add_argument('--created-by', required=True, dest='created_by',
                       help='Actor creating this artifact')
    p_cca.add_argument('--session-id', dest='session_id', type=int, default=None,
                       help='Cognition session id to attach to (optional)')
    p_cca.add_argument('--compression-confidence', dest='compression_confidence',
                       type=int, default=None, choices=list(range(1, 6)),
                       help='Operator confidence in compression quality (1–5, optional)')
    p_cca.add_argument('--excluded-event-ids', dest='excluded_event_ids', default=None,
                       help='Comma-separated memory event ids excluded from assembly (optional)')
    p_cca.add_argument('--unresolved-issue-count', dest='unresolved_issue_count',
                       type=int, default=0,
                       help='Count of unresolved governance issues at compression time (default: 0)')
    p_cca.add_argument('--provenance-json', dest='provenance_json', default=None,
                       help='Arbitrary provenance dict as JSON (optional)')

    # promote-compression-artifact
    p_pca = sub.add_parser('promote-compression-artifact', parents=[_db],
                           help='Promote a candidate compression artifact to active')
    p_pca.add_argument('--id', required=True, type=int, help='Compression artifact id')
    p_pca.add_argument('--promoted-by', required=True, dest='promoted_by',
                       help='Operator promoting this artifact')
    p_pca.add_argument('--promotion-notes', required=True, dest='promotion_notes',
                       help='Notes explaining the promotion decision')

    # invalidate-compression-artifact
    p_ica = sub.add_parser('invalidate-compression-artifact', parents=[_db],
                           help='Invalidate a candidate or active compression artifact')
    p_ica.add_argument('--id', required=True, type=int, help='Compression artifact id')
    p_ica.add_argument('--reason', required=True,
                       help='Reason for invalidation')
    p_ica.add_argument('--invalidated-by', required=True, dest='invalidated_by',
                       help='Actor invalidating this artifact')

    # list-compression-artifacts
    p_lca = sub.add_parser('list-compression-artifacts', parents=[_db],
                           help='List compression artifacts with optional filters (JSON output)')
    p_lca.add_argument('--status', choices=['candidate', 'active', 'superseded', 'invalidated'],
                       default=None, help='Filter by status')
    p_lca.add_argument('--method', default=None,
                       help='Filter by compression method')
    p_lca.add_argument('--assembly-id', dest='assembly_id', type=int, default=None,
                       help='Filter by source assembly id')
    p_lca.add_argument('--limit', type=int, default=50,
                       help='Max results to return (default: 50)')

    # show-compression-artifact
    p_sca = sub.add_parser('show-compression-artifact', parents=[_db],
                           help='Show one compression artifact by id (JSON output)')
    p_sca.add_argument('--id', required=True, type=int, help='Compression artifact id')

    # supersede-compression-artifact
    p_sup = sub.add_parser('supersede-compression-artifact', parents=[_db],
                           help='Supersede an active compression artifact with a newer replacement')
    p_sup.add_argument('--id', required=True, type=int,
                       help='Artifact id to supersede (must be active)')
    p_sup.add_argument('--superseded-by', required=True, type=int, dest='superseded_by',
                       help='Id of the replacement artifact')
    p_sup.add_argument('--reason', required=True,
                       help='Reason for supersession')
    p_sup.add_argument('--operator', required=True,
                       help='Operator recording the supersession')

    # list-supersession-chain
    p_lsc = sub.add_parser('list-supersession-chain', parents=[_db],
                           help='Walk the supersession chain from a root artifact (JSON output)')
    p_lsc.add_argument('--id', required=True, type=int,
                       help='Root artifact id (oldest in chain)')

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _COMMANDS[args.command](args)


if __name__ == '__main__':
    main()
