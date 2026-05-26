"""
Output formatting for the memory-shell REPL.

All functions write plain text to stdout. No ANSI colour codes, no external
library dependencies. Every function is deterministic: same input → same output.
"""
import os
import sqlite3
from typing import List, Optional


# ---------------------------------------------------------------------------
# DB counts (cheap reads for prompt)
# ---------------------------------------------------------------------------

def db_counts(db_path: str) -> dict:
    """Read cheap counts from the memory DB for the status prompt.

    Returns zeros on any error so a bad DB path doesn't crash the prompt.
    Uses read-only mode so it never accidentally acquires a write lock.
    """
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM memory_events"
            ).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE status IN ('active','accepted')"
            ).fetchone()[0]
            proposed = conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE status='proposed'"
            ).fetchone()[0]
        finally:
            conn.close()
        return {'total': total, 'active': active, 'proposed': proposed}
    except Exception:
        return {'total': 0, 'active': 0, 'proposed': 0}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_prompt(db_path: str, session_id: Optional[int], counts: dict) -> str:
    db_label = os.path.basename(db_path) if db_path else '(no db)'
    sess = f"session:{session_id}" if session_id is not None else "no-session"
    return (
        f"\n[{db_label} | {sess} | "
        f"events:{counts['total']} | active:{counts['active']} | "
        f"proposed:{counts['proposed']}] > "
    )


# ---------------------------------------------------------------------------
# Section separators
# ---------------------------------------------------------------------------

def separator(title: str = '') -> None:
    if title:
        print(f"\n  ── {title} {'─' * max(0, 54 - len(title))}")
    else:
        print("  " + "─" * 60)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def table(headers: List[str], rows: List[list]) -> None:
    """Print a left-justified fixed-width table to stdout."""
    if not rows:
        print("  (none)")
        return
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:cols]):
            widths[i] = max(widths[i], len(str(cell) if cell is not None else ''))
    header_line = "  " + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("  " + "  ".join("-" * widths[i] for i in range(cols)))
    for row in rows:
        cells = [(str(row[i]) if i < len(row) and row[i] is not None else '').ljust(widths[i])
                 for i in range(cols)]
        print("  " + "  ".join(cells))


# ---------------------------------------------------------------------------
# Event list
# ---------------------------------------------------------------------------

def print_event_list(events, limit: int = 50) -> None:
    """Print a compact table of MemoryEvent objects."""
    shown = events[:limit]
    rows = []
    for e in shown:
        summary = e.summary or ''
        summary = (summary[:58] + "…") if len(summary) > 58 else summary
        rows.append([e.id, e.event_type, e.confidence, e.status, summary])
    table(['id', 'type', 'conf', 'status', 'summary'], rows)
    if len(events) > limit:
        print(f"  … {len(events) - limit} more (use --limit N to show more)")


# ---------------------------------------------------------------------------
# Assembly view
# ---------------------------------------------------------------------------

def print_assembly_summary(assembly_id: int, snapshot: dict, divergence=None) -> None:
    """Print a structured view of a stored context assembly."""
    separator(f"Assembly id={assembly_id}")

    def _section(name: str, items: list) -> None:
        if not items:
            return
        print(f"\n  {name} ({len(items)} events):")
        for m in items:
            mid = m.get('memory_id', '?')
            etype = m.get('event_type', '?')
            conf = m.get('confidence', '?')
            status = m.get('status', '?')
            title = m.get('title', m.get('summary', ''))
            title = (title[:58] + "…") if len(title) > 58 else title
            print(f"    [{mid}] {etype} conf={conf} {status}  {title}")

    _section("Governance context", snapshot.get('governance_context', []))
    _section("Unresolved items",   snapshot.get('unresolved_items', []))
    _section("Relevant memory",    snapshot.get('relevant_memory', []))
    _section("Active investigations", snapshot.get('active_investigations', []))

    included = snapshot.get('included_entries', '?')
    chars_used = snapshot.get('chars_used', '?')
    char_budget = snapshot.get('char_budget', '?')
    print(f"\n  included_entries: {included}  chars: {chars_used}/{char_budget}")

    if divergence is not None:
        added = divergence.events_added_since_assembly
        removed = divergence.events_removed_since_assembly
        if added or removed:
            print(f"\n  DIVERGENCE SINCE ASSEMBLY:")
            if added:
                print(f"    Added since: {added}")
            if removed:
                print(f"    Removed since: {removed}")
        else:
            print("\n  Divergence: none (assembly matches current DB)")


