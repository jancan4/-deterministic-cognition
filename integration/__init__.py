"""
Deterministic external model integration layer.

Provides a governed, file-based interface for consuming substrate memory via
external LLMs, SLMs, or agent systems without granting write authority to
canonical memory.

Architecture:
  substrate   — canonical cognition state (unchanged by this layer)
  adapter     — transient reasoning interface (packet generation, rendering, result capture)
  model       — disposable cognition processor (stateless, consumer-only, produces candidates)

No model call reaches the DB. No candidate enters memory_events without
traversing operator review (memory-review approve).
"""
