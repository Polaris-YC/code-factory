"""Adapter registry, capability discovery (`doctor`), and cross-model routing."""

from __future__ import annotations

from typing import Any

from code_factory.adapters.agy import AgyAdapter
from code_factory.adapters.base import BaseAdapter, CapabilityInfo
from code_factory.adapters.claude import ClaudeAdapter
from code_factory.adapters.codex import CodexAdapter
from code_factory.adapters.generic import GenericCliAdapter
from code_factory.adapters.mock import MockAdapter


class AdapterResolutionError(RuntimeError):
    """Raised when no usable CLI adapter can be resolved for a role."""


_ADAPTER_TYPES: dict[str, type[BaseAdapter]] = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
    "agy": AgyAdapter,
    "generic": GenericCliAdapter,
    "mock": MockAdapter,
}


def build_adapter(name: str, cfg: dict[str, Any]) -> BaseAdapter:
    adapter_type = str(cfg.get("type", "generic"))
    cls = _ADAPTER_TYPES.get(adapter_type)
    if cls is None:
        raise AdapterResolutionError(
            f"Unknown adapter type '{adapter_type}' for adapter '{name}'."
        )
    return cls(name=name, config=cfg)


class AdapterRegistry:
    def __init__(
        self,
        adapters: dict[str, BaseAdapter],
        routing: dict[str, list[str]] | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._routing = dict(routing or {})
        self._probe_cache: dict[str, CapabilityInfo] = {}

    @classmethod
    def from_config(
        cls,
        adapter_configs: dict[str, dict[str, Any]],
        routing: dict[str, list[str]],
    ) -> AdapterRegistry:
        built = {
            name: build_adapter(name, cfg) for name, cfg in adapter_configs.items()
        }
        return cls(adapters=built, routing=routing)

    def get(self, name: str) -> BaseAdapter:
        if name not in self._adapters:
            raise AdapterResolutionError(f"Adapter '{name}' is not configured.")
        return self._adapters[name]

    def probe_all(self, refresh: bool = False) -> list[CapabilityInfo]:
        results: list[CapabilityInfo] = []
        for name, adapter in self._adapters.items():
            if refresh or name not in self._probe_cache:
                self._probe_cache[name] = adapter.probe()
            results.append(self._probe_cache[name])
        return results

    def is_available(self, name: str) -> bool:
        if name not in self._adapters:
            return False
        if name not in self._probe_cache:
            self._probe_cache[name] = self._adapters[name].probe()
        return self._probe_cache[name].available

    def pick_adapter(
        self,
        role: str,
        *,
        preferred: str | None = None,
        exclude: tuple[str, ...] = (),
        allow_fallback_to_excluded: bool = True,
    ) -> BaseAdapter:
        """Pick the best available adapter for a role, preferring cross-model diversity."""
        candidates: list[str] = []
        if preferred:
            candidates.append(preferred)
        for name in self._routing.get(role, []):
            if name not in candidates:
                candidates.append(name)
        for name in self._adapters:
            if name not in candidates:
                candidates.append(name)

        excluded_set = set(exclude)
        for name in candidates:
            if name in excluded_set:
                continue
            if self.is_available(name):
                return self._adapters[name]

        if allow_fallback_to_excluded:
            for name in candidates:
                if self.is_available(name):
                    return self._adapters[name]

        available = [c.name for c in self.probe_all() if c.available]
        raise AdapterResolutionError(
            f"No available CLI adapter found for role '{role}'. "
            f"Configured: {list(self._adapters)}, available: {available}."
        )
