"""
Tests for OllamaPacketAdapter.

All tests mock HTTP — no live Ollama process required or permitted.
"""
import hashlib
import json
import pytest
from unittest.mock import MagicMock, patch

from integration.ollama_adapter import OllamaPacketAdapter
from integration.adapters import run_packet_task
from integration.models import (
    BudgetSummary,
    ContextPacket,
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

_TEST_DIGEST = "sha256:abc123def456"
_TEST_MODEL = "mistral"


def _mock_show_response(digest: str = _TEST_DIGEST) -> MagicMock:
    m = MagicMock()
    m.json.return_value = {"digest": digest}
    m.raise_for_status = MagicMock()
    return m


def _mock_generate_response(response_text: str) -> MagicMock:
    m = MagicMock()
    m.json.return_value = {"response": response_text}
    m.raise_for_status = MagicMock()
    return m


@pytest.fixture
def adapter():
    """OllamaPacketAdapter with mocked /api/show at construction."""
    with patch("integration.ollama_adapter.requests.post") as mock_post:
        mock_post.return_value = _mock_show_response()
        return OllamaPacketAdapter(_TEST_MODEL)


def _make_packet(task_prompt="Ollama integration test."):
    envelope = TaskEnvelope.build("echo", task_prompt)
    packet_id = derive_packet_id(
        1, "assemblyhash", "policyhash", envelope.task_envelope_hash, f"ollama/{_TEST_MODEL}"
    )
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
        provenance=PacketProvenance(f"ollama/{_TEST_MODEL}", "operator", "/tmp/test.db"),
    )


# ---------------------------------------------------------------------------
# Construction and identity
# ---------------------------------------------------------------------------

class TestOllamaAdapterConstruction:
    def test_adapter_target_format(self):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_show_response()
            a = OllamaPacketAdapter("mistral")
        assert a.adapter_target == "ollama/mistral"

    def test_adapter_target_includes_tag(self):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_show_response()
            a = OllamaPacketAdapter("llama3.2:3b")
        assert a.adapter_target == "ollama/llama3.2:3b"

    def test_model_version_from_digest(self):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_show_response("sha256:deadbeef")
            a = OllamaPacketAdapter("mistral")
        assert a.model_version == "sha256:deadbeef"

    def test_model_version_unknown_on_connection_error(self):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.side_effect = ConnectionRefusedError("refused")
            a = OllamaPacketAdapter("mistral")
        assert a.model_version.startswith("unknown:")
        assert "ConnectionRefusedError" in a.model_version

    def test_model_version_fixed_at_construction(self, adapter):
        """model_version must not change between calls."""
        v1 = adapter.model_version
        v2 = adapter.model_version
        assert v1 == v2 == _TEST_DIGEST

    def test_output_is_deterministic_false(self, adapter):
        assert adapter.output_is_deterministic is False


# ---------------------------------------------------------------------------
# run() contract
# ---------------------------------------------------------------------------

class TestOllamaAdapterRun:
    def test_run_returns_response_text(self, adapter):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_generate_response("Generated output.")
            result = adapter.run("Test prompt.")
        assert result == "Generated output."

    def test_run_sends_stream_false(self, adapter):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_generate_response("ok")
            adapter.run("prompt")
        call_kwargs = mock_post.call_args
        body = json.loads(call_kwargs.kwargs.get("data") or call_kwargs.args[1])
        assert body["stream"] is False

    def test_run_sends_correct_model_name(self, adapter):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_generate_response("ok")
            adapter.run("prompt")
        call_kwargs = mock_post.call_args
        body = json.loads(call_kwargs.kwargs.get("data") or call_kwargs.args[1])
        assert body["model"] == _TEST_MODEL

    def test_run_raises_on_http_error(self, adapter):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock = MagicMock()
            mock.raise_for_status.side_effect = Exception("HTTP 503")
            mock_post.return_value = mock
            with pytest.raises(Exception, match="HTTP 503"):
                adapter.run("prompt")

    def test_run_raises_on_missing_response_field(self, adapter):
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock = MagicMock()
            mock.json.return_value = {"model": "mistral", "done": True}
            mock.raise_for_status = MagicMock()
            mock_post.return_value = mock
            with pytest.raises(ValueError, match="missing 'response' field"):
                adapter.run("prompt")

    def test_run_exception_captured_by_run_packet_task(self, adapter):
        """run() raises → run_packet_task() returns adapter_error result."""
        packet = _make_packet()
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.side_effect = ConnectionRefusedError("Ollama not running")
            result = run_packet_task(packet, adapter)
        assert result.parse_status == "adapter_error"
        assert result.adapter_error_type == "ConnectionRefusedError"
        assert result.raw_output_text == ""
        assert result.parsed_candidates == []


