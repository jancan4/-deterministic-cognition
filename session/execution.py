"""
Activation policy execution substrate (Phase 8B-core).

execute_activation_policy() orchestrates the full pipeline:
  evaluate_trigger() → reconstruct() → log_assembly() →
  log_assembly_transition() [optional, soft-fail] → log_activation_decision()

Invariants
----------
- evaluate_trigger() is called unchanged — pure, no I/O.
- No writes to memory_events, memory_revisions, memory_links,
  confidence_revisions, event_embeddings, ontology_*, or activation_policies.
- resulting_retrieval_id is always NULL — retrieval is embedded in reconstruct().
- If assembly creation succeeds but session transition logging fails, the
  decision is still logged with resulting_assembly_id populated and
  resulting_transition_id=None. PolicyExecutionResult.transition_error carries
  the failure description. The caller is informed but no exception is raised.
- Hard-fails (raises) before decision logging only when:
    - policy_id not found
    - trigger_event is not a dict
    - reconstruct() or log_assembly() fails
    - log_activation_decision() fails
- Replay semantics are unchanged: replay_activation_decision() and
  replay_assembly() operate independently of this module.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .activation_policy import (
    ActivationPolicy,
    ActivationTriggerResult,
    evaluate_trigger,
    get_activation_policy,
    log_activation_decision,
)
from .models import ContextActivationPolicy


@dataclass
class PolicyExecutionResult:
    """
    Result of one execute_activation_policy() call.

    fired=True: policy conditions met; assembly created and decision logged.
    fired=False: conditions not met; no assembly created.

    transition_error is non-None when assembly succeeded but session transition
    logging failed. The decision is logged in that case with resulting_assembly_id
    populated and resulting_transition_id=None. No exception is raised.

    decision_id is None only when log_non_firing=False and fired=False.
    """
    policy_id: int
    decision_id: Optional[int]
    fired: bool
    detection_reason: str
    resulting_assembly_id: Optional[int]
    resulting_transition_id: Optional[int]
    triggering_artifact_ids: List[int] = field(default_factory=list)
    transition_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'policy_id': self.policy_id,
            'decision_id': self.decision_id,
            'fired': self.fired,
            'detection_reason': self.detection_reason,
            'resulting_assembly_id': self.resulting_assembly_id,
            'resulting_transition_id': self.resulting_transition_id,
            'triggering_artifact_ids': list(self.triggering_artifact_ids),
            'transition_error': self.transition_error,
        }


def execute_activation_policy(
    db_path: str,
    policy_id: int,
    trigger_event: dict,
    context_policy: Optional[ContextActivationPolicy] = None,
    *,
    cognition_session_id: Optional[int] = None,
    triggered_by: str,
    transition_reason: str = '',
    log_non_firing: bool = False,
) -> PolicyExecutionResult:
    """
    Evaluate and execute one activation policy.

    Execution steps when fired:
      1. Fetch policy — raises ValueError if not found (hard-fail).
      2. evaluate_trigger() — pure, no I/O.
      3. reconstruct() + log_assembly() — raises on failure (hard-fail).
      4. log_assembly_transition() — only when cognition_session_id provided;
         failure is captured in transition_error, not re-raised (soft-fail).
      5. log_activation_decision() — always written when fired; raises on
         failure (hard-fail).

    When not fired and log_non_firing=False: zero DB writes.
    When not fired and log_non_firing=True: one decision row with fired=False.

    Args:
        db_path: SQLite database path (memory DB).
        policy_id: ID of an activation_policies row.
        trigger_event: pre-fetched trigger context dict passed to evaluate_trigger().
        context_policy: retrieval scope for reconstruct(); defaults to
            ContextActivationPolicy() when None.
        cognition_session_id: if provided, a 'policy_update' transition is logged.
        triggered_by: actor initiating execution; must not be empty.
        transition_reason: reason for assembly_transition_log; defaults to
            detection_reason when empty.
        log_non_firing: log a decision row even when fired=False.

    Returns:
        PolicyExecutionResult with full lineage.

    Raises:
        ValueError: policy_id not found, or triggered_by is empty.
        TypeError: trigger_event is not a dict.
        Any exception from reconstruct() or log_assembly() propagates unchanged.
    """
    if not triggered_by or not triggered_by.strip():
        raise ValueError("'triggered_by' must not be empty")
    if not isinstance(trigger_event, dict):
        raise TypeError(
            f"trigger_event must be a dict, got {type(trigger_event).__name__!r}"
        )

    if context_policy is None:
        context_policy = ContextActivationPolicy()

    # Step 1: fetch policy — hard-fail if not found
    policy: ActivationPolicy = get_activation_policy(db_path, policy_id)

    # Step 2: pure evaluation — no I/O
    result: ActivationTriggerResult = evaluate_trigger(policy, trigger_event)

    # Non-firing path
    if not result.fired:
        if not log_non_firing:
            return PolicyExecutionResult(
                policy_id=policy_id,
                decision_id=None,
                fired=False,
                detection_reason=result.detection_reason,
                resulting_assembly_id=None,
                resulting_transition_id=None,
                triggering_artifact_ids=list(result.triggering_artifact_ids),
            )
        decision_id = log_activation_decision(
            db_path, policy, result, trigger_event,
            resulting_retrieval_id=None,
            resulting_assembly_id=None,
            resulting_transition_id=None,
        )
        return PolicyExecutionResult(
            policy_id=policy_id,
            decision_id=decision_id,
            fired=False,
            detection_reason=result.detection_reason,
            resulting_assembly_id=None,
            resulting_transition_id=None,
            triggering_artifact_ids=list(result.triggering_artifact_ids),
        )

    # Firing path — Steps 3-5
    # Lazy import avoids a circular dependency: activation_policy ← execution → reconstruction
    from .reconstruction import log_assembly, log_assembly_transition, reconstruct

    # Step 3: reconstruct + log_assembly — hard-fail zone
    reconstruction = reconstruct(db_path, context_policy)
    assembly_row = log_assembly(db_path, reconstruction)
    assembly_id: int = assembly_row['id']

    # Step 4: session transition — soft-fail
    transition_id: Optional[int] = None
    transition_error: Optional[str] = None

    if cognition_session_id is not None:
        effective_reason = transition_reason.strip() or result.detection_reason

        contradiction_ids = None
        revision_ids = None
        if result.trigger_class == 'contradiction_change':
            contradiction_ids = list(result.triggering_artifact_ids) or None
        elif result.trigger_class == 'confidence_revision':
            revision_ids = list(result.triggering_artifact_ids) or None

        try:
            transition = log_assembly_transition(
                db_path,
                cognition_session_id,
                assembly_id,
                'policy_update',
                triggered_by,
                effective_reason,
                triggering_contradiction_link_ids=contradiction_ids,
                triggering_confidence_revision_ids=revision_ids,
                provenance={'policy_id': policy_id},
            )
            transition_id = transition.id
        except Exception as exc:
            transition_error = str(exc)

    # Step 5: log decision — hard-fail if this raises
    decision_id = log_activation_decision(
        db_path, policy, result, trigger_event,
        resulting_retrieval_id=None,
        resulting_assembly_id=assembly_id,
        resulting_transition_id=transition_id,
    )

    return PolicyExecutionResult(
        policy_id=policy_id,
        decision_id=decision_id,
        fired=True,
        detection_reason=result.detection_reason,
        resulting_assembly_id=assembly_id,
        resulting_transition_id=transition_id,
        triggering_artifact_ids=list(result.triggering_artifact_ids),
        transition_error=transition_error,
    )
