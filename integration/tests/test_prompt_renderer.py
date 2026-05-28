"""Tests for the deterministic prompt renderer."""
import pytest

from integration.models import (
    BudgetSummary,
    ContextPacket,
    PacketEntry,
    PacketProvenance,
    PACKET_SCHEMA_VERSION,
    SECTION_ORDER,
    TaskEnvelope,
    derive_packet_id,
)
from integration.renderer import render_prompt


def _entry(event_id, event_type, content, assembly_order=0):
    return PacketEntry(
        event_id=event_id,
        event_type=event_type,
        content=content,
        assembly_order=assembly_order,
        char_count=len(content),
    )


def _budget(entry_count=1):
    return BudgetSummary(
        total_chars=100, governance_rule_chars=50,
        architecture_decision_chars=30, narrative_chars=20,
        entry_count=entry_count,
    )


def _make_packet(
    gov_entries=None,
    adr_entries=None,
    impl_entries=None,
    oq_entries=None,
    narr_entries=None,
    task_type="echo",
    task_prompt="Summarise the context.",
):
    envelope = TaskEnvelope.build(task_type=task_type, task_prompt_text=task_prompt)
    packet_id = derive_packet_id(1, "h", "ph", envelope.task_envelope_hash, "echo")
    all_entries = (
        (gov_entries or [])
        + (adr_entries or [])
        + (impl_entries or [])
        + (oq_entries or [])
        + (narr_entries or [])
    )
    return ContextPacket(
        packet_schema_version=PACKET_SCHEMA_VERSION,
        packet_id=packet_id,
        generated_at="2026-05-28T10:00:00Z",
        substrate_assembly_id=1,
        substrate_assembly_hash="h",
        substrate_schema_version=16,
        policy_hash="ph",
        governance_rules=gov_entries or [],
        architecture_decisions=adr_entries or [],
        implementation_notes=impl_entries or [],
        open_questions=oq_entries or [],
        narrative_memory=narr_entries or [],
        budget_summary=_budget(entry_count=len(all_entries)),
        task_envelope=envelope,
        provenance=PacketProvenance("echo", "op", "/tmp/t.db"),
    )


class TestRenderDeterminism:
    def test_render_is_deterministic(self):
        p = _make_packet(gov_entries=[_entry(1, "governance_rule", "Rule A.")])
        assert render_prompt(p) == render_prompt(p)

    def test_render_same_packet_twice_identical(self):
        p = _make_packet(gov_entries=[_entry(1, "governance_rule", "Rule A.")])
        r1 = render_prompt(p)
        r2 = render_prompt(p)
        assert r1 == r2


class TestRenderSectionOrder:
    def test_governance_rules_before_architecture_decisions(self):
        p = _make_packet(
            gov_entries=[_entry(1, "governance_rule", "Rule A.")],
            adr_entries=[_entry(2, "architecture_decision", "ADR B.")],
        )
        rendered = render_prompt(p)
        gov_pos = rendered.index("GOVERNANCE RULES")
        adr_pos = rendered.index("ARCHITECTURE DECISIONS")
        assert gov_pos < adr_pos

    def test_task_is_last_section(self):
        p = _make_packet(
            gov_entries=[_entry(1, "governance_rule", "Rule A.")],
            adr_entries=[_entry(2, "architecture_decision", "ADR B.")],
            task_prompt="Final task prompt.",
        )
        rendered = render_prompt(p)
        task_pos = rendered.index("--- TASK ---")
        for section_label in ["GOVERNANCE RULES", "ARCHITECTURE DECISIONS"]:
            assert rendered.index(section_label) < task_pos

    def test_empty_sections_omitted(self):
        p = _make_packet(
            gov_entries=[_entry(1, "governance_rule", "Rule A.")],
        )
        rendered = render_prompt(p)
        assert "ARCHITECTURE DECISIONS" not in rendered
        assert "IMPLEMENTATION NOTES" not in rendered
        assert "OPEN QUESTIONS" not in rendered
        assert "NARRATIVE MEMORY" not in rendered

    def test_entries_sorted_by_assembly_order(self):
        entries = [
            _entry(3, "governance_rule", "Third.", assembly_order=2),
            _entry(1, "governance_rule", "First.", assembly_order=0),
            _entry(2, "governance_rule", "Second.", assembly_order=1),
        ]
        p = _make_packet(gov_entries=entries)
        rendered = render_prompt(p)
        first_pos = rendered.index("First.")
        second_pos = rendered.index("Second.")
        third_pos = rendered.index("Third.")
        assert first_pos < second_pos < third_pos


class TestRenderContentInvariants:
    def test_verbatim_content_appears(self):
        known_content = "This is a unique governance rule text."
        p = _make_packet(gov_entries=[_entry(1, "governance_rule", known_content)])
        rendered = render_prompt(p)
        assert known_content in rendered

    def test_assembly_order_not_exposed_as_score(self):
        p = _make_packet(gov_entries=[_entry(1, "governance_rule", "Rule A.", assembly_order=7)])
        rendered = render_prompt(p)
        for forbidden in ("score", "similarity", "relevance", "rank_override"):
            assert forbidden not in rendered

    def test_task_prompt_appears_verbatim(self):
        task_text = "Extract all open questions from the context."
        p = _make_packet(task_prompt=task_text)
        rendered = render_prompt(p)
        assert task_text in rendered

    def test_header_contains_packet_id_prefix(self):
        p = _make_packet()
        rendered = render_prompt(p)
        assert p.packet_id[:12] in rendered

    def test_header_contains_assembly_hash_prefix(self):
        p = _make_packet()
        rendered = render_prompt(p)
        assert p.substrate_assembly_hash[:12] in rendered
