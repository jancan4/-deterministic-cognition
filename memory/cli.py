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
# Ontology commands (Phase 8A)
# ---------------------------------------------------------------------------

def cmd_ontology_register(args: argparse.Namespace) -> None:
    from .ontology import register_term
    provenance = None
    if args.provenance:
        try:
            provenance = json.loads(args.provenance)
        except json.JSONDecodeError as exc:
            _die(f"Invalid --provenance JSON: {exc}")
    try:
        term = register_term(
            db_path=args.db,
            vocabulary_name=args.vocabulary,
            term=args.term,
            label=args.label,
            introduced_by=args.introduced_by,
            description=args.description,
            provenance=provenance,
        )
    except ValueError as exc:
        _die(str(exc))
    print(f"registered term id={term.id} vocabulary={term.vocabulary_name!r} "
          f"term={term.term!r} status={term.status}")


def cmd_ontology_deprecate(args: argparse.Namespace) -> None:
    from .ontology import deprecate_term
    try:
        term = deprecate_term(
            db_path=args.db,
            vocabulary_name=args.vocabulary,
            term=args.term,
            deprecated_by=args.deprecated_by,
            deprecation_reason=args.reason,
        )
    except ValueError as exc:
        _die(str(exc))
    print(f"deprecated term={term.term!r} vocabulary={term.vocabulary_name!r} "
          f"at={term.deprecated_at} by={term.deprecated_by!r}")


def cmd_ontology_supersede(args: argparse.Namespace) -> None:
    from .ontology import supersede_term
    try:
        term = supersede_term(
            db_path=args.db,
            vocabulary_name=args.vocabulary,
            term=args.term,
            superseded_by=args.superseded_by,
            deprecated_by=args.deprecated_by,
            deprecation_reason=args.reason,
        )
    except ValueError as exc:
        _die(str(exc))
    print(f"superseded term={term.term!r} vocabulary={term.vocabulary_name!r} "
          f"superseded_by={term.superseded_by!r} at={term.deprecated_at}")


def cmd_ontology_add_alias(args: argparse.Namespace) -> None:
    from .ontology import add_alias
    try:
        alias = add_alias(
            db_path=args.db,
            vocabulary_name=args.vocabulary,
            term=args.term,
            alias=args.alias,
            created_by=args.created_by,
            reason=args.reason,
        )
    except ValueError as exc:
        _die(str(exc))
    print(f"alias added id={alias.id} vocabulary={alias.vocabulary_name!r} "
          f"alias={alias.alias!r} -> term={alias.term!r}")


def cmd_ontology_list(args: argparse.Namespace) -> None:
    from .ontology import list_terms
    terms = list_terms(
        db_path=args.db,
        vocabulary_name=args.vocabulary,
        status=args.status,
    )
    if not terms:
        print("No ontology terms found.")
        return
    header = f"{'id':>4}  {'vocabulary':<20}  {'term':<30}  {'status':<12}  label"
    print(header)
    print('-' * 90)
    for t in terms:
        print(f"{t.id:>4}  {t.vocabulary_name:<20}  {t.term:<30}  {t.status:<12}  {t.label}")


def cmd_ontology_show(args: argparse.Namespace) -> None:
    from .ontology import get_term, list_aliases
    term = get_term(args.db, args.vocabulary, args.term)
    if term is None:
        _die(f"Term {args.term!r} not found in vocabulary {args.vocabulary!r}")
    print(json.dumps(term.to_dict(), indent=2, sort_keys=True))
    aliases = list_aliases(args.db, vocabulary_name=args.vocabulary)
    aliases_for_term = [a for a in aliases if a.term == args.term]
    if aliases_for_term:
        print(f"\nAliases ({len(aliases_for_term)}):")
        for a in aliases_for_term:
            print(f"  {a.alias!r}  (by {a.created_by!r}, {a.created_at})")


def cmd_ontology_resolve(args: argparse.Namespace) -> None:
    from .ontology import resolve_alias
    canonical = resolve_alias(args.db, args.vocabulary, args.alias)
    if canonical is None:
        print(f"No alias {args.alias!r} found in vocabulary {args.vocabulary!r}.")
    else:
        print(f"{args.alias!r} -> {canonical!r}")


