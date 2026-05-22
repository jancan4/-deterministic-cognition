"""
Checkpoint utilities: serialization, creation, and restoration.

All state serialization uses sort_keys=True to maintain determinism across
Python versions and dict ordering. Restoration is a pure function — it never
writes to any database.
"""
import copy
import json
from typing import Any, Dict

from .models import Checkpoint
from .state_store import save_checkpoint


def serialize_checkpoint(state: Dict[str, Any]) -> str:
    """Deterministic JSON serialization for checkpoint state."""
    return json.dumps(state, sort_keys=True)


def create_checkpoint(
    state_db: str,
    runtime_id: int,
    iteration: int,
    state: Dict[str, Any],
    reason: str,
) -> Checkpoint:
    """Persist a checkpoint and return the saved record."""
    return save_checkpoint(state_db, runtime_id, iteration, state, reason)


def restore_from_checkpoint(checkpoint: Checkpoint) -> Dict[str, Any]:
    """
    Extract the state dict from a checkpoint record.

    Pure function — reads from the in-memory Checkpoint object, writes
    nothing. The caller decides what to do with the restored state.

    Returns a deep copy so the caller cannot mutate nested structures in the
    Checkpoint's in-memory state dict.
    """
    return copy.deepcopy(checkpoint.state)
