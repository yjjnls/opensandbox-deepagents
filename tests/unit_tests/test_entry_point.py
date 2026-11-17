"""Entry-point registration tests (require `deepagents-code`, py3.14+)."""

from __future__ import annotations

import importlib.metadata

import pytest

pytest.importorskip("deepagents_code")

from deepagents_code.integrations.sandbox_registry import (  # noqa: E402
    ENTRY_POINT_GROUP,
    SandboxRegistry,
)

ENTRY_POINT_NAME = "opensandbox"


def _provider_entry_points() -> dict[str, importlib.metadata.EntryPoint]:
    entries = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    return {entry.name: entry for entry in entries}


def test_package_registered_under_dcode_entry_point_group() -> None:
    assert ENTRY_POINT_NAME in _provider_entry_points()


def test_entry_point_loads_provider() -> None:
    entry = _provider_entry_points()[ENTRY_POINT_NAME]
    provider = entry.load()()
    assert provider.metadata.name == ENTRY_POINT_NAME


def test_registry_resolves_provider() -> None:
    registry = SandboxRegistry(include_entry_points=True)
    assert registry.is_available(ENTRY_POINT_NAME)
    provider = registry.create_provider(ENTRY_POINT_NAME)
    assert provider.metadata.name == ENTRY_POINT_NAME
    metadata = registry.provider_metadata(ENTRY_POINT_NAME)
    assert metadata.supports_snapshot_name is True
    assert metadata.working_dir == "/workspace"
