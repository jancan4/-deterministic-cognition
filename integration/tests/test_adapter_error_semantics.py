"""
Tests for adapter error capture, error-path result_id derivation, and replay doctrine.

Covers:
  - adapter_error result_id uniqueness across distinct failure modes
  - adapter_error result_id stability for repeated identical failures
  - raw_output_text and parsed_candidates invariants on adapter_error
  - no collision between adapter_error result_id and success empty-output result_id
  - run_packet_task catches all adapter exceptions
  - execution_config does not affect result_id on any path
  - adapter_error round-trip serialization
  - replay doctrine: stored error results load without adapter invocation
"""
import pytest

from integration.adapters import EchoPacketAdapter, PacketAdapterBase, run_packet_task
from integration.models import (
    BudgetSummary,
    ContextPacket,
    ModelTaskResult,
    PacketEntry,
    PacketProvenance,
    PACKET_SCHEMA_VERSION,
    TaskEnvelope,
    derive_packet_id,
    derive_result_id,
    derive_result_id_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_packet(task_prompt="Error semantics test."):
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
        governance_rules=[PacketEntry(1, "governance_rule", "Rule: stay governed.", 0, 22)],
        architecture_decisions=[],
        implementation_notes=[],
        open_questions=[],
        narrative_memory=[],
        budget_summary=BudgetSummary(22, 22, 0, 0, 1),
        task_envelope=envelope,
        provenance=PacketProvenance("echo", "operator", "/tmp/test.db"),
    )


class _RaisingAdapter(PacketAdapterBase):
    """Test double that raises a configurable exception on run()."""

    def __init__(self, exc: Exception, target: str = "test/raising"):
        self._exc = exc
        self._target = target
        self._call_count = 0

    @property
    def adapter_target(self) -> str:
        return self._target

    @property
    def model_version(self) -> str:
        return "test"

    def run(self, rendered_prompt: str) -> str:
        self._call_count += 1
        raise self._exc


class _SilentEmptyAdapter(PacketAdapterBase):
    """Returns empty string — degenerate but valid success path."""

    @property
    def adapter_target(self) -> str:
        return "test/empty"

    @property
    def model_version(self) -> str:
        return "test"

    def run(self, rendered_prompt: str) -> str:
        return ""


# ---------------------------------------------------------------------------
# derive_result_id_error unit tests
# ---------------------------------------------------------------------------

class TestDeriveResultIdError:
    def test_error_result_id_is_deterministic(self):
        rid1 = derive_result_id_error("pid", "tgt", "adapter_error", "TimeoutError", "timed out")
        rid2 = derive_result_id_error("pid", "tgt", "adapter_error", "TimeoutError", "timed out")
        assert rid1 == rid2

    def test_error_result_id_differs_for_different_error_types(self):
        rid1 = derive_result_id_error("pid", "tgt", "adapter_error", "ConnectionRefusedError", "msg")
        rid2 = derive_result_id_error("pid", "tgt", "adapter_error", "ReadTimeout", "msg")
        assert rid1 != rid2

    def test_error_result_id_differs_for_different_error_messages(self):
        rid1 = derive_result_id_error("pid", "tgt", "adapter_error", "ValueError", "error A")
        rid2 = derive_result_id_error("pid", "tgt", "adapter_error", "ValueError", "error B")
        assert rid1 != rid2

    def test_error_result_id_differs_for_different_packets(self):
        rid1 = derive_result_id_error("packet_A", "tgt", "adapter_error", "Err", "msg")
        rid2 = derive_result_id_error("packet_B", "tgt", "adapter_error", "Err", "msg")
        assert rid1 != rid2

    def test_error_result_id_differs_for_different_adapter_targets(self):
        rid1 = derive_result_id_error("pid", "ollama/mistral", "adapter_error", "Err", "msg")
        rid2 = derive_result_id_error("pid", "ollama/llama3", "adapter_error", "Err", "msg")
        assert rid1 != rid2


# ---------------------------------------------------------------------------
# Adapter error capture via run_packet_task
# ---------------------------------------------------------------------------