def cmd_ontology_migrate_report(args: argparse.Namespace) -> None:
    """Read-only report of post-deprecation usage of deprecated/superseded terms."""
    from .ontology import (
        detect_deprecated_event_type_usage,
        detect_deprecated_relationship_usage,
        detect_deprecated_trigger_class_usage,
        detect_unregistered_compression_methods,
    )
    sections = [
        ('event_type', detect_deprecated_event_type_usage(args.db)),
        ('relationship', detect_deprecated_relationship_usage(args.db)),
        ('trigger_class', detect_deprecated_trigger_class_usage(args.db)),
        ('compression_method (unregistered)', detect_unregistered_compression_methods(args.db)),
    ]
    found_any = False
    for vocab, issues in sorted(sections, key=lambda x: x[0]):
        if not issues:
            continue
        found_any = True
        print(f"\n=== {vocab} ({len(issues)} issue(s)) ===")
        for issue in sorted(issues, key=lambda i: (i.issue_type, i.memory_id)):
            print(f"  [{issue.memory_id}] {issue.title}")
            print(f"    {issue.rationale}")
    if not found_any:
        print("No post-deprecation vocabulary usage detected.")


# ---------------------------------------------------------------------------
# Activation policy commands
# ---------------------------------------------------------------------------

def cmd_seed_memory_from_compression(args: argparse.Namespace) -> None:
    from .compression import MemorySeedException, seed_memory_from_compression
    if not args.operator or not args.operator.strip():
        _die("--operator must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")
    extra_tags = [t.strip() for t in args.tags.split(',') if t.strip()] if args.tags else []
    try:
        event = seed_memory_from_compression(
            db_path=args.db,
            artifact_id=args.artifact_id,
            operator=args.operator,
            reason=args.reason,
            event_type=args.event_type,
            title=args.title,
            tags=extra_tags or None,
            confidence=args.confidence,
        )
    except MemorySeedException as exc:
        _die(
            f"artifact id={args.artifact_id} has already seeded memory event "
            f"id={exc.existing_memory_event_id}. "
            f"Use 'show --id {exc.existing_memory_event_id}' to inspect."
        )
    except ValueError as exc:
        _die(str(exc))
    print(f"created memory event id={event.id} status={event.status}")


def cmd_list_compression_derived_memory(args: argparse.Namespace) -> None:
    from .compression import list_compression_derived_memory
    events = list_compression_derived_memory(
        db_path=args.db,
        status=args.status,
        limit=args.limit,
    )
    if not events:
        print("No compression-derived memory events found.")
        return
    header = (
        f"{'id':>4}  {'event_type':<22}  {'status':<12}  {'conf':>4}  "
        f"{'source':<35}  title"
    )
    print(header)
    print('-' * 110)
    for ev in events:
        print(
            f"{ev.id:>4}  {ev.event_type:<22}  {ev.status:<12}  {ev.confidence:>4}  "
            f"{ev.source:<35}  {ev.title[:40]}"
        )


def cmd_activation_policy_create(args: argparse.Namespace) -> None:
    from session.activation_policy import (
        create_activation_policy,
        ActivationPolicyValidationError,
    )
    conditions = {}
    if args.conditions:
        try:
            conditions = json.loads(args.conditions)
        except json.JSONDecodeError as exc:
            _die(f"Invalid --conditions JSON: {exc}")
    provenance = None
    if args.provenance:
        try:
            provenance = json.loads(args.provenance)
        except json.JSONDecodeError as exc:
            _die(f"Invalid --provenance JSON: {exc}")
    try:
        policy = create_activation_policy(
            db_path=args.db,
            name=args.name,
            trigger_class=args.trigger_class,
            trigger_conditions=conditions,
            created_by=args.created_by,
            reason=args.reason,
            priority=args.priority,
            policy_version=args.policy_version,
            provenance=provenance,
        )
    except (ActivationPolicyValidationError, ValueError) as exc:
        _die(str(exc))
    print(f"created policy id={policy.id} name={policy.name!r} status={policy.status}")


