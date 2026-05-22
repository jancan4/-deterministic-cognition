from typing import FrozenSet, List, Optional, Tuple

from .models import TERMINAL_STATES, VALID_STATES, VALID_TRANSITIONS


class TransitionError(ValueError):
    pass


def can_transition(old_state: str, new_state: str) -> bool:
    """Return True if old_state → new_state is a permitted transition."""
    return new_state in VALID_TRANSITIONS.get(old_state, frozenset())


def validate_transition(old_state: str, new_state: str) -> None:
    """Raise TransitionError if the transition is not permitted."""
    if old_state not in VALID_TRANSITIONS:
        raise TransitionError(f"Unknown source state: '{old_state}'")
    if new_state not in VALID_STATES:
        raise TransitionError(f"Unknown target state: '{new_state}'")
    if not can_transition(old_state, new_state):
        valid = sorted(VALID_TRANSITIONS[old_state])
        label = valid if valid else ['(none — terminal state)']
        raise TransitionError(
            f"Invalid transition '{old_state}' → '{new_state}'. "
            f"Valid transitions from '{old_state}': {label}"
        )


def is_terminal(state: str) -> bool:
    """Return True if state has no valid outgoing transitions."""
    return state in TERMINAL_STATES


def get_valid_transitions(state: str) -> FrozenSet[str]:
    """Return the set of states reachable from state in one hop."""
    return VALID_TRANSITIONS.get(state, frozenset())


def replay_state_history(lineage: list) -> List[Tuple[Optional[str], str]]:
    """
    Reconstruct the state sequence from a task's lineage events.

    Returns a list of (old_state, new_state) tuples in chronological order,
    ordered by lineage event id (ascending). The first entry always has
    old_state=None (task creation event).
    """
    return [(ev.old_state, ev.new_state) for ev in lineage]


def current_state_from_lineage(lineage: list) -> Optional[str]:
    """
    Derive the current state purely from the lineage log, without reading
    the tasks table. Returns None if lineage is empty.
    """
    if not lineage:
        return None
    return lineage[-1].new_state