class TestAdapterErrorCapture:
    def test_run_packet_task_catches_connection_refused(self):
        adapter = _RaisingAdapter(ConnectionRefusedError("Connection refused by peer"))
        packet = _make_packet()
        result = run_packet_task(packet, adapter)
        assert result.parse_status == "adapter_error"

    def test_adapter_error_raw_output_is_empty_string(self):
        adapter = _RaisingAdapter(RuntimeError("boom"))
        result = run_packet_task(_make_packet(), adapter)
        assert result.raw_output_text == ""

    def test_adapter_error_parsed_candidates_is_empty(self):
        adapter = _RaisingAdapter(OSError("failure"))
        result = run_packet_task(_make_packet(), adapter)
        assert result.parsed_candidates == []

    def test_adapter_error_type_is_exception_class_name(self):
        adapter = _RaisingAdapter(TimeoutError("took too long"))
        result = run_packet_task(_make_packet(), adapter)
        assert result.adapter_error_type == "TimeoutError"

    def test_adapter_error_message_is_truncated_string(self):
        adapter = _RaisingAdapter(ValueError("specific failure detail"))
        result = run_packet_task(_make_packet(), adapter)
        assert result.adapter_error_message is not None
        assert "specific failure detail" in result.adapter_error_message
        assert len(result.adapter_error_message) <= 256

    def test_adapter_error_type_none_on_success(self):
        adapter = EchoPacketAdapter()
        result = run_packet_task(_make_packet(), adapter)
        assert result.parse_status == "ok"
        assert result.adapter_error_type is None

    def test_adapter_error_message_none_on_success(self):
        adapter = EchoPacketAdapter()
        result = run_packet_task(_make_packet(), adapter)
        assert result.adapter_error_message is None


# ---------------------------------------------------------------------------
# result_id collision prevention
# ---------------------------------------------------------------------------

class TestResultIdCollisionPrevention:
    def test_two_distinct_error_types_produce_different_result_ids(self):
        packet = _make_packet()
        r1 = run_packet_task(packet, _RaisingAdapter(ConnectionRefusedError("refused"), "t/a"))
        r2 = run_packet_task(packet, _RaisingAdapter(TimeoutError("timeout"), "t/a"))
        assert r1.result_id != r2.result_id

    def test_same_error_type_and_message_produces_same_result_id(self):
        packet = _make_packet()
        exc = ConnectionRefusedError("Connection refused")
        r1 = run_packet_task(packet, _RaisingAdapter(exc, "t/a"))
        r2 = run_packet_task(packet, _RaisingAdapter(exc, "t/a"))
        assert r1.result_id == r2.result_id

    def test_adapter_error_does_not_collide_with_success_empty_output(self):
        """
        An adapter_error (raw_output="") must NOT have the same result_id
        as a degenerate success result with raw_output_text="".
        The error path hashes error metadata; the success path hashes the empty string.
        """
        packet = _make_packet()
        error_result = run_packet_task(packet, _RaisingAdapter(RuntimeError("fail"), "test/x"))
        success_result = run_packet_task(packet, _SilentEmptyAdapter())

        assert error_result.raw_output_text == ""
        assert success_result.raw_output_text == ""
        assert error_result.result_id != success_result.result_id

    def test_adapter_error_result_id_uses_error_derivation_not_success_derivation(self):
        packet = _make_packet()
        adapter = _RaisingAdapter(ValueError("test error"), "test/y")
        result = run_packet_task(packet, adapter)

        # Verify by re-deriving with the error function
        expected = derive_result_id_error(
            packet.packet_id,
            "test/y",
            "adapter_error",
            result.adapter_error_type,
            result.adapter_error_message,
        )
        assert result.result_id == expected

        # Verify it does NOT match the success derivation with empty string
        not_expected = derive_result_id(packet.packet_id, "test/y", "")
        assert result.result_id != not_expected


# ---------------------------------------------------------------------------
# execution_config does not affect result_id
# ---------------------------------------------------------------------------

