"""Unified CLI adapter layer for heterogeneous coding agents."""

from code_factory.adapters.base import BaseAdapter, CapabilityInfo
from code_factory.adapters.registry import AdapterRegistry

__all__ = ["AdapterRegistry", "BaseAdapter", "CapabilityInfo"]