def cmd_activation_policy_list(args: argparse.Namespace) -> None:
    from session.activation_policy import list_activation_policies
    policies = list_activation_policies(
        db_path=args.db,
        status=args.status,
        trigger_class=args.trigger_class,
        limit=args.limit,
    )
    if not policies:
        print("No activation policies found.")
        return
    header = f"{'id':>4}  {'name':<30}  {'trigger_class':<26}  {'status':<12}  {'priority':>8}  created_at"
    print(header)
    print('-' * len(header))
    for p in policies:
        activated = p.activated_at or '—'
        print(
            f"{p.id:>4}  {p.name:<30}  {p.trigger_class:<26}  {p.status:<12}  "
            f"{p.priority:>8}  {p.created_at}"
        )


def cmd_activation_policy_inspect(args: argparse.Namespace) -> None:
    from session.activation_policy import get_activation_policy, list_activation_decisions
    try:
        policy = get_activation_policy(args.db, args.id)
    except ValueError as exc:
        _die(str(exc))

    print(json.dumps(policy.to_dict(), indent=2, sort_keys=True))

    decisions = list_activation_decisions(args.db, policy_id=policy.id, limit=5)
    if decisions:
        print(f"\nLast {len(decisions)} decision(s):")
        for d in decisions:
            print(
                f"  decision_id={d['id']}  fired={bool(d['fired'])}  "
                f"reason={d['detection_reason']!r}  detected_at={d['detected_at']}"
            )
    else:
        print("\nNo decisions logged for this policy.")


def cmd_activation_policy_activate(args: argparse.Namespace) -> None:
    from session.activation_policy import (
        activate_activation_policy,
        ActivationPolicyLifecycleError,
    )
    if not args.operator or not args.operator.strip():
        _die("--operator must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")
    try:
        policy = activate_activation_policy(
            db_path=args.db,
            policy_id=args.id,
            activated_by=args.operator,
            reason=args.reason,
        )
    except (ActivationPolicyLifecycleError, ValueError) as exc:
        _die(str(exc))
    print(f"activated policy id={policy.id} at={policy.activated_at}")


def cmd_activation_policy_supersede(args: argparse.Namespace) -> None:
    from session.activation_policy import (
        supersede_activation_policy,
        ActivationPolicyLifecycleError,
    )
    if not args.operator or not args.operator.strip():
        _die("--operator must not be empty")
    if not args.reason or not args.reason.strip():
        _die("--reason must not be empty")
    try:
        policy = supersede_activation_policy(
            db_path=args.db,
            policy_id=args.id,
            superseded_by_operator=args.operator,
            reason=args.reason,
            superseded_by_policy_id=args.successor_id,
        )
    except (ActivationPolicyLifecycleError, ValueError) as exc:
        _die(str(exc))
    print(f"superseded policy id={policy.id} at={policy.superseded_at}")


def cmd_activation_policy_decisions(args: argparse.Namespace) -> None:
    from session.activation_policy import list_activation_decisions
    decisions = list_activation_decisions(
        db_path=args.db,
        policy_id=args.id,
        fired_only=args.fired_only,
        limit=args.limit,
    )
    if not decisions:
        print("No decisions found.")
        return
    header = f"{'decision_id':>12}  {'fired':>5}  {'trigger_class':<26}  {'detected_at':<22}  reason"
    print(header)
    print('-' * 100)
    for d in decisions:
        print(
            f"{d['id']:>12}  {str(bool(d['fired'])):>5}  {d['trigger_class']:<26}  "
            f"{d['detected_at']:<22}  {d['detection_reason']}"
        )