class TestExecutionConfigIsolation:
    def test_execution_config_does_not_affect_success_result_id(self):
        """Two adapters with different execution_config but same run() output must have same result_id."""

        class ConfiguredAdapter(PacketAdapterBase):
            def __init__(self, config_value: str):
                self._config_value = config_value

            @property
            def adapter_target(self) -> str:
                return "test/configured"

            @property
            def model_version(self) -> str:
                return "1.0"

            def run(self, rendered_prompt: str) -> str:
                return "same output"

            def build_execution_config(self, rendered_prompt: str):
                return {"config": self._config_value}

        packet = _make_packet()
        r1 = run_packet_task(packet, ConfiguredAdapter("alpha"))
        r2 = run_packet_task(packet, ConfiguredAdapter("beta"))

        assert r1.result_id == r2.result_id
        assert r1.provenance.execution_config != r2.provenance.execution_config

    def test_execution_config_stored_in_provenance(self):
        class ConfiguredAdapter(PacketAdapterBase):
            @property
            def adapter_target(self) -> str:
                return "test/cfg"

            @property
            def model_version(self) -> str:
                return "1.0"

            def run(self, rendered_prompt: str) -> str:
                return "output"

            def build_execution_config(self, rendered_prompt: str):
                return {"temperature": 0, "seed": None}

        result = run_packet_task(_make_packet(), ConfiguredAdapter())
        assert result.provenance.execution_config == {"temperature": 0, "seed": None}

    def test_execution_config_none_for_echo_adapter(self):
        result = run_packet_task(_make_packet(), EchoPacketAdapter())
        assert result.provenance.execution_config is None


# ---------------------------------------------------------------------------
# Serialization round-trip for adapter_error results
# ---------------------------------------------------------------------------

class TestAdapterErrorSerialization:
    def test_adapter_error_round_trips_json(self):
        adapter = _RaisingAdapter(ConnectionRefusedError("refused"), "test/z")
        result = run_packet_task(_make_packet(), adapter)
        assert result.parse_status == "adapter_error"

        json_str = result.to_json()
        loaded = ModelTaskResult.from_json(json_str)

        assert loaded.result_id == result.result_id
        assert loaded.parse_status == "adapter_error"
        assert loaded.raw_output_text == ""
        assert loaded.parsed_candidates == []
        assert loaded.adapter_error_type == result.adapter_error_type
        assert loaded.adapter_error_message == result.adapter_error_message

    def test_adapter_error_json_contains_explicit_null_fields(self):
        """Ensure null fields are explicit in JSON, not omitted."""
        import json
        adapter = _RaisingAdapter(RuntimeError("r"), "test/null")
        result = run_packet_task(_make_packet(), adapter)
        d = result.to_dict()
        assert "adapter_error_type" in d
        assert "adapter_error_message" in d
        # Success fields that are null on error path
        assert d["parse_error_detail"] is None

    def test_success_result_adapter_error_fields_are_null_in_json(self):
        result = run_packet_task(_make_packet(), EchoPacketAdapter())
        d = result.to_dict()
        assert d["adapter_error_type"] is None
        assert d["adapter_error_message"] is None


# ---------------------------------------------------------------------------
# Replay doctrine
# ---------------------------------------------------------------------------

class TestReplayDoctrine:
    def test_replay_uses_stored_adapter_error_without_reexecution(self):
        """
        Replay doctrine: adapter_error results are loaded from stored JSON.
        No adapter is invoked during replay. No HTTP is attempted.

        Simulate: a raising adapter produced an error result in a prior session.
        On replay, the stored JSON is loaded — the adapter is never called.
        """
        adapter = _RaisingAdapter(ConnectionRefusedError("refused"), "test/replay")
        packet = _make_packet()

        # First execution — captures the error
        result = run_packet_task(packet, adapter)
        assert result.parse_status == "adapter_error"
        assert adapter._call_count == 1

        # Serialize (simulate writing to disk)
        stored_json = result.to_json()

        # Replay: deserialize the stored result — adapter is NOT called
        replayed = ModelTaskResult.from_json(stored_json)

        assert adapter._call_count == 1, "adapter.run() must not be called during replay"
        assert replayed.result_id == result.result_id
        assert replayed.parse_status == "adapter_error"
        assert replayed.raw_output_text == ""
        assert replayed.adapter_error_type == result.adapter_error_type
        assert replayed.adapter_error_message == result.adapter_error_message

    def test_replay_preserves_packet_id_linkage(self):
        """Replayed error result retains its association to the originating packet."""
        packet = _make_packet()
        adapter = _RaisingAdapter(TimeoutError("timeout"), "test/link")
        result = run_packet_task(packet, adapter)
        replayed = ModelTaskResult.from_json(result.to_json())
        assert replayed.packet_id == packet.packet_id
        assert replayed.substrate_assembly_hash == packet.substrate_assembly_hash
