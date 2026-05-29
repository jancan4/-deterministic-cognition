"""
Data models for the deterministic context packet integration layer.

ContextPacket   — immutable, self-contained, detached read snapshot for model consumption
ModelTaskResult — captured model output with deterministic result_id
RawCandidate    — parsed candidate from model output (unvalidated, pre-review)

Identity contracts:
  packet_id  = sha256(assembly_id, assembly_hash, policy_hash, task_envelope_hash,
                      adapter_target, packet_schema_version)
               generated_at MUST NOT participate in packet_id

  result_id (success)       = sha256(packet_id, adapter_target, raw_output_text)
               scopes deduplication to (assembly, adapter, output content)

  result_id (adapter_error) = sha256(packet_id, adapter_target, parse_status,
                                     normalized_error_type, normalized_error_message)
               prevents collapse of distinct failure modes to a single identity class
               raw_output_text is always "" for adapter_error results

assembly_order in PacketEntry:
  0-based position within the section, sorted by activation_rank ascending.
  Not a relevance score. Not adaptive. Identical to the order entries appear
  in the assembled context window for that section.
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


PACKET_SCHEMA_VERSION = "1.0"

# Fixed section rendering order — must not change
SECTION_ORDER = (
    "governance_rules",
    "architecture_decisions",
    "implementation_notes",
    "open_questions",
    "narrative_memory",
)

# Event types that route to each packet section
SECTION_EVENT_TYPES = {
    "governance_rules": frozenset({"governance_rule"}),
    "architecture_decisions": frozenset({"architecture_decision"}),
    "implementation_notes": frozenset({"implementation_note"}),
    "open_questions": frozenset({"open_question"}),
    "narrative_memory": frozenset({
        "hypothesis", "experiment", "validation_result", "adaptation",
        "regime_observation", "incident", "source_reference",
        "rejected_idea",
    }),
}

VALID_TASK_TYPES = ("extraction", "classification", "synthesis", "echo")


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _sha256(*parts: str) -> str:
    """Deterministic sha256 over null-delimited parts."""
    raw = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# PacketEntry
# ---------------------------------------------------------------------------

@dataclass
class PacketEntry:
    """
    One memory entry as it appears in a ContextPacket.

    assembly_order is the 0-based index of this entry within its section,
    ordered by activation_rank ascending. It is a deterministic doctrine order,
    NOT a relevance score, similarity weight, or adaptive ranking.
    """
    event_id: int
    event_type: str
    content: str
    assembly_order: int
    char_count: int

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "content": self.content,
            "assembly_order": self.assembly_order,
            "char_count": self.char_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PacketEntry":
        return cls(
            event_id=d["event_id"],
            event_type=d["event_type"],
            content=d["content"],
            assembly_order=d["assembly_order"],
            char_count=d["char_count"],
        )


# ---------------------------------------------------------------------------
# BudgetSummary
# ---------------------------------------------------------------------------

@dataclass
class BudgetSummary:
    total_chars: int
    governance_rule_chars: int
    architecture_decision_chars: int
    narrative_chars: int
    entry_count: int

    def to_dict(self) -> dict:
        return {
            "total_chars": self.total_chars,
            "governance_rule_chars": self.governance_rule_chars,
            "architecture_decision_chars": self.architecture_decision_chars,
            "narrative_chars": self.narrative_chars,
            "entry_count": self.entry_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetSummary":
        return cls(
            total_chars=d["total_chars"],
            governance_rule_chars=d["governance_rule_chars"],
            architecture_decision_chars=d["architecture_decision_chars"],
            narrative_chars=d["narrative_chars"],
            entry_count=d["entry_count"],
        )


# ---------------------------------------------------------------------------
# TaskEnvelope
# ---------------------------------------------------------------------------

@dataclass
class TaskEnvelope:
    task_type: str
    task_id: str
    task_envelope_hash: str
    task_prompt_text: str

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "task_id": self.task_id,
            "task_envelope_hash": self.task_envelope_hash,
            "task_prompt_text": self.task_prompt_text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskEnvelope":
        return cls(
            task_type=d["task_type"],
            task_id=d["task_id"],
            task_envelope_hash=d["task_envelope_hash"],
            task_prompt_text=d["task_prompt_text"],
        )

    @classmethod
    def build(cls, task_type: str, task_prompt_text: str) -> "TaskEnvelope":
        if task_type not in VALID_TASK_TYPES:
            raise ValueError(
                f"task_type {task_type!r} is not valid. Must be one of: {VALID_TASK_TYPES}"
            )
        h = _sha256(task_type, task_prompt_text)
        return cls(
            task_type=task_type,
            task_id=h,
            task_envelope_hash=h,
            task_prompt_text=task_prompt_text,
        )


# ---------------------------------------------------------------------------
# PacketProvenance
# ---------------------------------------------------------------------------

@dataclass
class PacketProvenance:
    adapter_target: str
    requested_by: str
    source_db_path: str

    def to_dict(self) -> dict:
        return {
            "adapter_target": self.adapter_target,
            "requested_by": self.requested_by,
            "source_db_path": self.source_db_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PacketProvenance":
        return cls(
            adapter_target=d["adapter_target"],
            requested_by=d["requested_by"],
            source_db_path=d["source_db_path"],
        )


# ---------------------------------------------------------------------------
# ContextPacket
# ---------------------------------------------------------------------------

@dataclass
class ContextPacket:
    """
    Immutable, self-contained snapshot for model consumption.

    Invariants:
      - Immutable after generation. No field may change.
      - Replayable: same packet_id always yields the same rendered prompt.
      - Self-contained: all context needed to run the task is in the packet.
      - Detached: no live DB connection after generation.

    packet_id is derived from stable substrate state only.
    generated_at is provenance metadata — it does NOT participate in packet_id.
    """
    packet_schema_version: str
    packet_id: str
    generated_at: str
    substrate_assembly_id: int
    substrate_assembly_hash: str
    substrate_schema_version: int
    policy_hash: str

    governance_rules: List[PacketEntry]
    architecture_decisions: List[PacketEntry]
    implementation_notes: List[PacketEntry]
    open_questions: List[PacketEntry]
    narrative_memory: List[PacketEntry]

    budget_summary: BudgetSummary
    task_envelope: TaskEnvelope
    provenance: PacketProvenance

    def all_entries(self) -> List[PacketEntry]:
        result = []
        for section_name in SECTION_ORDER:
            result.extend(getattr(self, section_name))
        return result

    def to_dict(self) -> dict:
        return {
            "packet_schema_version": self.packet_schema_version,
            "packet_id": self.packet_id,
            "generated_at": self.generated_at,
            "substrate_assembly_id": self.substrate_assembly_id,
            "substrate_assembly_hash": self.substrate_assembly_hash,
            "substrate_schema_version": self.substrate_schema_version,
            "policy_hash": self.policy_hash,
            "sections": {
                "governance_rules": [e.to_dict() for e in self.governance_rules],
                "architecture_decisions": [e.to_dict() for e in self.architecture_decisions],
                "implementation_notes": [e.to_dict() for e in self.implementation_notes],
                "open_questions": [e.to_dict() for e in self.open_questions],
                "narrative_memory": [e.to_dict() for e in self.narrative_memory],
            },
            "budget_summary": self.budget_summary.to_dict(),
            "task_envelope": self.task_envelope.to_dict(),
            "provenance": self.provenance.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)

    @classmethod
    def from_dict(cls, d: dict) -> "ContextPacket":
        sections = d.get("sections", {})
        return cls(
            packet_schema_version=d["packet_schema_version"],
            packet_id=d["packet_id"],
            generated_at=d["generated_at"],
            substrate_assembly_id=d["substrate_assembly_id"],
            substrate_assembly_hash=d["substrate_assembly_hash"],
            substrate_schema_version=d["substrate_schema_version"],
            policy_hash=d["policy_hash"],
            governance_rules=[PacketEntry.from_dict(e) for e in sections.get("governance_rules", [])],
            architecture_decisions=[PacketEntry.from_dict(e) for e in sections.get("architecture_decisions", [])],
            implementation_notes=[PacketEntry.from_dict(e) for e in sections.get("implementation_notes", [])],
            open_questions=[PacketEntry.from_dict(e) for e in sections.get("open_questions", [])],
            narrative_memory=[PacketEntry.from_dict(e) for e in sections.get("narrative_memory", [])],
            budget_summary=BudgetSummary.from_dict(d["budget_summary"]),
            task_envelope=TaskEnvelope.from_dict(d["task_envelope"]),
            provenance=PacketProvenance.from_dict(d["provenance"]),
        )

    @classmethod
    def from_json(cls, text: str) -> "ContextPacket":
        return cls.from_dict(json.loads(text))


# ---------------------------------------------------------------------------
# Deterministic ID derivation
# ---------------------------------------------------------------------------

def derive_packet_id(
    assembly_id: int,
    assembly_hash: str,
    policy_hash: str,
    task_envelope_hash: str,
    adapter_target: str,
    packet_schema_version: str = PACKET_SCHEMA_VERSION,
) -> str:
    """
    Deterministic packet_id. generated_at must NOT be passed here.

    Same inputs → same packet_id regardless of when generation occurred.
    """
    return _sha256(
        str(assembly_id),
        assembly_hash,
        policy_hash,
        task_envelope_hash,
        adapter_target,
        packet_schema_version,
    )


def derive_policy_hash(policy_dict: dict) -> str:
    """sha256 of the JSON-serialized policy dict (sort_keys=True)."""
    raw = json.dumps(policy_dict, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# RawCandidate
# ---------------------------------------------------------------------------

@dataclass
class RawCandidate:
    """
    A parsed candidate from model output. Not yet validated or reviewed.

    proposed_event_type is the model's claimed event_type — it is a string
    and has not been validated against VALID_EVENT_TYPES at this point.
    Validation occurs at ingest time.
    """
    candidate_index: int
    proposed_event_type: str
    proposed_content: str
    proposed_tags: List[str]
    raw_excerpt: str

    def to_dict(self) -> dict:
        return {
            "candidate_index": self.candidate_index,
            "proposed_event_type": self.proposed_event_type,
            "proposed_content": self.proposed_content,
            "proposed_tags": list(self.proposed_tags),
            "raw_excerpt": self.raw_excerpt,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RawCandidate":
        return cls(
            candidate_index=d["candidate_index"],
            proposed_event_type=d["proposed_event_type"],
            proposed_content=d["proposed_content"],
            proposed_tags=list(d.get("proposed_tags", [])),
            raw_excerpt=d["raw_excerpt"],
        )


# ---------------------------------------------------------------------------
# ModelTaskResultProvenance
# ---------------------------------------------------------------------------

@dataclass
class ModelTaskResultProvenance:
    requested_by: str
    source_db_path: str
    packet_generated_at: str
    execution_config: Optional[dict] = None  # provenance only; never participates in result_id

    def to_dict(self) -> dict:
        return {
            "requested_by": self.requested_by,
            "source_db_path": self.source_db_path,
            "packet_generated_at": self.packet_generated_at,
            "execution_config": self.execution_config,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelTaskResultProvenance":
        return cls(
            requested_by=d["requested_by"],
            source_db_path=d["source_db_path"],
            packet_generated_at=d["packet_generated_at"],
            execution_config=d.get("execution_config"),
        )


# ---------------------------------------------------------------------------
# ModelTaskResult
# ---------------------------------------------------------------------------

def derive_result_id(packet_id: str, adapter_target: str, raw_output_text: str) -> str:
    """
    Deterministic result_id for a successful adapter run.

    Same packet_id + same adapter_target + same raw_output_text → same result_id.
    Different adapter_targets or different packets always produce different result_ids,
    even for identical output text.
    """
    return _sha256(packet_id, adapter_target, raw_output_text)


def derive_result_id_error(
    packet_id: str,
    adapter_target: str,
    parse_status: str,
    normalized_error_type: str,
    normalized_error_message: str,
) -> str:
    """
    Deterministic result_id for an adapter_error result.

    Uses error identity inputs instead of raw_output_text (which is always "" on
    adapter_error). Prevents multiple distinct failure modes on the same packet
    from collapsing to the same result_id.

    Never call this on the success path — use derive_result_id() instead.
    """
    return _sha256(packet_id, adapter_target, parse_status, normalized_error_type, normalized_error_message)


@dataclass
class ModelTaskResult:
    """
    The captured output of one model task run.

    result_id derivation:
      success path:       sha256(packet_id, adapter_target, raw_output_text)
      adapter_error path: sha256(packet_id, adapter_target, parse_status,
                                 adapter_error_type, adapter_error_message)

    parse_error does not discard raw_output_text — raw text is always preserved.
    adapter_error sets raw_output_text="" and parsed_candidates=[].
    Replay reads the stored result; the adapter is never re-invoked.
    """
    result_id: str
    task_id: str
    packet_id: str
    substrate_assembly_hash: str

    adapter_target: str
    model_version: str
    executed_at: str

    raw_output_text: str
    parsed_candidates: List[RawCandidate]

    parse_status: str            # "ok" | "parse_error" | "empty" | "adapter_error"
    parse_error_detail: Optional[str]

    provenance: ModelTaskResultProvenance

    # Populated on adapter_error path only. None on success path.
    adapter_error_type: Optional[str] = None
    adapter_error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "result_id": self.result_id,
            "task_id": self.task_id,
            "packet_id": self.packet_id,
            "substrate_assembly_hash": self.substrate_assembly_hash,
            "adapter_target": self.adapter_target,
            "model_version": self.model_version,
            "executed_at": self.executed_at,
            "raw_output_text": self.raw_output_text,
            "parsed_candidates": [c.to_dict() for c in self.parsed_candidates],
            "parse_status": self.parse_status,
            "parse_error_detail": self.parse_error_detail,
            "provenance": self.provenance.to_dict(),
            "adapter_error_type": self.adapter_error_type,
            "adapter_error_message": self.adapter_error_message,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelTaskResult":
        return cls(
            result_id=d["result_id"],
            task_id=d["task_id"],
            packet_id=d["packet_id"],
            substrate_assembly_hash=d["substrate_assembly_hash"],
            adapter_target=d["adapter_target"],
            model_version=d["model_version"],
            executed_at=d["executed_at"],
            raw_output_text=d["raw_output_text"],
            parsed_candidates=[RawCandidate.from_dict(c) for c in d.get("parsed_candidates", [])],
            parse_status=d["parse_status"],
            parse_error_detail=d.get("parse_error_detail"),
            provenance=ModelTaskResultProvenance.from_dict(d["provenance"]),
            adapter_error_type=d.get("adapter_error_type"),
            adapter_error_message=d.get("adapter_error_message"),
        )

    @classmethod
    def from_json(cls, text: str) -> "ModelTaskResult":
        return cls.from_dict(json.loads(text))