def cmd_activation_policy_replay(args: argparse.Namespace) -> None:
    """Replay a historical activation decision. Read-only. Never writes."""
    from session.activation_policy import replay_activation_decision, evaluate_trigger
    try:
        replayed = replay_activation_decision(args.db, args.id)
    except ValueError as exc:
        _die(str(exc))

    # Re-evaluate using the snapshot to detect determinism divergence.
    # evaluate_trigger() is pure — no writes, no reads.
    re_result = evaluate_trigger(replayed.policy_snapshot, replayed.trigger_event)

    divergence = (re_result.fired != replayed.fired)

    print(f"decision_id={replayed.decision_id}")
    print(f"policy_id={replayed.policy_snapshot.id} name={replayed.policy_snapshot.name!r}")
    print(f"trigger_class={replayed.trigger_class}")
    print(f"detected_at={replayed.detected_at}")
    print()
    print(f"original  fired={replayed.fired}  reason={replayed.detection_reason!r}")
    print(f"replayed  fired={re_result.fired}  reason={re_result.detection_reason!r}")

    if divergence:
        print()
        print("DIVERGENCE DETECTED: original and replayed fired values differ.")
        print("This indicates non-determinism in trigger evaluation. Investigate the policy snapshot.")
    else:
        print()
        print("deterministic: original and replayed results match.")

    if replayed.resulting_assembly_id is not None:
        print(f"resulting_assembly_id={replayed.resulting_assembly_id}")
    if replayed.resulting_retrieval_id is not None:
        print(f"resulting_retrieval_id={replayed.resulting_retrieval_id}")
    if replayed.resulting_transition_id is not None:
        print(f"resulting_transition_id={replayed.resulting_transition_id}")


def cmd_activation_policy_execute(args: argparse.Namespace) -> None:
    """Execute an active activation policy: evaluate, assemble, and log decision."""
    from session.execution import execute_activation_policy
    from session.models import ContextActivationPolicy

    try:
        trigger_event = json.loads(args.trigger_event)
    except (json.JSONDecodeError, TypeError) as exc:
        _die(f"--trigger-event is not valid JSON: {exc}")

    tags = [t.strip() for t in args.tags.split(',')] if args.tags else []
    context_policy = ContextActivationPolicy(
        tags=tags,
        min_confidence=args.min_confidence,
    )

    try:
        result = execute_activation_policy(
            args.db,
            args.id,
            trigger_event,
            context_policy,
            cognition_session_id=args.session_id,
            triggered_by=args.triggered_by,
            transition_reason=args.reason or '',
            log_non_firing=args.log_non_firing,
        )
    except (ValueError, TypeError) as exc:
        _die(str(exc))

    print(f"policy_id={result.policy_id}")
    print(f"fired={result.fired}")
    print(f"detection_reason={result.detection_reason!r}")

    if result.fired:
        print(f"decision_id={result.decision_id}")
        print(f"resulting_assembly_id={result.resulting_assembly_id}")
        if result.resulting_transition_id is not None:
            print(f"resulting_transition_id={result.resulting_transition_id}")
        else:
            print("resulting_transition_id=None")
        if result.triggering_artifact_ids:
            print(f"triggering_artifact_ids={result.triggering_artifact_ids}")
        if result.transition_error is not None:
            print(f"WARNING: transition logging failed: {result.transition_error}",
                  file=sys.stderr)
    elif result.decision_id is not None:
        print(f"decision_id={result.decision_id} (non-firing, logged)")
    else:
        print("no decision logged (use --log-non-firing to log non-firing decisions)")


