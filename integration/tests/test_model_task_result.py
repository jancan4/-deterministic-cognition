"""Tests for ModelTaskResult identity contracts and serialization."""
import pytest

from integration.models import (
    ModelTaskResult,
    ModelTaskResultProvenance,
    RawCandidate,
    derive_result_id,
)


def _prov():
    return ModelTaskResultProvenance(
        requested_by="operator",
        source_db_path="/tmp/t.db",
        packet_generated_at="2026-05-28T10:00:00Z",
    )


def _make_result(
    packet_id="pid1",
    adapter_target="echo",
    raw_output="some output",
    parse_status="ok",
    candidates=None,
):
    result_id = derive_result_id(packet_id, adapter_target, raw_output)
    return ModelTaskResult(
        result_id=result_id,
        task_id="tid1",
        packet_id=packet_id,
        substrate_assembly_hash="ahash",
        adapter_target=adapter_target,
        model_version="1.0.0",
        executed_at="2026-05-28T10:01:00Z",
        raw_output_text=raw_output,
        parsed_candidates=candidates or [],
        parse_status=parse_status,
        parse_error_detail=None,
        provenance=_prov(),
    )


class TestResultIdDeterminism:
    def test_result_id_is_deterministic(self):
        rid1 = derive_result_id("pid", "echo", "output text")
        rid2 = derive_result_id("pid", "echo", "output text")
        assert rid1 == rid2

    def test_result_id_differs_for_different_adapter_targets(self):
        rid1 = derive_result_id("same_pid", "echo", "same output")
        rid2 = derive_result_id("same_pid", "ollama/mistral-7b", "same output")
        assert rid1 != rid2

    def test_result_id_differs_for_different_packets(self):
        rid1 = derive_result_id("packet_A", "echo", "same output")
        rid2 = derive_result_id("packet_B", "echo", "same output")
        assert rid1 != rid2

    def test_result_id_differs_for_different_output_text(self):
        rid1 = derive_result_id("pid", "echo", "output A")
        rid2 = derive_result_id("pid", "echo", "output B")
        assert rid1 != rid2


class TestResultSerialization:
    def test_result_round_trips_json(self):
        r = _make_result()
        json_str = r.to_json()
        r2 = ModelTaskResult.from_json(json_str)
        assert r2.to_json() == json_str

    def test_result_round_trips_dict(self):
        r = _make_result()
        d = r.to_dict()
        r2 = ModelTaskResult.from_dict(d)
        assert r2.result_id == r.result_id
        assert r2.packet_id == r.packet_id
        assert r2.adapter_target == r.adapter_target

    def test_parse_error_preserves_raw_output(self):
        r = _make_result(raw_output="unparseable gibberish", parse_status="parse_error")
        assert r.raw_output_text == "unparseable gibberish"
        d = r.to_dict()
        assert d["raw_output_text"] == "unparseable gibberish"

    def test_parsed_candidates_round_trip(self):
        cands = [
            RawCandidate(0, "implementation_note", "Some content", ["tag1"], "raw"),
            RawCandidate(1, "open_question", "A question?", [], "raw2"),
        ]
        r = _make_result(candidates=cands)
        r2 = ModelTaskResult.from_json(r.to_json())
        assert len(r2.parsed_candidates) == 2
        assert r2.parsed_candidates[0].proposed_event_type == "implementation_note"
        assert r2.parsed_candidates[1].proposed_event_type == "open_question"
