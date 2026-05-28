"""
Integration-layer packet adapters.

PacketAdapterBase — abstract base for all packet-level adapters.
EchoPacketAdapter — returns the rendered prompt verbatim. Deterministic.

These adapters operate on rendered prompt strings, not LocalModelRequest
objects. They have no DB access and produce no side effects.

run_packet_task() is the single entry point for executing a model task:
  ContextPacket → render → adapter.run() → ModelTaskResult

No DB is queried inside run_packet_task(). The packet is self-contained.
"""
import re
import json
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from .models import (
    ModelTaskResult,
    ModelTaskResultProvenance,
    RawCandidate,
    _now,
    derive_result_id,
)
from .renderer import render_prompt


class PacketAdapterBase(ABC):
    """
    Abstract base for integration-layer model adapters.

    Subclasses must implement:
      adapter_target  — stable identifier string (e.g. "echo", "ollama/mistral-7b")
      model_version   — version string; "unknown" is permitted
      run()           — synchronous, side-effect-free execution

    Contracts:
      - run() must not query any database.
      - run() must not write to any database.
      - run() must not make network calls (except OllamaAdapter, future).
      - run() must not mutate the packet.
    """

    @property
    @abstractmethod
    def adapter_target(self) -> str:
        """Stable adapter identifier."""

    @property
    @abstractmethod
    def model_version(self) -> str:
        """Model version string."""

    @abstractmethod
    def run(self, rendered_prompt: str) -> str:
        """
        Execute on the rendered prompt text. Returns raw output text.
        Must be a pure function of the input for deterministic adapters.
        """


class EchoPacketAdapter(PacketAdapterBase):
    """
    Returns the rendered prompt verbatim as raw_output_text.

    Deterministic: same rendered prompt → same raw_output_text → same result_id.
    Used for integration smoke tests and replay determinism verification.
    """

    @property
    def adapter_target(self) -> str:
        return "echo"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def run(self, rendered_prompt: str) -> str:
        return rendered_prompt


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def _parse_candidates(raw_output: str) -> Tuple[List[RawCandidate], str, Optional[str]]:
    """
    Attempt to parse structured candidates from raw model output.

    Tries:
      1. JSON array inside a ```json ... ``` code block.
      2. Bare JSON array at the start of output.
      3. No structured output — returns empty candidates with status="ok".

    A parse failure preserves raw_output_text (never discards it).
    Returns (candidates, parse_status, parse_error_detail).
    """
    if not raw_output or not raw_output.strip():
        return [], "empty", None

    json_block_re = re.compile(r'```(?:json)?\s*(\[.*?\])\s*```', re.DOTALL)
    match = json_block_re.search(raw_output)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list):
                return _build_candidates(data), "ok", None
        except json.JSONDecodeError as exc:
            return [], "parse_error", f"JSON block parse failed: {exc}"

    stripped = raw_output.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, list):
                return _build_candidates(data), "ok", None
        except json.JSONDecodeError:
            pass

    return [], "ok", None


def _build_candidates(data: list) -> List[RawCandidate]:
    candidates = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        candidates.append(RawCandidate(
            candidate_index=i,
            proposed_event_type=str(item.get("event_type", "")),
            proposed_content=str(item.get("content", "")),
            proposed_tags=list(item.get("tags", [])),
            raw_excerpt=str(item)[:500],
        ))
    return candidates


# ---------------------------------------------------------------------------
# run_packet_task — single entry point
# ---------------------------------------------------------------------------

def run_packet_task(
    packet,
    adapter: PacketAdapterBase,
    requested_by: str = "cli",
) -> ModelTaskResult:
    """
    Execute a model task for the given ContextPacket using adapter.

    Does not query any DB. Does not write to any DB.
    The packet is self-contained — all context is in the packet file.

    Steps:
      1. render_prompt(packet) — pure deterministic transform
      2. adapter.run(rendered_prompt) — model call
      3. _parse_candidates(raw_output) — structured parse attempt
      4. Build ModelTaskResult with deterministic result_id
    """
    rendered = render_prompt(packet)
    raw_output = adapter.run(rendered)
    result_id = derive_result_id(packet.packet_id, adapter.adapter_target, raw_output)
    candidates, parse_status, parse_error = _parse_candidates(raw_output)

    return ModelTaskResult(
        result_id=result_id,
        task_id=packet.task_envelope.task_id,
        packet_id=packet.packet_id,
        substrate_assembly_hash=packet.substrate_assembly_hash,
        adapter_target=adapter.adapter_target,
        model_version=adapter.model_version,
        executed_at=_now(),
        raw_output_text=raw_output,
        parsed_candidates=candidates,
        parse_status=parse_status,
        parse_error_detail=parse_error,
        provenance=ModelTaskResultProvenance(
            requested_by=requested_by,
            source_db_path=packet.provenance.source_db_path,
            packet_generated_at=packet.generated_at,
        ),
    )