def cmd_activation_policy_evaluate(args: argparse.Namespace) -> None:
    """Dry-run evaluate an activation policy trigger. Zero DB writes."""
    from session.activation_policy import evaluate_trigger, get_activation_policy

    try:
        trigger_event = json.loads(args.trigger_event)
    except (json.JSONDecodeError, TypeError) as exc:
        _die(f"--trigger-event is not valid JSON: {exc}")

    try:
        policy = get_activation_policy(args.db, args.id)
    except ValueError as exc:
        _die(str(exc))

    result = evaluate_trigger(policy, trigger_event)

    print(f"policy_id={policy.id}  name={policy.name!r}  status={policy.status!r}")
    print(f"trigger_class={result.trigger_class}")
    print(f"fired={result.fired}")
    print(f"detection_reason={result.detection_reason!r}")
    if result.triggering_artifact_ids:
        print(f"triggering_artifact_ids={result.triggering_artifact_ids}")
    print("[dry-run: zero DB writes]")


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
    'ontology-register': cmd_ontology_register,
    'ontology-deprecate': cmd_ontology_deprecate,
    'ontology-supersede': cmd_ontology_supersede,
    'ontology-add-alias': cmd_ontology_add_alias,
    'ontology-list': cmd_ontology_list,
    'ontology-show': cmd_ontology_show,
    'ontology-resolve': cmd_ontology_resolve,
    'ontology-migrate-report': cmd_ontology_migrate_report,
    'seed-memory-from-compression': cmd_seed_memory_from_compression,
    'list-compression-derived-memory': cmd_list_compression_derived_memory,
    'activation-policy-create': cmd_activation_policy_create,
    'activation-policy-list': cmd_activation_policy_list,
    'activation-policy-inspect': cmd_activation_policy_inspect,
    'activation-policy-activate': cmd_activation_policy_activate,
    'activation-policy-supersede': cmd_activation_policy_supersede,
    'activation-policy-decisions': cmd_activation_policy_decisions,
    'activation-policy-replay': cmd_activation_policy_replay,
    'activation-policy-execute': cmd_activation_policy_execute,
    'activation-policy-evaluate': cmd_activation_policy_evaluate,
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

    # ontology-register
    from .ontology import VALID_VOCABULARY_NAMES, VALID_TERM_STATUSES
    p_or = sub.add_parser('ontology-register', parents=[_db],
                          help='Register a new ontology term with status=active')
    p_or.add_argument('--vocabulary', required=True, choices=sorted(VALID_VOCABULARY_NAMES),
                      help='Vocabulary name')
    p_or.add_argument('--term', required=True, help='Canonical term string')
    p_or.add_argument('--label', required=True, help='Human-readable label')
    p_or.add_argument('--introduced-by', required=True, dest='introduced_by',
                      help='Operator registering this term')
    p_or.add_argument('--description', default=None, help='Optional description')
    p_or.add_argument('--provenance', default=None, help='Optional provenance as JSON')

    # ontology-deprecate
    p_od = sub.add_parser('ontology-deprecate', parents=[_db],
                          help='Deprecate an active ontology term')
    p_od.add_argument('--vocabulary', required=True, choices=sorted(VALID_VOCABULARY_NAMES),
                      help='Vocabulary name')
    p_od.add_argument('--term', required=True, help='Term to deprecate')
    p_od.add_argument('--deprecated-by', required=True, dest='deprecated_by',
                      help='Operator deprecating this term')
    p_od.add_argument('--reason', required=True, help='Reason for deprecation')

    # ontology-supersede
    p_os = sub.add_parser('ontology-supersede', parents=[_db],
                          help='Supersede an active ontology term with a canonical replacement')
    p_os.add_argument('--vocabulary', required=True, choices=sorted(VALID_VOCABULARY_NAMES),
                      help='Vocabulary name')
    p_os.add_argument('--term', required=True, help='Term to supersede')
    p_os.add_argument('--superseded-by', required=True, dest='superseded_by',
                      help='Canonical replacement term (must be active in same vocabulary)')
    p_os.add_argument('--deprecated-by', required=True, dest='deprecated_by',
                      help='Operator authorizing this supersession')
    p_os.add_argument('--reason', required=True, help='Reason for supersession')

    # ontology-add-alias
    p_oaa = sub.add_parser('ontology-add-alias', parents=[_db],
                           help='Add an alias for a canonical ontology term')
    p_oaa.add_argument('--vocabulary', required=True, choices=sorted(VALID_VOCABULARY_NAMES),
                       help='Vocabulary name')
    p_oaa.add_argument('--term', required=True, help='Canonical term to alias')
    p_oaa.add_argument('--alias', required=True, help='Alias string')
    p_oaa.add_argument('--created-by', required=True, dest='created_by',
                       help='Operator creating this alias')
    p_oaa.add_argument('--reason', required=True, help='Reason for this alias')

    # ontology-list
    p_ol = sub.add_parser('ontology-list', parents=[_db],
                          help='List ontology terms (ordered by vocabulary, term)')
    p_ol.add_argument('--vocabulary', choices=sorted(VALID_VOCABULARY_NAMES), default=None,
                      help='Filter by vocabulary')
    p_ol.add_argument('--status', choices=list(VALID_TERM_STATUSES), default=None,
                      help='Filter by status')

    # ontology-show
    p_osh = sub.add_parser('ontology-show', parents=[_db],
                           help='Show a specific ontology term with aliases')
    p_osh.add_argument('--vocabulary', required=True, choices=sorted(VALID_VOCABULARY_NAMES),
                       help='Vocabulary name')
    p_osh.add_argument('--term', required=True, help='Term to show')

    # ontology-resolve
    p_ores = sub.add_parser('ontology-resolve', parents=[_db],
                            help='Resolve an alias to its canonical term (one step only)')
    p_ores.add_argument('--vocabulary', required=True, choices=sorted(VALID_VOCABULARY_NAMES),
                        help='Vocabulary name')
    p_ores.add_argument('--alias', required=True, help='Alias string to resolve')

    # ontology-migrate-report
    p_omr = sub.add_parser('ontology-migrate-report', parents=[_db],
                           help='Report post-deprecation vocabulary usage (read-only)')

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

    # seed-memory-from-compression
    p_smfc = sub.add_parser('seed-memory-from-compression', parents=[_db],
                            help='Create a proposed memory candidate from an active compression artifact')
    p_smfc.add_argument('--artifact-id', required=True, type=int, dest='artifact_id',
                        help='Compression artifact id (must have status=active)')
    p_smfc.add_argument('--operator', required=True, help='Operator seeding this candidate')
    p_smfc.add_argument('--reason', required=True, help='Reason for creating the memory candidate')
    p_smfc.add_argument('--event-type', required=True, dest='event_type',
                        choices=sorted(VALID_EVENT_TYPES), help='Memory event type')
    p_smfc.add_argument('--title', required=True, help='Memory event title')
    p_smfc.add_argument('--tags', default=None,
                        help='Comma-separated additional tags (optional; "compression-derived" always added)')
    p_smfc.add_argument('--confidence', type=int, default=None,
                        choices=list(range(CONFIDENCE_MIN, CONFIDENCE_MAX + 1)),
                        help='Override confidence 1–5 (default: from artifact.compression_confidence or 3)')

    # list-compression-derived-memory
    p_lcdm = sub.add_parser('list-compression-derived-memory', parents=[_db],
                             help='List memory events seeded from compression artifacts')
    p_lcdm.add_argument('--status', choices=list(VALID_STATUSES), default=None,
                        help='Filter by status')
    p_lcdm.add_argument('--limit', type=int, default=50, help='Max results (default: 50)')

    # activation-policy-create
    from session.activation_policy import VALID_POLICY_STATUSES
    from session.models import VALID_TRIGGER_CLASSES
    p_apc = sub.add_parser('activation-policy-create', parents=[_db],
                           help='Create a candidate activation policy')
    p_apc.add_argument('--name', required=True, help='Policy name')
    p_apc.add_argument('--trigger-class', required=True, dest='trigger_class',
                       choices=sorted(VALID_TRIGGER_CLASSES),
                       help='Trigger class')
    p_apc.add_argument('--created-by', required=True, dest='created_by',
                       help='Operator creating this policy')
    p_apc.add_argument('--reason', required=True, help='Reason for creating this policy')
    p_apc.add_argument('--conditions', default=None,
                       help='Trigger conditions as JSON object (optional)')
    p_apc.add_argument('--priority', type=int, default=100,
                       help='Priority (lower = higher priority; default: 100)')
    p_apc.add_argument('--policy-version', dest='policy_version', default='1.0.0',
                       help='Policy version string (default: 1.0.0)')
    p_apc.add_argument('--provenance', default=None,
                       help='Provenance metadata as JSON object (optional)')

    # activation-policy-list
    p_apl = sub.add_parser('activation-policy-list', parents=[_db],
                           help='List activation policies')
    p_apl.add_argument('--status', choices=sorted(VALID_POLICY_STATUSES), default=None,
                       help='Filter by status')
    p_apl.add_argument('--trigger-class', dest='trigger_class',
                       choices=sorted(VALID_TRIGGER_CLASSES), default=None,
                       help='Filter by trigger class')
    p_apl.add_argument('--limit', type=int, default=50, help='Max results (default: 50)')

    # activation-policy-inspect
    p_api = sub.add_parser('activation-policy-inspect', parents=[_db],
                           help='Show full activation policy row and recent decisions')
    p_api.add_argument('--id', required=True, type=int, help='Activation policy id')

    # activation-policy-activate
    p_apa = sub.add_parser('activation-policy-activate', parents=[_db],
                           help='Transition an activation policy from candidate to active')
    p_apa.add_argument('--id', required=True, type=int, help='Activation policy id')
    p_apa.add_argument('--operator', required=True, help='Operator activating this policy')
    p_apa.add_argument('--reason', required=True, help='Reason for activation')

    # activation-policy-supersede
    p_aps = sub.add_parser('activation-policy-supersede', parents=[_db],
                           help='Transition an active activation policy to superseded')
    p_aps.add_argument('--id', required=True, type=int, help='Activation policy id')
    p_aps.add_argument('--operator', required=True, help='Operator superseding this policy')
    p_aps.add_argument('--reason', required=True, help='Reason for supersession')
    p_aps.add_argument('--successor-id', dest='successor_id', type=int, default=None,
                       help='Id of the replacement policy (optional)')

    # activation-policy-decisions
    p_apd = sub.add_parser('activation-policy-decisions', parents=[_db],
                           help='List activation decisions for a policy')
    p_apd.add_argument('--id', required=True, type=int, help='Activation policy id')
    p_apd.add_argument('--fired-only', dest='fired_only', action='store_true',
                       help='Only show decisions where fired=True')
    p_apd.add_argument('--limit', type=int, default=20, help='Max results (default: 20)')

    # activation-policy-replay
    p_apr = sub.add_parser('activation-policy-replay', parents=[_db],
                           help='Replay a historical activation decision (read-only, never writes)')
    p_apr.add_argument('--id', required=True, type=int, help='Activation decision id')

    # activation-policy-execute
    p_apex = sub.add_parser('activation-policy-execute', parents=[_db],
                            help='Evaluate and execute an active activation policy')
    p_apex.add_argument('--id', required=True, type=int, help='Activation policy id')
    p_apex.add_argument('--trigger-event', required=True, dest='trigger_event',
                        metavar='JSON', help='Trigger context as a JSON object')
    p_apex.add_argument('--triggered-by', required=True, dest='triggered_by',
                        help='Actor initiating execution')
    p_apex.add_argument('--reason', default='', help='Reason for this execution (optional)')
    p_apex.add_argument('--session-id', dest='session_id', type=int, default=None,
                        help='Cognition session id to log transition into (optional)')
    p_apex.add_argument('--tags', default='',
                        help='Comma-separated tags for retrieval scope (optional)')
    p_apex.add_argument('--min-confidence', dest='min_confidence', type=int, default=1,
                        help='Minimum confidence for retrieval (default: 1)')
    p_apex.add_argument('--log-non-firing', dest='log_non_firing', action='store_true',
                        help='Log a decision row even when fired=False')

    # activation-policy-evaluate
    p_apev = sub.add_parser('activation-policy-evaluate', parents=[_db],
                            help='Dry-run evaluate a policy trigger — zero DB writes')
    p_apev.add_argument('--id', required=True, type=int, help='Activation policy id')
    p_apev.add_argument('--trigger-event', required=True, dest='trigger_event',
                        metavar='JSON', help='Trigger context as a JSON object')

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _COMMANDS[args.command](args)


if __name__ == '__main__':
    main()
