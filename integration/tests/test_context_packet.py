"""Tests for ContextPacket identity, serialization, and structural invariants."""
import json
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
    derive_policy_hash,
)


def _make_entry(event_id=1, event_type="governance_rule", content="Test content.", assembly_order=0):
    return PacketEntry(
        event_id=event_id,
        event_type=event_type,
        content=content,
        assembly_order=assembly_order,
        char_count=len(content),
    )


def _make_budget(**kw):
    defaults = dict(
        total_chars=100, governance_rule_chars=50,
        architecture_decision_chars=30, narrative_chars=20, entry_count=3,
    )
    defaults.update(kw)
    return BudgetSummary(**defaults)


def _make_envelope(task_type="echo", task_prompt="Describe this."):
    return TaskEnvelope.build(task_type=task_type, task_prompt_text=task_prompt)


def _make_provenance(adapter_target="echo"):
    return PacketProvenance(
        adapter_target=adapter_target,
        requested_by="operator",
        source_db_path="/tmp/test.db",
    )


def _make_packet(
    assembly_id=1,
    assembly_hash="abc123",
    policy_hash="def456",
    adapter_target="echo",
    task_type="echo",
    task_prompt="Test task.",
    gov_entries=None,
    adr_entries=None,
):
    envelope = TaskEnvelope.build(task_type=task_type, task_prompt_text=task_prompt)
    packet_id = derive_packet_id(
        assembly_id=assembly_id,
        assembly_hash=assembly_hash,
        policy_hash=policy_hash,
        task_envelope_hash=envelope.task_envelope_hash,
        adapter_target=adapter_target,
    )
    return ContextPacket(
        packet_schema_version=PACKET_SCHEMA_VERSION,
        packet_id=packet_id,
        generated_at="2026-05-28T10:00:00Z",
        substrate_assembly_id=assembly_id,
        substrate_assembly_hash=assembly_hash,
        substrate_schema_version=16,
        policy_hash=policy_hash,
        governance_rules=gov_entries or [_make_entry(event_id=10, event_type="governance_rule")],
        architecture_decisions=adr_entries or [],
        implementation_notes=[],
        open_questions=[],
        narrative_memory=[],
        budget_summary=_make_budget(),
        task_envelope=envelope,
        provenance=_make_provenance(adapter_target=adapter_target),
    )


class TestPacketIdDeterminism:
    def test_packet_id_is_deterministic(self):
        envelope = TaskEnvelope.build("echo", "Same prompt.")
        pid1 = derive_packet_id(1, "hash1", "phash", envelope.task_envelope_hash, "echo")
        pid2 = derive_packet_id(1, "hash1", "phash", envelope.task_envelope_hash, "echo")
        assert pid1 == pid2

    def test_packet_id_stable_across_generation_timestamps(self):
        """generated_at must not participate in packet_id — same inputs at different times → same id."""
        task_prompt = "Test prompt."
        envelope = TaskEnvelope.build("echo", task_prompt)
        pid_base = derive_packet_id(5, "ahash", "phash", envelope.task_envelope_hash, "echo")

        p1 = _make_packet(assembly_id=5, assembly_hash="ahash", policy_hash="phash", task_prompt=task_prompt)
        p2 = _make_packet(assembly_id=5, assembly_hash="ahash", policy_hash="phash", task_prompt=task_prompt)
        # Force different generated_at
        import dataclasses
        p2 = dataclasses.replace(p2, generated_at="2099-01-01T00:00:00Z")

        assert p1.packet_id == p2.packet_id
        assert p1.packet_id == pid_base

    def test_packet_id_differs_for_different_assembly_hash(self):
        envelope = TaskEnvelope.build("echo", "Same.")
        pid1 = derive_packet_id(1, "hash_A", "ph", envelope.task_envelope_hash, "echo")
        pid2 = derive_packet_id(1, "hash_B", "ph", envelope.task_envelope_hash, "echo")
        assert pid1 != pid2

    def test_packet_id_differs_for_different_adapter_target(self):
        envelope = TaskEnvelope.build("echo", "Same.")
        pid1 = derive_packet_id(1, "h", "ph", envelope.task_envelope_hash, "echo")
        pid2 = derive_packet_id(1, "h", "ph", envelope.task_envelope_hash, "ollama/mistral")
        assert pid1 != pid2


class TestPacketSectionOrder:
    def test_section_order_is_fixed(self):
        assert SECTION_ORDER == (
            "governance_rules",
            "architecture_decisions",
            "implementation_notes",
            "open_questions",
            "narrative_memory",
        )

    def test_packet_entry_order_matches_assembly_order(self):
        entries = [
            _make_entry(event_id=3, assembly_order=2),
            _make_entry(event_id=1, assembly_order=0),
            _make_entry(event_id=2, assembly_order=1),
        ]
        p = _make_packet(gov_entries=entries)
        sorted_entries = sorted(p.governance_rules, key=lambda e: e.assembly_order)
        assert [e.event_id for e in sorted_entries] == [1, 2, 3]

    def test_packet_contains_no_computed_weights(self):
        p = _make_packet()
        d = p.to_dict()
        all_text = json.dumps(d)
        for forbidden in ("score", "similarity", "rank_override", "relevance_weight"):
            assert forbidden not in all_text, f"Forbidden field '{forbidden}' found in packet"


class TestPacketSerialization:
    def test_packet_round_trips_json(self):
        p = _make_packet()
        json_str = p.to_json()
        p2 = ContextPacket.from_json(json_str)
        assert p2.to_json() == json_str

    def test_packet_from_dict_round_trip(self):
        p = _make_packet()
        d = p.to_dict()
        p2 = ContextPacket.from_dict(d)
        assert p2.packet_id == p.packet_id
        assert p2.substrate_assembly_hash == p.substrate_assembly_hash
        assert len(p2.governance_rules) == len(p.governance_rules)

    def test_packet_budget_summary_entry_count_matches_sections(self):
        gov = [_make_entry(event_id=i, event_type="governance_rule") for i in range(3)]
        p = _make_packet(gov_entries=gov)
        total = sum(
            len(getattr(p, s)) for s in SECTION_ORDER
        )
        assert total == p.budget_summary.entry_count