# ---------------------------------------------------------------------------
# Governance summary
# ---------------------------------------------------------------------------

def print_governance_summary(report) -> None:
    """Print a governance report summary with issue counts and details."""
    separator("Governance Report")
    print(
        f"\n  Total events : {report.total_events}"
        f"\n  CRITICAL     : {report.critical_count}"
        f"\n  WARNING      : {report.warning_count}"
        f"\n  INFO         : {report.info_count}"
    )
    critical = [i for i in report.issues if i.severity == 'critical']
    warning  = [i for i in report.issues if i.severity == 'warning']
    if critical:
        print("\n  Critical issues:")
        for issue in critical:
            mid = f" memory_id={issue.memory_id}" if issue.memory_id else ""
            print(f"    [CRITICAL]{mid}  {issue.issue_type}")
            if issue.rationale:
                print(f"      {issue.rationale[:100]}")
    if warning:
        print("\n  Warning issues:")
        for issue in warning:
            mid = f" memory_id={issue.memory_id}" if issue.memory_id else ""
            print(f"    [WARNING]{mid}  {issue.issue_type}")
            if issue.rationale:
                print(f"      {issue.rationale[:100]}")
    if not critical and not warning:
        print("\n  No critical or warning issues.")


# ---------------------------------------------------------------------------
# Event detail
# ---------------------------------------------------------------------------

def print_event_detail(event, revisions, links) -> None:
    """Print full detail for a single memory event with revision and link history."""
    separator(f"Memory Event id={event.id}")
    print(f"\n  id          : {event.id}")
    print(f"  event_type  : {event.event_type}")
    print(f"  status      : {event.status}")
    print(f"  confidence  : {event.confidence}")
    print(f"  created_by  : {event.created_by}")
    print(f"  created_at  : {event.created_at}")
    print(f"  updated_at  : {event.updated_at}")
    print(f"  version     : {event.version}")
    print(f"  source      : {event.source}")
    tags_str = ', '.join(event.tags) if event.tags else '(none)'
    print(f"  tags        : {tags_str}")
    related_str = ', '.join(str(i) for i in event.related_ids) if event.related_ids else '(none)'
    print(f"  related_ids : {related_str}")
    print(f"\n  title:\n    {event.title}")
    summary_lines = (event.summary or '').splitlines()
    print(f"\n  summary:")
    for line in summary_lines:
        print(f"    {line}")
    if event.evidence:
        ev_lines = event.evidence.splitlines()
        print(f"\n  evidence:")
        for line in ev_lines:
            print(f"    {line}")

    if revisions:
        print(f"\n  Revisions ({len(revisions)}):")
        for rev in revisions:
            print(f"    id={rev.id}  at={rev.created_at}  by={rev.created_by}")
            print(f"      reason: {rev.reason}")
    else:
        print("\n  Revisions: none")

    if links:
        print(f"\n  Links ({len(links)}):")
        for lnk in links:
            direction = "→" if lnk.source_id == event.id else "←"
            other_id = lnk.target_id if lnk.source_id == event.id else lnk.source_id
            conf_str = f"  conf={lnk.link_confidence}" if lnk.link_confidence is not None else ""
            print(
                f"    [{lnk.id}] {direction} id={other_id}  "
                f"rel={lnk.relationship}  status={lnk.status}{conf_str}"
            )
    else:
        print("\n  Links: none")


# ---------------------------------------------------------------------------
# Decision history
# ---------------------------------------------------------------------------

def print_decision_history(decisions: list) -> None:
    """Print a table of activation_decision_log rows."""
    separator("Activation Decision History")
    if not decisions:
        print("\n  (no decisions found)")
        return
    rows = []
    for d in decisions:
        fired = "YES" if d.get('fired') else "no"
        trigger = (d.get('trigger_class') or '')[:20]
        reason = (d.get('detection_reason') or '')[:30]
        rows.append([
            d.get('id', '?'),
            d.get('policy_id', '?'),
            fired,
            trigger,
            reason,
            d.get('decided_at', '?'),
        ])
    table(
        ['id', 'policy_id', 'fired', 'trigger_class', 'detection_reason', 'decided_at'],
        rows,
    )
    print(f"\n  Total: {len(decisions)}")