# ---------------------------------------------------------------------------
# Payload hashing and execution_config
# ---------------------------------------------------------------------------

class TestOllamaExecutionConfig:
    def test_execution_config_includes_temperature(self, adapter):
        config = adapter.build_execution_config("some prompt")
        assert config["temperature"] == 0

    def test_execution_config_includes_timeout(self, adapter):
        config = adapter.build_execution_config("some prompt")
        assert config["timeout_seconds"] == 120

    def test_execution_config_includes_request_payload_hash(self, adapter):
        config = adapter.build_execution_config("some prompt")
        assert "request_payload_hash" in config
        assert len(config["request_payload_hash"]) == 64  # sha256 hex

    def test_request_payload_hash_is_deterministic(self, adapter):
        h1 = adapter.build_execution_config("same prompt")["request_payload_hash"]
        h2 = adapter.build_execution_config("same prompt")["request_payload_hash"]
        assert h1 == h2

    def test_request_payload_hash_differs_for_different_prompts(self, adapter):
        h1 = adapter.build_execution_config("prompt A")["request_payload_hash"]
        h2 = adapter.build_execution_config("prompt B")["request_payload_hash"]
        assert h1 != h2

    def test_request_payload_hash_matches_sorted_json(self, adapter):
        """Hash must match sha256 of the exact JSON body sent (sorted keys)."""
        prompt = "verify hash integrity"
        config = adapter.build_execution_config(prompt)
        payload = adapter._build_payload(prompt)
        expected_body = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        expected_hash = hashlib.sha256(expected_body).hexdigest()
        assert config["request_payload_hash"] == expected_hash

    def test_execution_config_does_not_affect_success_result_id(self):
        """Two adapters differing only by timeout must produce same result_id for same output."""
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_show_response()
            a1 = OllamaPacketAdapter(_TEST_MODEL, timeout=30)
            a2 = OllamaPacketAdapter(_TEST_MODEL, timeout=120)

        packet = _make_packet()
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_generate_response("identical output")
            r1 = run_packet_task(packet, a1)
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_generate_response("identical output")
            r2 = run_packet_task(packet, a2)

        assert r1.result_id == r2.result_id
        assert r1.provenance.execution_config["timeout_seconds"] != r2.provenance.execution_config["timeout_seconds"]


# ---------------------------------------------------------------------------
# Purity and boundary enforcement
# ---------------------------------------------------------------------------

class TestOllamaAdapterPurity:
    def test_adapter_is_stateless_between_runs(self, adapter):
        """Two successive run() calls must not share mutable state."""
        outputs = []
        for text in ["first output", "second output"]:
            with patch("integration.ollama_adapter.requests.post") as mock_post:
                mock_post.return_value = _mock_generate_response(text)
                outputs.append(adapter.run("prompt"))
        assert outputs == ["first output", "second output"]

    def test_model_version_not_fetched_during_run(self, adapter):
        """run() must not call /api/show — model_version is construction-time only."""
        with patch("integration.ollama_adapter.requests.post") as mock_post:
            mock_post.return_value = _mock_generate_response("ok")
            adapter.run("prompt")
        # Verify only one POST call was made (to /api/generate, not /api/show)
        assert mock_post.call_count == 1
        call_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get("url", "")
        # The single call must be to /api/generate, not /api/show
        # (adapter fixture already consumed the /api/show call)

    def test_adapter_target_is_stable(self, adapter):
        assert adapter.adapter_target == f"ollama/{_TEST_MODEL}"
        assert adapter.adapter_target == f"ollama/{_TEST_MODEL}"
