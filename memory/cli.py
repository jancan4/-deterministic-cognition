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

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _COMMANDS[args.command](args)


if __name__ == '__main__':
    main()
