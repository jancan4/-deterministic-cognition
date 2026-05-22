"""Tests for models/registry.py."""
import pytest

from semantic.models import SEMANTIC_TASK_TYPES
from models.adapters import EchoModelAdapter, LocalModelAdapter, StubModelAdapter
from models.registry import AdapterRegistry, AdapterRegistryError, make_default_registry


# ---------------------------------------------------------------------------
# AdapterRegistry — registration
# ---------------------------------------------------------------------------

class TestAdapterRegistryRegistration:
    def test_register_and_get(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        adapter = registry.get('stub')
        assert isinstance(adapter, StubModelAdapter)

    def test_register_two_adapters(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.register(EchoModelAdapter())
        assert registry.contains('stub')
        assert registry.contains('echo')

    def test_duplicate_raises_without_replace(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        with pytest.raises(AdapterRegistryError, match='already registered'):
            registry.register(StubModelAdapter())

    def test_duplicate_allowed_with_replace(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.register(StubModelAdapter(), replace=True)  # no exception
        assert registry.contains('stub')

    def test_replace_updates_adapter(self):
        class StubV2(StubModelAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self): return '2.0.0'

        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.register(StubV2(), replace=True)
        assert registry.get('stub').adapter_version == '2.0.0'

    def test_non_adapter_raises(self):
        registry = AdapterRegistry()
        with pytest.raises(AdapterRegistryError):
            registry.register({'name': 'fake'})

    def test_len(self):
        registry = AdapterRegistry()
        assert len(registry) == 0
        registry.register(StubModelAdapter())
        assert len(registry) == 1
        registry.register(EchoModelAdapter())
        assert len(registry) == 2


# ---------------------------------------------------------------------------
# AdapterRegistry — lookup
# ---------------------------------------------------------------------------

class TestAdapterRegistryLookup:
    def test_get_unknown_raises(self):
        registry = AdapterRegistry()
        with pytest.raises(AdapterRegistryError, match="No adapter registered"):
            registry.get('unknown')

    def test_error_lists_registered_adapters(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        with pytest.raises(AdapterRegistryError, match='stub'):
            registry.get('missing')

    def test_contains_true(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        assert registry.contains('stub') is True

    def test_contains_false(self):
        registry = AdapterRegistry()
        assert registry.contains('stub') is False


# ---------------------------------------------------------------------------
# AdapterRegistry — deterministic ordering
# ---------------------------------------------------------------------------

class TestAdapterRegistryOrdering:
    def test_list_names_sorted(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.register(EchoModelAdapter())
        names = registry.list_names()
        assert names == sorted(names)

    def test_list_names_sorted_regardless_of_registration_order(self):
        r1 = AdapterRegistry()
        r1.register(StubModelAdapter())
        r1.register(EchoModelAdapter())

        r2 = AdapterRegistry()
        r2.register(EchoModelAdapter())
        r2.register(StubModelAdapter())

        assert r1.list_names() == r2.list_names()

    def test_list_adapters_sorted_by_name(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.register(EchoModelAdapter())
        adapters = registry.list_adapters()
        names = [a.adapter_name for a in adapters]
        assert names == sorted(names)

    def test_empty_registry_list_names(self):
        registry = AdapterRegistry()
        assert registry.list_names() == []

    def test_repr_shows_sorted_names(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.register(EchoModelAdapter())
        r = repr(registry)
        assert 'echo' in r
        assert 'stub' in r


# ---------------------------------------------------------------------------
# AdapterRegistry — unregister
# ---------------------------------------------------------------------------

class TestAdapterRegistryUnregister:
    def test_unregister_removes_adapter(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.unregister('stub')
        assert not registry.contains('stub')

    def test_unregister_unknown_raises(self):
        registry = AdapterRegistry()
        with pytest.raises(AdapterRegistryError, match='Cannot unregister'):
            registry.unregister('unknown')

    def test_unregister_reduces_len(self):
        registry = AdapterRegistry()
        registry.register(StubModelAdapter())
        registry.register(EchoModelAdapter())
        registry.unregister('stub')
        assert len(registry) == 1


# ---------------------------------------------------------------------------
# make_default_registry
# ---------------------------------------------------------------------------

class TestMakeDefaultRegistry:
    def test_returns_registry(self):
        registry = make_default_registry()
        assert isinstance(registry, AdapterRegistry)

    def test_stub_registered(self):
        registry = make_default_registry()
        assert registry.contains('stub')
        assert isinstance(registry.get('stub'), StubModelAdapter)

    def test_echo_registered(self):
        registry = make_default_registry()
        assert registry.contains('echo')
        assert isinstance(registry.get('echo'), EchoModelAdapter)

    def test_two_adapters_registered(self):
        registry = make_default_registry()
        assert len(registry) == 2

    def test_names_deterministic(self):
        r1 = make_default_registry()
        r2 = make_default_registry()
        assert r1.list_names() == r2.list_names()

    def test_names_sorted(self):
        registry = make_default_registry()
        names = registry.list_names()
        assert names == sorted(names)

    def test_independent_registries(self):
        """Two default registries do not share state."""
        r1 = make_default_registry()
        r2 = make_default_registry()
        r1.unregister('stub')
        assert r2.contains('stub')  # r2 unaffected
