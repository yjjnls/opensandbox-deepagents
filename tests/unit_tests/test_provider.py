"""Unit tests for `OpenSandboxProvider` (no network, SDK surface faked)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from opensandbox.models.sandboxes import SnapshotFilter, SnapshotInfo, SnapshotStatus

pytest.importorskip("deepagents_code")

from deepagents_code.integrations.sandbox_provider import (  # noqa: E402
    SandboxInstallHint,
    SandboxProviderMetadata,
)

from opensandbox_deepagents import OpenSandboxProvider  # noqa: E402
from opensandbox_deepagents.sandbox import OpenSandboxSandbox  # noqa: E402


@dataclass
class FakeManager:
    snapshots: list[SnapshotInfo] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    get_overrides: dict[str, SnapshotInfo] = field(default_factory=dict)

    def list_snapshots(self, snapshot_filter: SnapshotFilter):  # noqa: ANN001
        return Pagination(snapshots=self.snapshots)

    def get_snapshot(self, snapshot_id: str):  # noqa: ANN001
        override = self.get_overrides.get(snapshot_id)
        if override is not None:
            return override
        for snapshot in self.snapshots:
            if snapshot.id == snapshot_id:
                return snapshot
        raise AssertionError(f"unknown snapshot {snapshot_id}")

    def kill_sandbox(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)


@dataclass
class Pagination:
    snapshots: list[SnapshotInfo]

    @property
    def snapshot_infos(self) -> list[SnapshotInfo]:
        return self.snapshots


@dataclass
class FakeBuilder:
    snapshot: SnapshotInfo | None = None
    destroyed: bool = False

    def create_snapshot(self, name=None, mode=None):  # noqa: ANN001, ARG002
        return self.snapshot

    def destroy(self) -> None:
        self.destroyed = True


@dataclass
class FakeSandbox:
    id: str = "sbx-restored"


_NOW = datetime(2026, 11, 17, tzinfo=UTC)


def _snapshot(
    snapshot_id: str,
    name: str | None,
    state: str = "ready",
) -> SnapshotInfo:
    return SnapshotInfo(
        id=snapshot_id,
        sandbox_id="sbx-src",
        name=name,
        status=SnapshotStatus(state=state),
        created_at=_NOW,
    )


def _patch_factory(monkeypatch: pytest.MonkeyPatch, manager: FakeManager):
    """Route `SandboxManagerSync.create()` to the given fake manager."""
    import opensandbox_deepagents.provider as provider_module

    monkeypatch.setattr(
        provider_module.SandboxManagerSync,
        "create",
        classmethod(lambda cls, connection_config=None: manager),
    )


class TestMetadata:
    def test_metadata_shape(self) -> None:
        metadata = OpenSandboxProvider().metadata
        assert isinstance(metadata, SandboxProviderMetadata)
        assert metadata.name == "opensandbox"
        assert metadata.supports_sandbox_id is True
        assert metadata.supports_snapshot_name is True
        assert metadata.install == SandboxInstallHint(
            kind="package", name="opensandbox-deepagents"
        )


class TestGetOrCreate:
    def test_connect_by_sandbox_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_connect(sandbox_id, connection_config=None, **kwargs):  # noqa: ARG001
            captured["sandbox_id"] = sandbox_id
            return FakeSandbox(id="sbx-9")

        import opensandbox_deepagents.provider as provider_module

        monkeypatch.setattr(provider_module.SandboxSync, "connect", fake_connect)
        backend = OpenSandboxProvider().get_or_create(sandbox_id="sbx-9")
        assert isinstance(backend, OpenSandboxSandbox)
        assert backend.id == "sbx-9"
        assert captured["sandbox_id"] == "sbx-9"

    def test_create_default_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_create(image=None, **kwargs):  # noqa: ANN001
            captured["image"] = image
            captured["kwargs"] = kwargs
            return FakeSandbox()

        import opensandbox_deepagents.provider as provider_module

        monkeypatch.setattr(provider_module.SandboxSync, "create", fake_create)
        OpenSandboxProvider().get_or_create()
        assert captured["image"] == "python:3.12-slim"
        assert captured["kwargs"]["timeout"] == timedelta(minutes=60)

    def test_lifetime_zero_disables_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_create(image=None, **kwargs):  # noqa: ANN001
            captured["kwargs"] = kwargs
            return FakeSandbox()

        import opensandbox_deepagents.provider as provider_module

        monkeypatch.setattr(provider_module.SandboxSync, "create", fake_create)
        OpenSandboxProvider().get_or_create(lifetime_seconds=0)
        assert captured["kwargs"]["timeout"] is None

    def test_existing_snapshot_is_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = FakeManager(snapshots=[_snapshot("snap-1", "gold")])
        _patch_factory(monkeypatch, manager)
        builders: list[FakeBuilder] = []
        creates: list[dict] = []

        def fake_create(image=None, **kwargs):  # noqa: ANN001
            if kwargs.get("snapshot_id") is not None:
                creates.append(kwargs)
                return FakeSandbox()
            builder = FakeBuilder(snapshot=_snapshot("snap-new", "gold"))
            builders.append(builder)
            return builder

        import opensandbox_deepagents.provider as provider_module

        monkeypatch.setattr(provider_module.SandboxSync, "create", fake_create)
        OpenSandboxProvider().get_or_create(snapshot="gold")
        assert builders == []  # no builder sandbox was launched
        assert creates and creates[0]["snapshot_id"] == "snap-1"

    def test_missing_snapshot_is_built_then_restored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = FakeManager(snapshots=[])
        _patch_factory(monkeypatch, manager)
        builders: list[FakeBuilder] = []
        creates: list[dict] = []

        def fake_create(image=None, **kwargs):  # noqa: ANN001
            if kwargs.get("snapshot_id") is not None:
                creates.append(kwargs)
                return FakeSandbox()
            builder = FakeBuilder(snapshot=_snapshot("snap-new", "gold"))
            builders.append(builder)
            return builder

        import opensandbox_deepagents.provider as provider_module

        monkeypatch.setattr(provider_module.SandboxSync, "create", fake_create)
        OpenSandboxProvider().get_or_create(snapshot="gold", image="python:3.11")
        assert len(builders) == 1
        assert builders[0].destroyed is True
        assert creates and creates[0]["snapshot_id"] == "snap-new"
        assert "image" not in creates[0]  # image goes to the builder, not restore

    def test_terminal_snapshot_state_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = FakeManager(
            snapshots=[],
            get_overrides={"snap-x": _snapshot("snap-x", "gold", state="failed")},
        )
        _patch_factory(monkeypatch, manager)

        def fake_create(image=None, **kwargs):  # noqa: ANN001
            if kwargs.get("snapshot_id") is not None:
                return FakeSandbox()
            return FakeBuilder(snapshot=_snapshot("snap-x", "gold", state="failed"))

        import opensandbox_deepagents.provider as provider_module

        monkeypatch.setattr(provider_module.SandboxSync, "create", fake_create)
        with pytest.raises(RuntimeError, match="terminal state"):
            OpenSandboxProvider().get_or_create(snapshot="gold")


class TestDelete:
    def test_delete_kills_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = FakeManager()
        _patch_factory(monkeypatch, manager)
        OpenSandboxProvider().delete(sandbox_id="sbx-42")
        assert manager.killed == ["sbx-42"]
