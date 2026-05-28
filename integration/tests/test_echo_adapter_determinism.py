"""
EchoPacketAdapter determinism test — the first complete integration round-trip.

Verifies that: same packet → same rendered prompt → same raw_output_text → same result_id
This is the foundational determinism guarantee for the integration layer.
"""
import pytest

from integration.adapters import EchoPacketAdapter, run_packet_task
from integration.models import (
    BudgetSummary,
    ContextPacket,
    PacketEntry,
    PacketProvenance,
    PACKET_SCHEMA_VERSION,
    TaskEnvelope,
    derive_packet_id,
)
from integration.renderer import render_prompt


def _make_packet(task_prompt="Test the echo round-trip."):
    envelope = TaskEnvelope.build("echo", task_prompt)
    packet_id = derive_packet_id(1, "assemblyhash", "policyhash", envelope.task_envelope_hash, "echo")
    return ContextPacket(
        packet_schema_version=PACKET_SCHEMA_VERSION,
        packet_id=packet_id,
        generated_at="2026-05-28T10:00:00Z",
        substrate_assembly_id=1,
        substrate_assembly_hash="assemblyhash",
        substrate_schema_version=16,
        policy_hash="policyhash",
        governance_rules=[
            PacketEntry(1, "governance_rule", "Rule: do not trade.", 0, 22),
        ],
        architecture_decisions=[
            PacketEntry(2, "architecture_decision", "ADR-001: use SQLite.", 0, 22),
        ],
        implementation_notes=[],
        open_questions=[],
        narrative_memory=[],
        budget_summary=BudgetSummary(44, 22, 22, 0, 2),
        task_envelope=envelope,
        provenance=PacketProvenance("echo", "operator", "/tmp/test.db"),
    )


class TestEchoRoundTripDeterminism:
    def test_echo_roundtrip_is_deterministic(self):
        """
        Core determinism guarantee:
          same packet → same rendered prompt → same raw_output_text → same result_id
        Proven by running twice and asserting all three are identical.
        """
        adapter = EchoPacketAdapter()
        packet = _make_packet()

        result1 = run_packet_task(packet, adapter, requested_by="op")
        result2 = run_packet_task(packet, adapter, requested_by="op")

        assert result1.raw_output_text == result2.raw_output_text
        assert result1.result_id == result2.result_id

    def test_echo_rendered_prompt_is_deterministic(self):
        packet = _make_packet()
        r1 = render_prompt(packet)
        r2 = render_prompt(packet)
        assert r1 == r2

    def test_echo_raw_output_equals_rendered_prompt(self):
        adapter = EchoPacketAdapter()
        packet = _make_packet()
        rendered = render_prompt(packet)
        result = run_packet_task(packet, adapter)
        assert result.raw_output_text == rendered

    def test_echo_result_id_is_stable(self):
        adapter = EchoPacketAdapter()
        packet = _make_packet()
        r1 = run_packet_task(packet, adapter)
        r2 = run_packet_task(packet, adapter)
        assert r1.result_id == r2.result_id

    def test_echo_packet_id_propagated_to_result(self):
        adapter = EchoPacketAdapter()
        packet = _make_packet()
        result = run_packet_task(packet, adapter)
        assert result.packet_id == packet.packet_id

    def test_echo_assembly_hash_propagated_to_result(self):
        adapter = EchoPacketAdapter()
        packet = _make_packet()
        result = run_packet_task(packet, adapter)
        assert result.substrate_assembly_hash == packet.substrate_assembly_hash

    def test_echo_parse_status_is_ok_for_plain_text(self):
        adapter = EchoPacketAdapter()
        packet = _make_packet()
        result = run_packet_task(packet, adapter)
        assert result.parse_status == "ok"

    def test_different_packets_produce_different_result_ids(self):
        adapter = EchoPacketAdapter()
        p1 = _make_packet(task_prompt="Task A.")
        p2 = _make_packet(task_prompt="Task B.")
        r1 = run_packet_task(p1, adapter)
        r2 = run_packet_task(p2, adapter)
        assert r1.result_id != r2.result_id
