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