# ---------------------------------------------------------------------------
# Session timeline
# ---------------------------------------------------------------------------

def print_session_timeline(assemblies_data: list) -> None:
    """Print assembly transitions for a cognition session."""
    separator("Session Assembly Timeline")
    if not assemblies_data:
        print("\n  (no assembly transitions found)")
        return
    for i, row in enumerate(assemblies_data):
        seq = row.get('sequence_index', i)
        asm_id = row.get('to_assembly_id', '?')
        assembled_at = row.get('assembled_at', '?')
        entries = row.get('entries_accepted', '?')
        chars_used = row.get('char_budget_used', '?')
        chars_limit = row.get('char_budget_limit', '?')
        ttype = row.get('transition_type', '?')
        treason = (row.get('transition_reason') or '')[:50]
        asm_status = row.get('assembly_status', '?')
        print(
            f"\n  [{seq}] assembly id={asm_id}  {asm_status}  at={assembled_at}"
        )
        print(f"        entries={entries}  chars={chars_used}/{chars_limit}")
        print(f"        transition={ttype}  reason={treason!r}")
    print(f"\n  Total assemblies: {len(assemblies_data)}")


# ---------------------------------------------------------------------------
# Artifact list
# ---------------------------------------------------------------------------

def print_artifact_list(artifacts: list) -> None:
    """Print a table of CompressionArtifact objects."""
    separator("Compression Artifacts")
    if not artifacts:
        print("\n  (none)")
        return
    rows = []
    for a in artifacts:
        preview = (a.artifact_text or '')[:40]
        if len(a.artifact_text or '') > 40:
            preview += '…'
        rows.append([
            a.id,
            a.status,
            a.compression_confidence or '',
            a.artifact_char_count,
            a.generated_at,
            preview,
        ])
    table(
        ['id', 'status', 'conf', 'chars', 'generated_at', 'preview'],
        rows,
    )
    print(f"\n  Total: {len(artifacts)}")


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

_TRANSCRIPT_HEADER = (
    "This transcript is a human-readable audit artifact, not a replay/import format.\n"
    "It does not preserve complete cognition payloads and cannot be re-imported.\n"
    "Do not use this file as a substitute for a continuity bundle."
)

_TITLE_TRUNCATE = 80   # chars for event titles in transcript
_ARTIFACT_PREVIEW = 200  # chars for artifact text preview in transcript


def _trunc(text: str, limit: int) -> str:
    if not text:
        return ''
    if len(text) <= limit:
        return text
    return text[:limit] + '...[truncated]'


