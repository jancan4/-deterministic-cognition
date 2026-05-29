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
    derive_result_id_error,
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
      - run() must not mutate the packet.
      - run() raises on failure; run_packet_task() catches all exceptions.
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
        Raises on any failure — never swallows exceptions.
        """

    def build_execution_config(self, rendered_prompt: str) -> Optional[dict]:
        """
        Return adapter-specific execution metadata for provenance.

        Provenance only — never participates in result_id on any path.
        Return None if not applicable (e.g. EchoPacketAdapter).
        """
        return None


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
# Error normalization
# ---------------------------------------------------------------------------

def _normalize_error_message(exc: BaseException) -> str:
    """
    Produce a stable, deterministic error message for result_id derivation.

    Strips memory addresses (0x...) which vary per process invocation.
    Truncates to 256 characters. Encodes to UTF-8 safely.
    The goal is to make the same logical error produce the same string
    across repeated invocations on the same system.
    """
    raw = str(exc)
    raw = re.sub(r'\b0x[0-9a-fA-F]{4,}\b', '0xADDR', raw)
    return raw.encode('utf-8', errors='replace').decode('utf-8')[:256]


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
    Always returns a ModelTaskResult — never propagates adapter exceptions.

    Steps:
      1. render_prompt(packet) — pure deterministic transform
      2. adapter.build_execution_config() — provenance metadata (pre-run)
      3. adapter.run(rendered_prompt) — model call; caught if it raises
      4. _parse_candidates(raw_output) — structured parse attempt
      5. Build ModelTaskResult with deterministic result_id

    On adapter_error:
      result_id = sha256(packet_id, adapter_target, parse_status,
                         normalized_error_type, normalized_error_message)
      raw_output_text = ""
      parsed_candidates = []
    """
    rendered = render_prompt(packet)
    execution_config = adapter.build_execution_config(rendered)

    try:
        raw_output = adapter.run(rendered)
    except Exception as exc:
        norm_type = type(exc).__name__
        norm_msg = _normalize_error_message(exc)
        result_id = derive_result_id_error(
            packet.packet_id,
            adapter.adapter_target,
            "adapter_error",
            norm_type,
            norm_msg,
        )
        return ModelTaskResult(
            result_id=result_id,
            task_id=packet.task_envelope.task_id,
            packet_id=packet.packet_id,
            substrate_assembly_hash=packet.substrate_assembly_hash,
            adapter_target=adapter.adapter_target,
            model_version=adapter.model_version,
            executed_at=_now(),
            raw_output_text="",
            parsed_candidates=[],
            parse_status="adapter_error",
            parse_error_detail=None,
            provenance=ModelTaskResultProvenance(
                requested_by=requested_by,
                source_db_path=packet.provenance.source_db_path,
                packet_generated_at=packet.generated_at,
                execution_config=execution_config,
            ),
            adapter_error_type=norm_type,
            adapter_error_message=norm_msg,
        )

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
            execution_config=execution_config,
        ),
        adapter_error_type=None,
        adapter_error_message=None,
    )
