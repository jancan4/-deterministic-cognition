"""
Deterministic prompt renderer for ContextPacket.

render_prompt(packet) is a pure function:
  - Same packet → same rendered string always.
  - No DB access.
  - No re-ranking. Section and entry order are fixed.
  - Sections with no entries are omitted.
  - Task instruction always appears last.
"""
from .models import ContextPacket, SECTION_ORDER

_SECTION_LABELS = {
    "governance_rules": "GOVERNANCE RULES",
    "architecture_decisions": "ARCHITECTURE DECISIONS",
    "implementation_notes": "IMPLEMENTATION NOTES",
    "open_questions": "OPEN QUESTIONS",
    "narrative_memory": "NARRATIVE MEMORY",
}


def render_prompt(packet: ContextPacket) -> str:
    """
    Render a ContextPacket to a model prompt string.

    Rendering invariants:
      1. Section order: governance_rules → architecture_decisions →
         implementation_notes → open_questions → narrative_memory
      2. Entries within each section ordered by assembly_order ascending.
         assembly_order is NOT a relevance score — it is deterministic doctrine
         order, identical to the context window assembly order.
      3. Content is verbatim from the assembly.
      4. Task instruction is appended last, after all context sections.
      5. Budget block is a comment header only — not in content sections.
      6. No substrate metadata injected into content sections.
      7. Empty sections produce no header in output.
    """
    lines = [
        f"# Context packet: {packet.packet_id[:12]}",
        f"# Assembly: {packet.substrate_assembly_hash[:12]}",
        (
            f"# Budget: {packet.budget_summary.total_chars} chars, "
            f"{packet.budget_summary.entry_count} entries"
        ),
        f"# Task: {packet.task_envelope.task_type} / {packet.task_envelope.task_id[:12]}",
        "",
    ]

    for section_name in SECTION_ORDER:
        entries = getattr(packet, section_name)
        if not entries:
            continue
        label = _SECTION_LABELS[section_name]
        sorted_entries = sorted(entries, key=lambda e: e.assembly_order)
        lines.append(f"--- {label} ---")
        for entry in sorted_entries:
            lines.append(entry.content)
        lines.append("")

    lines.append("--- TASK ---")
    lines.append(packet.task_envelope.task_prompt_text)

    return "\n".join(lines)