def write_transcript(db_path: str, session_id: int, out_path: str) -> int:
    """
    Write a deterministic observational transcript for a cognition session.

    The transcript is a human-readable audit artifact only. It includes:
    - Session metadata
    - Per-assembly: id, created_at, event ids + truncated titles, section counts
    - Per-assembly: continuity artifact ids + truncated previews (no full text)

    Returns the number of assemblies written.

    Raises ValueError if session_id not found.
    Raises OSError if out_path cannot be written.
    """
    import json
    import sqlite3

    def _ro(path: str):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # Fetch session row
    conn = _ro(db_path)
    try:
        sess_row = conn.execute(
            "SELECT id, status, started_at, closed_at, assembly_count "
            "FROM cognition_session WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    if sess_row is None:
        raise ValueError(f"Session id={session_id} not found")

    # Fetch assembly transitions (ordered by sequence_index ASC)
    conn = _ro(db_path)
    try:
        transition_rows = conn.execute(
            """SELECT
                   atl.sequence_index,
                   atl.to_assembly_id,
                   atl.transition_type,
                   atl.transition_reason,
                   atl.transitioned_at,
                   cal.assembled_at,
                   cal.entries_accepted,
                   cal.char_budget_used,
                   cal.char_budget_limit,
                   cal.assembly_snapshot_json
               FROM assembly_transition_log atl
               JOIN context_assembly_log cal ON cal.id = atl.to_assembly_id
               WHERE atl.cognition_session_id = ?
               ORDER BY atl.sequence_index ASC""",
            (session_id,),
        ).fetchall()
        transitions = [dict(r) for r in transition_rows]
    finally:
        conn.close()

    lines = []
    sep = "=" * 64

    lines.append(sep)
    lines.append("  COGNITION SUBSTRATE — OPERATOR AUDIT TRANSCRIPT")
    for hline in _TRANSCRIPT_HEADER.splitlines():
        lines.append(f"  {hline}")
    lines.append(sep)
    lines.append("")
    lines.append(f"  session id    : {sess_row['id']}")
    lines.append(f"  status        : {sess_row['status']}")
    lines.append(f"  started_at    : {sess_row['started_at'] or '?'}")
    lines.append(f"  closed_at     : {sess_row['closed_at'] or '(open)'}")
    lines.append(f"  assembly_count: {sess_row['assembly_count']}")
    lines.append(f"  db_path       : {db_path}")
    lines.append("")

    for i, t in enumerate(transitions):
        asm_id = t['to_assembly_id']
        lines.append("  " + "─" * 60)
        lines.append(
            f"  Assembly {i + 1} of {len(transitions)}"
            f"  id={asm_id}"
            f"  assembled={t['assembled_at']}"
        )
        lines.append(
            f"  transition={t['transition_type']}"
            f"  reason={_trunc(t['transition_reason'] or '', 60)!r}"
        )
        entries = t['entries_accepted'] or 0
        chars_used = t['char_budget_used'] or 0
        chars_limit = t['char_budget_limit'] or 0
        lines.append(f"  entries_accepted={entries}  chars={chars_used}/{chars_limit}")
        lines.append("")

        snapshot_raw = t.get('assembly_snapshot_json') or '{}'
        try:
            snapshot = json.loads(snapshot_raw)
        except Exception:
            snapshot = {}

        _SECTION_KEYS = [
            ('governance_context', 'Governance context'),
            ('unresolved_items', 'Unresolved items'),
            ('relevant_memory', 'Relevant memory'),
            ('active_investigations', 'Active investigations'),
        ]
        for key, label in _SECTION_KEYS:
            items = snapshot.get(key, [])
            if not items:
                continue
            lines.append(f"    {label} ({len(items)} events):")
            for m in items:
                mid = m.get('memory_id', '?')
                etype = m.get('event_type', '?')
                conf = m.get('confidence', '?')
                title = m.get('title', m.get('summary', ''))
                lines.append(
                    f"      [{mid}] {etype} conf={conf}  "
                    f"{_trunc(title, _TITLE_TRUNCATE)}"
                )
            lines.append("")

        # Continuity artifacts in this snapshot
        continuity = snapshot.get('continuity_context', [])
        if continuity:
            lines.append(f"    Continuity artifacts ({len(continuity)}):")
            for entry in continuity:
                art_id = entry.get('artifact_id', '?')
                method = entry.get('compression_method', '?')
                char_count = entry.get('artifact_char_count', '?')
                promoted_at = entry.get('promoted_at', '?')
                text_preview = _trunc(entry.get('artifact_text', ''), _ARTIFACT_PREVIEW)
                lines.append(
                    f"      [artifact_id={art_id}] method={method}"
                    f"  chars={char_count}  promoted={promoted_at}"
                )
                lines.append(f"        preview: {text_preview!r}")
            lines.append("")

    lines.append(sep)
    lines.append("  END OF TRANSCRIPT")
    lines.append(sep)

    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')

    return len(transitions)


# ---------------------------------------------------------------------------
# Import result
# ---------------------------------------------------------------------------

def print_import_result(result, dry_run: bool = False) -> None:
    """Print a summary of an import_bundle() result."""
    mode = "DRY RUN — no writes" if dry_run else "LIVE IMPORT"
    separator(f"Import result ({mode})")
    print(
        f"\n  Events    : {result.imported_memory_events} imported, "
        f"{result.skipped_memory_events} skipped"
    )
    print(
        f"  Sources   : {result.imported_source_documents} imported, "
        f"{result.skipped_source_documents} skipped"
    )
    print(
        f"  Runs      : {result.imported_ingestion_runs} imported, "
        f"{result.skipped_ingestion_runs} skipped"
    )
    if result.warnings:
        print(f"\n  Warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"    WARNING: {w}")
    if result.collisions:
        print(f"\n  COLLISIONS DETECTED ({len(result.collisions)}) — import refused:")
        for c in result.collisions:
            print(f"    [{c.record_type}] id={c.identifier}: {c.reason}")
        print("\n  Action: investigate collision before retrying import.")
    else:
        if not dry_run:
            print("\n  Import successful.")
