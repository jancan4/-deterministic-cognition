"""
Adapter registry: explicit, deterministic registration of local model adapters.

An AdapterRegistry maps adapter names to LocalModelAdapter instances.
No implicit adapters. No dynamic discovery. No network calls.

Built-in adapters (stub, echo) are registered by make_default_registry().
Custom adapters are added via register().

Ordering guarantee:
  list_names() always returns sorted adapter names, regardless of
  registration order. This makes iteration deterministic.

Duplicate handling:
  Registering a name that already exists raises AdapterRegistryError
  unless replace=True is passed explicitly.
"""
from typing import Dict, List

from typing import Optional

from .adapters import EchoModelAdapter, LocalModelAdapter, StubModelAdapter


class AdapterRegistryError(ValueError):
    pass


class AdapterRegistry:
    """
    Explicit, ordered registry of LocalModelAdapter instances.

    Thread-safety: not thread-safe. Single-threaded CLI use only.
    """

    def __init__(self) -> None:
        self._adapters: Dict[str, LocalModelAdapter] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(self, adapter: LocalModelAdapter, replace: bool = False) -> None:
        """
        Register an adapter.

        Raises AdapterRegistryError if the name is already registered and
        replace=False (the default).
        """
        if not isinstance(adapter, LocalModelAdapter):
            raise AdapterRegistryError(
                f"Expected a LocalModelAdapter subclass, got {type(adapter).__name__}"
            )
        name = adapter.adapter_name
        if not name or not name.strip():
            raise AdapterRegistryError("adapter.adapter_name must not be empty")
        if name in self._adapters and not replace:
            raise AdapterRegistryError(
                f"Adapter {name!r} is already registered. "
                f"Pass replace=True to overwrite."
            )
        self._adapters[name] = adapter

    def unregister(self, name: str) -> None:
        """Remove an adapter by name. Raises if not found."""
        if name not in self._adapters:
            raise AdapterRegistryError(
                f"Cannot unregister {name!r}: not registered. "
                f"Registered: {self.list_names()}"
            )
        del self._adapters[name]

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> LocalModelAdapter:
        """
        Return the adapter with the given name.

        Raises AdapterRegistryError with the list of registered names if
        the name is not found.
        """
        if name not in self._adapters:
            raise AdapterRegistryError(
                f"No adapter registered with name {name!r}. "
                f"Registered adapters: {self.list_names()}"
            )
        return self._adapters[name]

    def contains(self, name: str) -> bool:
        """Return True if an adapter with this name is registered."""
        return name in self._adapters

    # ------------------------------------------------------------------
    # Introspection (deterministic ordering)
    # ------------------------------------------------------------------

    def list_names(self) -> List[str]:
        """Return sorted list of registered adapter names."""
        return sorted(self._adapters.keys())

    def list_adapters(self) -> List[LocalModelAdapter]:
        """Return adapters in sorted-by-name order."""
        return [self._adapters[name] for name in self.list_names()]

    def __len__(self) -> int:
        return len(self._adapters)

    def __repr__(self) -> str:
        return f"AdapterRegistry({self.list_names()})"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_default_registry() -> AdapterRegistry:
    """
    Return a registry pre-populated with all built-in adapters.

    Built-in adapters:
      stub  — StubModelAdapter: always returns deterministic stub response
      echo  — EchoModelAdapter: derives labels from capitalised input tokens
    """
    registry = AdapterRegistry()
    registry.register(StubModelAdapter())
    registry.register(EchoModelAdapter())
    return registry


def make_ollama_registry(
    model: str,
    base_url: str = 'http://localhost:11434',
    version: str = '1.0.0',
    timeout_seconds: float = 60.0,
    temperature: float = 0.0,
    seed: int = 42,
    num_predict: int = 512,
) -> AdapterRegistry:
    """
    Return a registry pre-populated with built-in adapters plus an OllamaAdapter.

    Requires 'requests' to be installed and a running Ollama instance.
    Raises ModelContractError if 'requests' is not available.

    The Ollama adapter is registered under the name 'ollama'. Built-in
    stub and echo adapters are also registered for fallback use.
    """
    from .ollama_adapter import OllamaAdapter
    registry = make_default_registry()
    registry.register(OllamaAdapter(
        model=model,
        base_url=base_url,
        version=version,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        seed=seed,
        num_predict=num_predict,
    ))
    return registry
