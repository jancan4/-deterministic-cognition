"""
Build a ContextPacket from a logged substrate assembly.

packet_from_reconstruction() converts a SessionReconstruction that has been
logged to context_assembly_log into a ContextPacket. The assembly must have
been logged before this call; use session.reconstruction.log_assembly() first.

The packet captures a static snapshot — it is detached from the live DB after
this function returns.
"""
import sqlite3
from typing import List, Optional

from .models import (
    BudgetSummary,
    ContextPacket,
    PacketEntry,
    PacketProvenance,
    PACKET_SCHEMA_VERSION,
    SECTION_EVENT_TYPES,
    SECTION_ORDER,
    TaskEnvelope,
    _now,
    derive_packet_id,
    derive_policy_hash,
)


def _get_schema_version(db_path: str) -> int:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT version FROM memory_schema_version").fetchone()
            return int(row["version"]) if row else 0
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()
    except Exception:
        return 0


def _section_for_event_type(event_type: str) -> Optional[str]:
    for section_name, types in SECTION_EVENT_TYPES.items():
        if event_type in types:
            return section_name
    return "narrative_memory"


def packet_from_reconstruction(
    reconstruction,
    assembly_log_row: dict,
    task_envelope: TaskEnvelope,
    adapter_target: str,
    requested_by: str,
    db_path: str,
    redact_provenance: bool = False,
) -> ContextPacket:
    """
    Build an immutable ContextPacket from a logged assembly.

    assembly_log_row must be the dict returned by session.reconstruction.log_assembly().
    All entries from governance_context, unresolved_items, relevant_memory, and
    active_investigations are partitioned by event_type into the five packet sections.

    assembly_order within each section is the 0-based position in the list as
    returned by the reconstruction (which is already sorted by activation_rank).
    No re-sorting occurs here — we preserve the doctrine order from assembly.
    """
    ctx = reconstruction.context
    policy_hash = derive_policy_hash(ctx.policy.to_dict())
    assembly_id = assembly_log_row["id"]
    assembly_hash = assembly_log_row["assembly_hash"]

    packet_id = derive_packet_id(
        assembly_id=assembly_id,
        assembly_hash=assembly_hash,
        policy_hash=policy_hash,
        task_envelope_hash=task_envelope.task_envelope_hash,
        adapter_target=adapter_target,
        packet_schema_version=PACKET_SCHEMA_VERSION,
    )

    # Collect all activated memory entries in their existing activation order
    # (governance_context first — it has highest priority, then the rest)
    all_mem = (
        list(ctx.governance_context)
        + list(ctx.unresolved_items)
        + list(ctx.active_investigations)
        + list(ctx.relevant_memory)
    )

    # Partition into sections, assigning assembly_order within each section
    section_accumulators: dict = {name: [] for name in SECTION_ORDER}
    seen_ids = set()

    for mem in all_mem:
        if mem.memory_id in seen_ids:
            continue
        seen_ids.add(mem.memory_id)
        section_name = _section_for_event_type(mem.event_type)
        section_accumulators[section_name].append(mem)

    sections: dict = {}
    total_chars = 0
    governance_rule_chars = 0
    architecture_decision_chars = 0
    narrative_chars = 0

    for section_name in SECTION_ORDER:
        entries = []
        for order_idx, mem in enumerate(section_accumulators[section_name]):
            content = mem.render()
            char_count = len(content)
            entry = PacketEntry(
                event_id=mem.memory_id,
                event_type=mem.event_type,
                content=content,
                assembly_order=order_idx,
                char_count=char_count,
            )
            entries.append(entry)
            total_chars += char_count
            if mem.event_type == "governance_rule":
                governance_rule_chars += char_count
            elif mem.event_type == "architecture_decision":
                architecture_decision_chars += char_count
            else:
                narrative_chars += char_count
        sections[section_name] = entries

    budget_summary = BudgetSummary(
        total_chars=total_chars,
        governance_rule_chars=governance_rule_chars,
        architecture_decision_chars=architecture_decision_chars,
        narrative_chars=narrative_chars,
        entry_count=sum(len(v) for v in sections.values()),
    )

    schema_version = _get_schema_version(db_path)
    provenance = PacketProvenance(
        adapter_target=adapter_target,
        requested_by="" if redact_provenance else requested_by,
        source_db_path="" if redact_provenance else db_path,
    )

    return ContextPacket(
        packet_schema_version=PACKET_SCHEMA_VERSION,
        packet_id=packet_id,
        generated_at=_now(),
        substrate_assembly_id=assembly_id,
        substrate_assembly_hash=assembly_hash,
        substrate_schema_version=schema_version,
        policy_hash=policy_hash,
        governance_rules=sections["governance_rules"],
        architecture_decisions=sections["architecture_decisions"],
        implementation_notes=sections["implementation_notes"],
        open_questions=sections["open_questions"],
        narrative_memory=sections["narrative_memory"],
        budget_summary=budget_summary,
        task_envelope=task_envelope,
        provenance=provenance,
    )
